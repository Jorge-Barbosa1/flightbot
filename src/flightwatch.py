#!/usr/bin/env python3
"""Koningsdag flight watch.

Vigia preços de voos Porto -> Países Baixos à volta do Koningsdag (27 abril 2027)
e avisa no Discord (webhook) quando aparece algo abaixo do limite definido.

Uso:
    python src/flightwatch.py                # corre as pesquisas e envia alertas
    python src/flightwatch.py --dry-run      # não envia nada ao Discord, só imprime
    python src/flightwatch.py --summary      # envia o top N mais baratos (resumo diário)
    python src/flightwatch.py --test-discord # envia uma mensagem de teste ao webhook
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "data" / "state.json"
HISTORY_PATH = ROOT / "data" / "history.csv"

HISTORY_FIELDS = [
    "checked_at", "origin", "destination", "depart", "return",
    "price_per_person", "airlines", "stops_out", "currency",
]


# ---------------------------------------------------------------- modelos
@dataclass(frozen=True)
class Offer:
    origin: str
    destination: str
    depart: str          # YYYY-MM-DD
    ret: str             # YYYY-MM-DD
    price: float         # por pessoa, ida e volta
    airlines: str
    stops_out: int
    currency: str

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}|{self.depart}|{self.ret}"

    def google_flights_url(self) -> str:
        """Link para abrires a pesquisa equivalente no Google Flights."""
        from fast_flights import FlightQuery, Passengers, create_query

        q = create_query(
            flights=[
                FlightQuery(date=self.depart, from_airport=self.origin, to_airport=self.destination),
                FlightQuery(date=self.ret, from_airport=self.destination, to_airport=self.origin),
            ],
            trip="round-trip",
            passengers=Passengers(adults=1),
            currency=self.currency,
            language="pt-PT",
        )
        return q.url()


# ---------------------------------------------------------------- config
def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def daterange(start: str, end: str) -> list[str]:
    d0, d1 = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def build_combinations(cfg: dict) -> list[tuple[str, str, str, str]]:
    """(origem, destino, ida, volta) respeitando o nº mínimo/máximo de noites."""
    out_dates = daterange(*cfg["dates"]["outbound"])
    ret_dates = daterange(*cfg["dates"]["return"])
    lo, hi = cfg["dates"]["min_nights"], cfg["dates"]["max_nights"]
    combos = []
    for dest, o, r in itertools.product(cfg["destinations"], out_dates, ret_dates):
        nights = (date.fromisoformat(r) - date.fromisoformat(o)).days
        if lo <= nights <= hi:
            combos.append((cfg["origin"], dest, o, r))
    limit = cfg.get("max_queries_per_run")
    return combos[:limit] if limit else combos


# ---------------------------------------------------------------- fornecedor
def fetch_cheapest(origin: str, dest: str, depart: str, ret: str, cfg: dict) -> Offer | None:
    """Pesquisa no Google Flights (via fast-flights) e devolve a oferta mais barata.

    Para trocar de fornecedor (ex.: SerpApi), basta substituir esta função por
    outra que devolva um Offer ou None. O resto do código não muda.
    """
    from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

    s = cfg["search"]
    q = create_query(
        flights=[
            FlightQuery(date=depart, from_airport=origin, to_airport=dest, max_stops=s.get("max_stops")),
            FlightQuery(date=ret, from_airport=dest, to_airport=origin, max_stops=s.get("max_stops")),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),  # preço por pessoa; multiplicamos depois
        currency=s["currency"],
        language="pt-PT",
        carry_on_bags=s.get("carry_on_bags", 0),
        checked_bags=s.get("checked_bags", 0),
    )
    try:
        results = get_flights(q)
    except FlightsNotFound:
        return None

    priced = [f for f in results if getattr(f, "price", 0) and f.price > 0]
    if not priced:
        return None
    best = min(priced, key=lambda f: f.price)
    return Offer(
        origin=origin, destination=dest, depart=depart, ret=ret,
        price=float(best.price),
        airlines=", ".join(best.airlines),
        stops_out=max(len(best.flights) - 1, 0),
        currency=s["currency"],
    )


# ---------------------------------------------------------------- estado
def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def append_history(offers: list[Offer], when: str, path: Path = HISTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(HISTORY_FIELDS)
        for o in offers:
            w.writerow([when, o.origin, o.destination, o.depart, o.ret,
                        o.price, o.airlines, o.stops_out, o.currency])


# ---------------------------------------------------------------- alertas
def should_alert(offer: Offer, state: dict, cfg: dict) -> bool:
    """Alerta se estiver abaixo do limite E for novo ou tiver baixado o
    suficiente face ao último preço alertado (evita spam)."""
    a = cfg["alerts"]
    if offer.price > a["max_price_per_person"]:
        return False
    last = state.get(offer.key, {}).get("last_alerted")
    if last is None:
        return True
    return offer.price <= last - a["min_drop_to_realert"]


def format_alert(offer: Offer, cfg: dict) -> str:
    n = cfg["search"]["passengers"]
    total = offer.price * n
    stops = "direto" if offer.stops_out == 0 else f"{offer.stops_out} escala(s)"
    return (
        f"✈️ **{offer.origin} → {offer.destination}** | {offer.depart} → {offer.ret}\n"
        f"**{offer.price:.0f} {offer.currency}/pessoa** (≈ {total:.0f} {offer.currency} para {n}) "
        f"· {offer.airlines} · {stops} na ida\n"
        f"<{offer.google_flights_url()}>"
    )


def send_discord(text: str, dry_run: bool = False) -> None:
    if dry_run:
        print("[dry-run] Discord:\n" + text + "\n")
        return
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise RuntimeError("Falta a variável de ambiente DISCORD_WEBHOOK_URL")
    for i in range(0, len(text), 1900):  # limite do Discord: 2000 caracteres
        r = requests.post(url, json={"content": text[i:i + 1900]}, timeout=15)
        r.raise_for_status()
        time.sleep(0.5)


# ---------------------------------------------------------------- execução
def run(cfg: dict, dry_run: bool = False, summary: bool = False,
        fetch=fetch_cheapest, state_path: Path = STATE_PATH,
        history_path: Path = HISTORY_PATH, sleep=time.sleep) -> int:
    combos = build_combinations(cfg)
    print(f"{len(combos)} pesquisas a fazer")
    state = load_state(state_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    offers: list[Offer] = []
    failures = 0
    for origin, dest, o, r in combos:
        try:
            offer = fetch(origin, dest, o, r, cfg)
        except Exception as e:  # noqa: BLE001 - não queremos parar tudo por uma pesquisa
            failures += 1
            print(f"  ! {origin}-{dest} {o}->{r}: {type(e).__name__}: {str(e)[:120]}")
            sleep(cfg["search"]["delay_seconds"])
            continue
        if offer:
            offers.append(offer)
            print(f"  {offer.key}: {offer.price:.0f} {offer.currency} ({offer.airlines})")
        sleep(cfg["search"]["delay_seconds"])

    if combos and failures == len(combos):
        print("Todas as pesquisas falharam (o Google pode estar a bloquear este IP).")
        return 1

    append_history(offers, now, history_path)

    if summary:
        top = sorted(offers, key=lambda x: x.price)[: cfg["alerts"]["summary_top_n"]]
        if top:
            body = "📊 **Resumo diário – mais baratos agora**\n\n" + "\n\n".join(
                format_alert(t, cfg) for t in top)
            send_discord(body, dry_run)
    else:
        for offer in sorted(offers, key=lambda x: x.price):
            if should_alert(offer, state, cfg):
                send_discord(format_alert(offer, cfg), dry_run)
                state.setdefault(offer.key, {})["last_alerted"] = offer.price

    for offer in offers:
        s = state.setdefault(offer.key, {})
        s["last_seen"] = offer.price
        s["best_seen"] = min(offer.price, s.get("best_seen", offer.price))
        s["updated"] = now

    if not dry_run:
        save_state(state, state_path)
    return 0


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Carrega o .env local (se existir) sem dependências extra."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="não envia nada nem grava estado")
    p.add_argument("--summary", action="store_true", help="envia o top N em vez de alertas")
    p.add_argument("--test-discord", action="store_true", help="envia mensagem de teste")
    p.add_argument("--config", default=str(CONFIG_PATH))
    args = p.parse_args()

    if args.test_discord:
        send_discord("✅ Koningsdag flight watch: ligação ao Discord a funcionar.")
        return 0
    return run(load_config(Path(args.config)), dry_run=args.dry_run, summary=args.summary)


if __name__ == "__main__":
    sys.exit(main())
