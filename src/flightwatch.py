#!/usr/bin/env python3
"""Koningsdag flight watch.

Vigia preços de voos Porto -> Países Baixos (e aeroportos vizinhos com comboio
para Amesterdão) à volta do Koningsdag (27 abril 2027) e avisa no Discord
(webhook) quando aparece uma viagem abaixo do limite definido.

Pesquisa a ida e a volta em separado (só ida), o que permite misturar
companhias e aeroportos: ex. ir pela Ryanair para Weeze e voltar de Amesterdão.
Nos aeroportos de `roundtrip_airports` pesquisa também ida e volta na mesma
companhia, e fica com a opção mais barata.

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
HISTORY_PATH = ROOT / "data" / "legs.csv"

HISTORY_FIELDS = ["checked_at", "from", "to", "date", "price", "airlines", "stops", "currency", "return"]

MONTHS_PT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


# ---------------------------------------------------------------- modelos
@dataclass(frozen=True)
class Leg:
    """O voo mais barato encontrado para essa rota e dia. Só ida, ou ida e
    volta na mesma companhia se `ret_day` estiver preenchido."""
    frm: str
    to: str
    day: str             # YYYY-MM-DD
    price: float         # por pessoa (ida e volta, se ret_day)
    airlines: str
    stops: int           # escalas na ida
    currency: str
    ret_day: str | None = None

    @property
    def key(self) -> str:
        return f"{self.frm}-{self.to}|{self.day}" + (f"|{self.ret_day}" if self.ret_day else "")

    def google_flights_url(self) -> str:
        """Link para abrires a pesquisa equivalente no Google Flights."""
        from fast_flights import FlightQuery, Passengers, create_query

        flights = [FlightQuery(date=self.day, from_airport=self.frm, to_airport=self.to)]
        if self.ret_day:
            flights.append(FlightQuery(date=self.ret_day, from_airport=self.to, to_airport=self.frm))
        q = create_query(
            flights=flights,
            trip="round-trip" if self.ret_day else "one-way",
            passengers=Passengers(adults=1),
            currency=self.currency,
            language="pt-PT",
        )
        return q.url()


@dataclass(frozen=True)
class Trip:
    """Ida + volta, com o custo estimado de comboio/autocarro até Amesterdão.

    Se `ret` for None, `out` é um bilhete de ida e volta na mesma companhia.
    """
    out: Leg
    ret: Leg | None
    ground_out: float    # do aeroporto de chegada até Amesterdão
    ground_ret: float    # de Amesterdão até ao aeroporto de partida

    @property
    def ret_day(self) -> str:
        return self.ret.day if self.ret else self.out.ret_day

    @property
    def key(self) -> str:
        return f"{self.out.day}>{self.ret_day}"

    @property
    def total(self) -> float:
        flights = self.out.price + (self.ret.price if self.ret else 0)
        return flights + self.ground_out + self.ground_ret

    @property
    def nights(self) -> int:
        return (date.fromisoformat(self.ret_day) - date.fromisoformat(self.out.day)).days


# ---------------------------------------------------------------- config
def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def daterange(start: str, end: str) -> list[str]:
    d0, d1 = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def date_pairs(cfg: dict) -> list[tuple[str, str]]:
    """(ida, volta) respeitando o nº mínimo/máximo de noites."""
    lo, hi = cfg["dates"]["min_nights"], cfg["dates"]["max_nights"]
    return [
        (o, r)
        for o, r in itertools.product(daterange(*cfg["dates"]["outbound"]),
                                      daterange(*cfg["dates"]["return"]))
        if lo <= (date.fromisoformat(r) - date.fromisoformat(o)).days <= hi
    ]


def build_searches(cfg: dict) -> list[tuple[str, str, str, str | None]]:
    """(de, para, dia, volta): idas Porto -> cada aeroporto, voltas cada
    aeroporto -> Porto (volta = None) e ida e volta nos `roundtrip_airports`."""
    pairs = date_pairs(cfg)
    out_days = sorted({o for o, _ in pairs})
    ret_days = sorted({r for _, r in pairs})
    origin, airports = cfg["origin"], list(cfg["airports"])
    searches = [(origin, a, d, None) for d in out_days for a in airports]
    searches += [(a, origin, d, None) for d in ret_days for a in airports]
    searches += [(origin, a, o, r) for o, r in pairs for a in cfg.get("roundtrip_airports", [])]
    limit = cfg.get("max_queries_per_run")
    return searches[:limit] if limit else searches


def ground_cost(cfg: dict, airport: str) -> float:
    return float(cfg["airports"][airport].get("ground_cost", 0))


def build_trips(legs: list[Leg], cfg: dict) -> list[Trip]:
    """A viagem mais barata (voos + comboio) para cada par de datas válido.

    A ida e a volta são escolhidas de forma independente, por isso podem ser
    de aeroportos e companhias diferentes. Um bilhete de ida e volta na mesma
    companhia ganha se custar o mesmo ou menos.
    """
    origin = cfg["origin"]
    best_out: dict[str, tuple[float, Leg]] = {}
    best_ret: dict[str, tuple[float, Leg]] = {}
    best_rt: dict[tuple[str, str], tuple[float, Leg]] = {}
    for leg in legs:
        if leg.ret_day:
            cost, pair = leg.price + 2 * ground_cost(cfg, leg.to), (leg.day, leg.ret_day)
            if pair not in best_rt or cost < best_rt[pair][0]:
                best_rt[pair] = (cost, leg)
            continue
        if leg.frm == origin:
            cost, table = leg.price + ground_cost(cfg, leg.to), best_out
        else:
            cost, table = leg.price + ground_cost(cfg, leg.frm), best_ret
        if leg.day not in table or cost < table[leg.day][0]:
            table[leg.day] = (cost, leg)

    trips = []
    for o, r in date_pairs(cfg):
        options = []
        if (o, r) in best_rt:
            rt = best_rt[(o, r)][1]
            g = ground_cost(cfg, rt.to)
            options.append(Trip(rt, None, g, g))
        if o in best_out and r in best_ret:
            out, ret = best_out[o][1], best_ret[r][1]
            options.append(Trip(out, ret, ground_cost(cfg, out.to), ground_cost(cfg, ret.frm)))
        if options:
            # em caso de empate fica o de ida e volta (vem primeiro)
            trips.append(min(options, key=lambda t: t.total))
    return sorted(trips, key=lambda t: t.total)


# ---------------------------------------------------------------- fornecedor
# Cookie de "rejeitar tudo". Sem ele, pedidos vindos da UE recebem a página de
# consentimento ("Antes de continuar") em vez dos resultados.
GOOGLE_CONSENT_COOKIES = {"SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}


def _consent_fetcher():
    from fast_flights.integrations.base import FetchIntegration
    from primp import Client

    class ConsentFetcher(FetchIntegration):
        def fetch_html(self, q, /) -> str:
            client = Client(impersonate="chrome_145", impersonate_os="macos",
                            referer=True, cookie_store=True)
            params = q.params() if hasattr(q, "params") else {"q": q}
            return client.get("https://www.google.com/travel/flights", params=params,
                              cookies=GOOGLE_CONSENT_COOKIES).text

    return ConsentFetcher()


def fetch_cheapest(frm: str, to: str, day: str, ret: str | None, cfg: dict) -> Leg | None:
    """Pesquisa no Google Flights (via fast-flights) e devolve o voo mais
    barato: só ida, ou ida e volta se `ret` estiver preenchido.

    Para trocar de fornecedor (ex.: SerpApi), basta substituir esta função por
    outra que devolva um Leg ou None. O resto do código não muda.
    """
    from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

    s = cfg["search"]
    flights = [FlightQuery(date=day, from_airport=frm, to_airport=to, max_stops=s.get("max_stops"))]
    if ret:
        flights.append(FlightQuery(date=ret, from_airport=to, to_airport=frm, max_stops=s.get("max_stops")))
    q = create_query(
        flights=flights,
        trip="round-trip" if ret else "one-way",
        seat="economy",
        passengers=Passengers(adults=1),  # preço por pessoa; multiplicamos depois
        currency=s["currency"],
        language="pt-PT",
        carry_on_bags=s.get("carry_on_bags", 0),
        checked_bags=s.get("checked_bags", 0),
    )
    try:
        results = get_flights(q, integration=_consent_fetcher())
    except FlightsNotFound:
        return None
    except TypeError:
        # o parser rebenta assim quando a página vem sem voos (ex.: a Ryanair
        # não voa nesse dia). Um bloqueio do Google dá outro erro e é contado.
        return None

    priced = [f for f in results if getattr(f, "price", 0) and f.price > 0]
    if not priced:
        return None
    best = min(priced, key=lambda f: f.price)
    return Leg(
        frm=frm, to=to, day=day,
        price=float(best.price),
        airlines=", ".join(best.airlines),
        stops=max(len(best.flights) - 1, 0),
        currency=s["currency"],
        ret_day=ret,
    )


# ---------------------------------------------------------------- estado
def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        # descarta chaves do formato antigo (ida+volta por rota)
        return {k: v for k, v in state.items() if ">" in k}
    return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def append_history(legs: list[Leg], when: str, path: Path = HISTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(HISTORY_FIELDS)
        for leg in legs:
            w.writerow([when, leg.frm, leg.to, leg.day, leg.price, leg.airlines, leg.stops, leg.currency,
                        leg.ret_day or ""])


# ---------------------------------------------------------------- alertas
def should_alert(trip: Trip, state: dict, cfg: dict) -> bool:
    """Alerta se o total estiver abaixo do limite E for novo ou tiver baixado o
    suficiente face ao último total alertado (evita spam)."""
    a = cfg["alerts"]
    if trip.total > a["max_price_per_person"]:
        return False
    last = state.get(trip.key, {}).get("last_alerted")
    if last is None:
        return True
    return trip.total <= last - a["min_drop_to_realert"]


def fmt_day(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.day} {MONTHS_PT[x.month - 1]}"


def format_leg(label: str, leg: Leg, ground: float, cfg: dict) -> str:
    names = cfg["airports"]
    other = leg.to if leg.frm == cfg["origin"] else leg.frm
    stops = "direto" if leg.stops == 0 else f"{leg.stops} escala(s)"
    if leg.ret_day:
        line = (f"**{label}** {fmt_day(leg.day)} → {fmt_day(leg.ret_day)}: {leg.frm} ⇄ {leg.to} "
                f"({names[other].get('name', other)}) · {leg.price:.0f} {leg.currency} · {leg.airlines} · "
                f"{stops} na ida")
    else:
        line = (f"**{label}** {fmt_day(leg.day)}: {leg.frm} → {leg.to} ({names[other].get('name', other)}) · "
                f"{leg.price:.0f} {leg.currency} · {leg.airlines} · {stops}")
    if ground:
        each_way = " (cada sentido)" if leg.ret_day else ""
        line += (f"\n  + comboio/autocarro{each_way} ≈ {ground:.0f} {leg.currency} · "
                 f"{names[other].get('ground_time', '')}")
    return line + f"\n  <{leg.google_flights_url()}>"


def format_alert(trip: Trip, cfg: dict) -> str:
    n = cfg["search"]["passengers"]
    cur = trip.out.currency
    return (
        f"✈️ **{fmt_day(trip.out.day)} → {fmt_day(trip.ret_day)}** · {trip.nights} noites · "
        f"**{trip.total:.0f} {cur}/pessoa** (≈ {trip.total * n:.0f} {cur} para {n})\n"
        + (format_leg("Ida e volta", trip.out, trip.ground_out, cfg) if trip.ret is None else
           f"{format_leg('Ida', trip.out, trip.ground_out, cfg)}\n"
           f"{format_leg('Volta', trip.ret, trip.ground_ret, cfg)}")
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
    searches = build_searches(cfg)
    print(f"{len(searches)} pesquisas a fazer")
    state = load_state(state_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    legs: list[Leg] = []
    failures = 0
    for frm, to, day, ret in searches:
        try:
            leg = fetch(frm, to, day, ret, cfg)
        except Exception as e:  # noqa: BLE001 - não queremos parar tudo por uma pesquisa
            failures += 1
            print(f"  ! {frm}-{to} {day}{'->' + ret if ret else ''}: {type(e).__name__}: {str(e)[:120]}")
            sleep(cfg["search"]["delay_seconds"])
            continue
        if leg:
            legs.append(leg)
            print(f"  {leg.key}: {leg.price:.0f} {leg.currency} ({leg.airlines})")
        sleep(cfg["search"]["delay_seconds"])

    if searches and failures == len(searches):
        print("Todas as pesquisas falharam (o Google pode estar a bloquear este IP).")
        return 1

    if not dry_run:
        append_history(legs, now, history_path)

    trips = build_trips(legs, cfg)
    for t in trips:
        how = f"ida e volta {t.out.to}" if t.ret is None else f"{t.out.to} / {t.ret.frm}"
        print(f"  viagem {t.key}: {t.total:.0f} ({how})")

    if summary:
        top = trips[: cfg["alerts"]["summary_top_n"]]
        if top:
            body = "📊 **Resumo diário – viagens mais baratas agora** (voos + comboio, por pessoa)\n\n"
            body += "\n\n".join(format_alert(t, cfg) for t in top)
            send_discord(body, dry_run)
    else:
        # várias datas costumam partilhar o mesmo voo barato: junta tudo numa mensagem
        hits = [t for t in trips if should_alert(t, state, cfg)]
        if hits:
            send_discord("🚨 **Preço abaixo do limite!**\n\n"
                         + "\n\n".join(format_alert(t, cfg) for t in hits), dry_run)
        for trip in hits:
            state.setdefault(trip.key, {})["last_alerted"] = trip.total

    for trip in trips:
        s = state.setdefault(trip.key, {})
        s["last_seen"] = trip.total
        s["best_seen"] = min(trip.total, s.get("best_seen", trip.total))
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
