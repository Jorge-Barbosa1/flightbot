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

Pedido à medida (manda os N melhores para essas datas, sem mexer nos alertas):
    python src/flightwatch.py --ida 23/04 --volta 01/05 --top 3 --hora 17
    python src/flightwatch.py --ida "23/04 a 24/04" --volta "30/04 a 01/05"
    python src/flightwatch.py --destino Madrid --ida "tarde 23/10" --volta "cedo 25/10"
    python src/flightwatch.py --destino Roma --quando "fins de semana de novembro" --bagagem cabine
    python src/flightwatch.py --pedido-json '{"acao": "flight", "destino": "MAD", ...}'  # vindo do Discord

Vigilâncias (criadas no Discord com /vigiar) ficam em data/watches.json e são
verificadas em cada corrida agendada, a seguir ao Koningsdag.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import re
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
WATCHES_PATH = ROOT / "data" / "watches.json"
MAX_WATCHES = 10

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
    depart: str = ""     # HH:MM de partida do primeiro voo
    arrive: str = ""     # HH:MM de chegada do último voo (só nos voos só de ida)

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
    """(ida, volta) respeitando o nº mínimo/máximo de noites, ou a lista
    explícita em `dates.pairs` (ex. fins de semana)."""
    if cfg["dates"].get("pairs"):
        return [tuple(p) for p in cfg["dates"]["pairs"]]
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


def parse_day(text: str, today: date | None = None) -> str:
    """'23/04', '23/04/2027' ou '2027-04-23' -> '2027-04-23'. Sem ano, usa a
    próxima vez que esse dia acontece (a contar de hoje)."""
    text = text.strip()
    if "-" in text:
        return date.fromisoformat(text).isoformat()
    parts = [int(x) for x in text.split("/")]
    d, m = parts[0], parts[1]
    if len(parts) > 2:
        y = parts[2]
        return date(y + 2000 if y < 100 else y, m, d).isoformat()
    today = today or date.today()
    candidate = date(today.year, m, d)
    return (candidate if candidate >= today else date(today.year + 1, m, d)).isoformat()


# palavras que se podem escrever junto às datas (ex. "tarde 23/10", "early 25/10")
LATE_WORDS = {"tarde", "late", "noite", "night", "evening"}
EARLY_WORDS = {"cedo", "early", "manha", "manhã", "morning"}
LATE_HOUR = 17       # "tarde" na ida = sair a partir das 17h
EARLY_HOUR = 15      # "cedo" na volta = chegar a casa até às 15h


def split_words(text: str) -> tuple[str, set[str]]:
    """Separa as palavras-chave ('tarde', 'cedo'...) das datas."""
    words, rest = set(), []
    for tok in text.split():
        if tok.lower() in LATE_WORDS | EARLY_WORDS:
            words.add(tok.lower())
        else:
            rest.append(tok)
    return " ".join(rest), words


def parse_span(text: str, today: date | None = None) -> list[str]:
    """'23/04' ou '23/04 a 24/04' -> [início, fim]."""
    parts = [x for x in text.replace(" até ", " a ").split(" a ") if x.strip()]
    days = [parse_day(x, today) for x in parts]
    return [days[0], days[-1]]


MONTH_WORDS = {
    "janeiro": 1, "jan": 1, "january": 1, "fevereiro": 2, "fev": 2, "february": 2, "feb": 2,
    "março": 3, "marco": 3, "march": 3, "abril": 4, "abr": 4, "april": 4, "apr": 4,
    "maio": 5, "mai": 5, "may": 5, "junho": 6, "jun": 6, "june": 6, "julho": 7, "jul": 7, "july": 7,
    "agosto": 8, "ago": 8, "august": 8, "aug": 8, "setembro": 9, "set": 9, "september": 9, "sep": 9,
    "outubro": 10, "out": 10, "october": 10, "oct": 10, "novembro": 11, "nov": 11, "november": 11,
    "dezembro": 12, "dez": 12, "december": 12, "dec": 12,
}
WEEKEND_WORDS = {"fim", "fins", "semana", "fds", "weekend", "weekends"}
MAX_WEEKENDS = 8


def parse_when(text: str, today: date | None = None) -> list[tuple[str, str]]:
    """Fins de semana (sexta -> domingo):
    'fins de semana de novembro', 'novembro 2027', 'próximos 4 fins de semana',
    'este fim de semana'."""
    today = today or date.today()
    words = re.findall(r"[a-zà-ú]+|\d+", text.lower())
    nums = [int(w) for w in words if w.isdigit()]
    month = next((MONTH_WORDS[w] for w in words if w in MONTH_WORDS), None)
    if month:
        year = next((n for n in nums if n > 2000),
                    today.year if month >= today.month else today.year + 1)
        first = date(year, month, 1)
        fridays = [first + timedelta(days=i) for i in range(31)
                   if (first + timedelta(days=i)).month == month and (first + timedelta(days=i)).weekday() == 4]
    elif nums or WEEKEND_WORDS & set(words):
        n = min(nums[0] if nums else 1, MAX_WEEKENDS)
        nxt = today + timedelta(days=(4 - today.weekday()) % 7)
        fridays = [nxt + timedelta(weeks=i) for i in range(n)]
    else:
        raise ValueError("Não percebi o 'quando'. Exemplos: 'fins de semana de novembro', "
                         "'próximos 4 fins de semana', 'este fim de semana'.")
    pairs = [(f.isoformat(), (f + timedelta(days=2)).isoformat()) for f in fridays if f >= today]
    if not pairs:
        raise ValueError("Esses fins de semana já passaram.")
    return pairs[:MAX_WEEKENDS]


def parse_bags(bagagem: str | None) -> int:
    """Malas de cabine a incluir no preço (o Google só sabe somar a de cabine)."""
    if not bagagem or str(bagagem).lower() in ("nenhuma", "não", "nao", "no", "false", "0"):
        return 0
    if str(bagagem).lower() in ("cabine", "sim", "yes", "true", "1", "carry-on"):
        return 1
    raise ValueError("bagagem: usa 'cabine' (mala de cabine incluída) ou deixa vazio.")


# cidades mais comuns -> aeroportos (também se pode escrever o código, ex. MAD)
CITY_AIRPORTS = {
    "madrid": ["MAD"], "barcelona": ["BCN"], "sevilha": ["SVQ"], "valencia": ["VLC"], "valência": ["VLC"],
    "malaga": ["AGP"], "málaga": ["AGP"], "palma": ["PMI"], "maiorca": ["PMI"], "ibiza": ["IBZ"],
    "bilbau": ["BIO"], "bilbao": ["BIO"], "tenerife": ["TFS", "TFN"], "lisboa": ["LIS"], "porto": ["OPO"],
    "faro": ["FAO"], "funchal": ["FNC"], "madeira": ["FNC"], "acores": ["PDL"], "açores": ["PDL"],
    "londres": ["LHR", "LGW", "STN", "LTN"], "london": ["LHR", "LGW", "STN", "LTN"],
    "paris": ["CDG", "ORY", "BVA"], "roma": ["FCO", "CIA"], "rome": ["FCO", "CIA"],
    "milao": ["MXP", "LIN", "BGY"], "milão": ["MXP", "LIN", "BGY"], "milan": ["MXP", "LIN", "BGY"],
    "veneza": ["VCE", "TSF"], "napoles": ["NAP"], "nápoles": ["NAP"], "bolonha": ["BLQ"],
    "amesterdao": ["AMS", "EIN"], "amesterdão": ["AMS", "EIN"], "amsterdam": ["AMS", "EIN"],
    "bruxelas": ["BRU", "CRL"], "brussels": ["BRU", "CRL"], "berlim": ["BER"], "berlin": ["BER"],
    "munique": ["MUC"], "frankfurt": ["FRA", "HHN"], "colonia": ["CGN"], "colónia": ["CGN"],
    "dusseldorf": ["DUS", "NRN"], "düsseldorf": ["DUS", "NRN"], "hamburgo": ["HAM"],
    "dublin": ["DUB"], "edimburgo": ["EDI"], "manchester": ["MAN"], "zurique": ["ZRH"],
    "genebra": ["GVA"], "basileia": ["BSL"], "viena": ["VIE"], "praga": ["PRG"], "budapeste": ["BUD"],
    "cracovia": ["KRK"], "cracóvia": ["KRK"], "varsovia": ["WAW", "WMI"], "varsóvia": ["WAW", "WMI"],
    "copenhaga": ["CPH"], "estocolmo": ["ARN"], "oslo": ["OSL"], "helsinquia": ["HEL"],
    "atenas": ["ATH"], "istambul": ["IST", "SAW"], "marraquexe": ["RAK"], "nice": ["NCE"],
    "lyon": ["LYS"], "marselha": ["MRS"], "toulouse": ["TLS"], "bordeus": ["BOD"], "nantes": ["NTE"],
    "luxemburgo": ["LUX"], "malta": ["MLA"],
}


# países -> aeroportos principais (os mais servidos a partir do Porto primeiro)
COUNTRY_AIRPORTS = {
    "holanda": ["AMS", "EIN", "RTM"], "países baixos": ["AMS", "EIN", "RTM"],
    "paises baixos": ["AMS", "EIN", "RTM"], "netherlands": ["AMS", "EIN", "RTM"],
    "espanha": ["MAD", "BCN", "AGP", "SVQ", "VLC", "PMI", "ALC", "BIO"],
    "spain": ["MAD", "BCN", "AGP", "SVQ", "VLC", "PMI", "ALC", "BIO"],
    "itália": ["FCO", "CIA", "MXP", "BGY", "VCE", "NAP", "BLQ", "PSA"],
    "italia": ["FCO", "CIA", "MXP", "BGY", "VCE", "NAP", "BLQ", "PSA"],
    "italy": ["FCO", "CIA", "MXP", "BGY", "VCE", "NAP", "BLQ", "PSA"],
    "frança": ["CDG", "ORY", "BVA", "LYS", "NCE", "MRS", "TLS", "BOD", "NTE"],
    "franca": ["CDG", "ORY", "BVA", "LYS", "NCE", "MRS", "TLS", "BOD", "NTE"],
    "france": ["CDG", "ORY", "BVA", "LYS", "NCE", "MRS", "TLS", "BOD", "NTE"],
    "reino unido": ["LHR", "LGW", "STN", "LTN", "MAN", "EDI", "BRS", "LPL"],
    "inglaterra": ["LHR", "LGW", "STN", "LTN", "MAN", "BRS", "LPL"],
    "uk": ["LHR", "LGW", "STN", "LTN", "MAN", "EDI", "BRS", "LPL"],
    "escócia": ["EDI", "GLA"], "escocia": ["EDI", "GLA"],
    "alemanha": ["BER", "MUC", "FRA", "DUS", "CGN", "HAM", "NRN", "STR"],
    "germany": ["BER", "MUC", "FRA", "DUS", "CGN", "HAM", "NRN", "STR"],
    "bélgica": ["BRU", "CRL"], "belgica": ["BRU", "CRL"], "belgium": ["BRU", "CRL"],
    "suíça": ["GVA", "ZRH", "BSL"], "suica": ["GVA", "ZRH", "BSL"], "switzerland": ["GVA", "ZRH", "BSL"],
    "irlanda": ["DUB"], "ireland": ["DUB"],
    "portugal": ["LIS", "FAO", "FNC", "PDL"],
    "polónia": ["KRK", "WAW", "WMI", "GDN", "WRO"], "polonia": ["KRK", "WAW", "WMI", "GDN", "WRO"],
    "grécia": ["ATH", "SKG"], "grecia": ["ATH", "SKG"], "greece": ["ATH", "SKG"],
    "áustria": ["VIE", "SZG"], "austria": ["VIE", "SZG"],
    "chéquia": ["PRG"], "chequia": ["PRG"], "república checa": ["PRG"], "republica checa": ["PRG"],
    "hungria": ["BUD"], "dinamarca": ["CPH", "BLL"], "suécia": ["ARN", "GOT"], "suecia": ["ARN", "GOT"],
    "noruega": ["OSL"], "croácia": ["ZAG", "SPU", "DBV"], "croacia": ["ZAG", "SPU", "DBV"],
    "marrocos": ["RAK", "CMN", "FEZ", "TNG"], "luxemburgo": ["LUX"], "malta": ["MLA"],
}


def resolve_airports(text: str) -> list[str]:
    """'Madrid' -> ['MAD'], 'MAD' -> ['MAD'], 'MAD,BCN' -> ['MAD', 'BCN'],
    'Holanda' -> todos os aeroportos principais do país."""
    codes: list[str] = []
    for chunk in text.split(","):
        key = " ".join(chunk.lower().split())
        if key in COUNTRY_AIRPORTS:
            codes += COUNTRY_AIRPORTS[key]
            continue
        for part in chunk.replace("/", " ").split():
            if part.lower() in COUNTRY_AIRPORTS:
                codes += COUNTRY_AIRPORTS[part.lower()]
            elif part.lower() in CITY_AIRPORTS:
                codes += CITY_AIRPORTS[part.lower()]
            elif len(part) == 3 and part.isalpha():
                codes.append(part.upper())
            else:
                raise ValueError(f"Não conheço '{part}'. Usa uma cidade, um país ou o código do "
                                 "aeroporto (ex. Madrid, Holanda, MAD).")
    if not codes:
        raise ValueError("Falta o destino.")
    return list(dict.fromkeys(codes))


def is_country(destino: str | None) -> bool:
    return bool(destino) and " ".join(destino.lower().split()) in COUNTRY_AIRPORTS


def apply_request(cfg: dict, ida: str | None, volta: str | None, top: int | None, hora: int | None,
                  destino: str | None = None, chegar_ate: int | None = None,
                  origem: str | None = None, today: date | None = None,
                  quando: str | None = None, bagagem: str | None = None) -> dict:
    """Config para um pedido à medida: só essas datas, top N, e opcionalmente
    outro destino/origem, hora mínima de partida na ida e hora máxima de
    chegada a casa na volta. 'tarde' na ida e 'cedo' na volta também valem.

    Com `quando` (ex. 'fins de semana de novembro') testa cada fim de semana,
    sexta a partir das 17h (ou `hora`) -> domingo, e mostra o melhor de cada."""
    if quando:
        if ida or volta:
            raise ValueError("Usa 'quando' ou 'ida'/'volta', não os dois.")
        pairs = parse_when(quando, today)
        hora = LATE_HOUR if hora is None else hora
        out_days, ret_days = [o for o, _ in pairs], [r for _, r in pairs]
        dates = {"pairs": [list(x) for x in pairs], "outbound": [out_days[0], out_days[-1]],
                 "return": [ret_days[0], ret_days[-1]], "min_nights": 0, "max_nights": 60}
        top = top or len(pairs)
        per_pair = 1
    else:
        if not (ida and volta):
            raise ValueError("Faltam as datas de ida e de volta (ou usa 'quando', "
                             "ex. 'fins de semana de novembro').")
        ida, ida_words = split_words(ida)
        volta, volta_words = split_words(volta)
        if hora is None and ida_words & LATE_WORDS:
            hora = LATE_HOUR
        if chegar_ate is None and volta_words & EARLY_WORDS:
            chegar_ate = EARLY_HOUR
        out, ret = parse_span(ida, today), parse_span(volta, today)
        if ret[-1] < out[0]:
            raise ValueError("A volta é antes da ida.")
        out_days, ret_days = daterange(*out), daterange(*ret)
        dates = {"outbound": out, "return": ret, "min_nights": 0, "max_nights": 60}
        top = top or 3
        per_pair = top
    dates["earliest_departure"] = {d: hora for d in out_days} if hora is not None else {}
    dates["latest_arrival"] = {d: chegar_ate for d in ret_days} if chegar_ate is not None else {}

    new = {**cfg}
    if origem:
        new["origin"] = resolve_airports(origem)[0]
    if destino:
        codes = resolve_airports(destino)
        new["airports"] = {c: {"name": c, "ground_cost": 0} for c in codes}
        new["roundtrip_airports"] = codes[:3]
    if chegar_ate is not None:
        # nos bilhetes de ida e volta o Google só diz a hora da ida: não dá para filtrar a volta
        new["roundtrip_airports"] = []
    new["dates"] = dates
    new["alerts"] = {**cfg["alerts"], "summary_top_n": top}
    new["search"] = {**cfg["search"], "options_per_search": per_pair, "carry_on_bags": parse_bags(bagagem)}
    new["max_queries_per_run"] = None
    new["request"] = {"per_pair": per_pair, "destino": destino}
    return new


def _hour_rule(cfg: dict, name: str, day: str) -> int | None:
    rules = {str(k): v for k, v in (cfg["dates"].get(name) or {}).items()}
    return None if rules.get(day) is None else int(rules[day])


def earliest_hour(cfg: dict, day: str) -> int:
    """Hora mínima de partida na ida para esse dia (0 = qualquer hora)."""
    return _hour_rule(cfg, "earliest_departure", day) or 0


def latest_arrival(cfg: dict, day: str) -> int | None:
    """Hora máxima de chegada a casa na volta para esse dia (None = qualquer)."""
    return _hour_rule(cfg, "latest_arrival", day)


def ground_cost(cfg: dict, airport: str) -> float:
    return float(cfg["airports"][airport].get("ground_cost", 0))


def build_trips(legs: list[Leg], cfg: dict, per_pair: int = 1) -> list[Trip]:
    """As `per_pair` viagens mais baratas (voos + comboio) para cada par de
    datas válido, todas juntas e ordenadas por preço.

    A ida e a volta são escolhidas de forma independente, por isso podem ser
    de aeroportos e companhias diferentes. Um bilhete de ida e volta na mesma
    companhia ganha se custar o mesmo ou menos.
    """
    origin = cfg["origin"]
    outs: dict[str, list[Leg]] = {}
    rets: dict[str, list[Leg]] = {}
    rts: dict[tuple[str, str], list[Leg]] = {}
    for leg in legs:
        if leg.ret_day:
            rts.setdefault((leg.day, leg.ret_day), []).append(leg)
        elif leg.frm == origin:
            outs.setdefault(leg.day, []).append(leg)
        else:
            rets.setdefault(leg.day, []).append(leg)

    trips = []
    for o, r in date_pairs(cfg):
        options = []
        for rt in rts.get((o, r), []):
            g = ground_cost(cfg, rt.to)
            options.append(Trip(rt, None, g, g))
        for out, ret in itertools.product(outs.get(o, []), rets.get(r, [])):
            options.append(Trip(out, ret, ground_cost(cfg, out.to), ground_cost(cfg, ret.frm)))
        # sort estável: em caso de empate fica o de ida e volta (vem primeiro)
        trips += sorted(options, key=lambda t: t.total)[:per_pair]
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


def parse_flights(html: str):
    """Como o `fast_flights.parser.parse`, mas lê as duas listas do Google.

    A página traz os "melhores voos" (payload[2]) e os "outros voos"
    (payload[3]); o fast-flights 3.1 só lê a segunda e perdia, por exemplo, o
    direto da Transavia OPO-AMS que o Google mostra no topo. Juntamos as duas
    antes de usar o parser da biblioteca.
    """
    from fast_flights.parser import parse_js
    from selectolax.lexbor import LexborHTMLParser

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("a página não tem resultados (bloqueio ou página de consentimento?)")
    js = script.text()
    head, rest = js.split("data:", 1)
    data, tail = rest.rsplit(",", 1)
    try:
        payload = json.loads(data)
        best = (payload[2] or [None])[0] or []
        other = (payload[3] or [None])[0] or []
    except (ValueError, IndexError, TypeError):
        return parse_js(js)       # formato inesperado: deixa o parser original decidir
    if not best:
        return parse_js(js)
    if payload[3]:
        payload[3][0] = best + other
    else:
        payload[3] = [best + other]
    return parse_js(f"{head}data:{json.dumps(payload)},{tail}")


def fetch_cheapest(frm: str, to: str, day: str, ret: str | None, cfg: dict) -> list[Leg]:
    """Pesquisa no Google Flights (via fast-flights) e devolve os voos mais
    baratos (`search.options_per_search`, por defeito só 1): só ida, ou ida e
    volta se `ret` estiver preenchido.

    Para trocar de fornecedor (ex.: SerpApi), basta substituir esta função por
    outra que devolva um Leg ou None. O resto do código não muda.
    """
    from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query

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
        results = parse_flights(_consent_fetcher().fetch_html(q))
    except FlightsNotFound:
        return []
    except (TypeError, IndexError):
        # o parser rebenta assim quando a página vem sem voos (ex.: a Ryanair
        # não voa nesse dia, ou não há rota). Um bloqueio do Google dá outro erro e é contado.
        return []

    priced = [f for f in results if getattr(f, "price", 0) and f.price > 0]
    if frm == cfg["origin"] and (min_hour := earliest_hour(cfg, day)):
        priced = [f for f in priced if f.flights and f.flights[0].departure.time[0] >= min_hour]
    if frm != cfg["origin"] and (max_hour := latest_arrival(cfg, day)) is not None:
        priced = [f for f in priced if f.flights and arrives_by(f.flights[-1].arrival, day, max_hour)]
    legs: list[Leg] = []
    for f in sorted(priced, key=lambda f: f.price):
        leg = Leg(
            frm=frm, to=to, day=day,
            price=float(f.price),
            airlines=", ".join(f.airlines),
            stops=max(len(f.flights) - 1, 0),
            currency=s["currency"],
            ret_day=ret,
            depart="%02d:%02d" % tuple(f.flights[0].departure.time) if f.flights else "",
            arrive="" if ret or not f.flights else format_arrival(f.flights[-1].arrival, day),
        )
        if leg not in legs:          # o Google às vezes repete o mesmo voo
            legs.append(leg)
        if len(legs) >= s.get("options_per_search", 1):
            break
    return legs


def format_arrival(arrival, day: str) -> str:
    """'13:30', ou '08:10 (+1 dia)' se só chega no dia seguinte."""
    hhmm = "%02d:%02d" % tuple(arrival.time)
    extra = (date(*arrival.date) - date.fromisoformat(day)).days
    return f"{hhmm} (+{extra} dia{'s' if extra > 1 else ''})" if extra > 0 else hhmm


def arrives_by(arrival, day: str, max_hour: int) -> bool:
    """Chega no próprio dia até às `max_hour`:00?"""
    y, m, d = arrival.date
    h, mi = arrival.time
    return date(y, m, d).isoformat() == day and (h, mi) <= (max_hour, 0)


# ---------------------------------------------------------------- estado
def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        # descarta chaves do formato antigo (ida+volta por rota)
        return {k: v for k, v in state.items() if ">" in k or k.startswith(("_", "watch:"))}
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
    name = names.get(other, {}).get("name", other)
    where = f" ({name})" if name != other else ""
    stops = "direto" if leg.stops == 0 else f"{leg.stops} escala(s)"
    if leg.depart:
        stops += f" · sai {leg.depart}"
    if leg.arrive and leg.frm != cfg["origin"]:
        stops += f", chega {leg.arrive}"
    if leg.ret_day:
        line = (f"**{label}** {fmt_day(leg.day)} → {fmt_day(leg.ret_day)}: {leg.frm} ⇄ {leg.to}{where} · "
                f"{leg.price:.0f} {leg.currency} · {leg.airlines} · "
                f"{stops} na ida")
    else:
        line = (f"**{label}** {fmt_day(leg.day)}: {leg.frm} → {leg.to}{where} · "
                f"{leg.price:.0f} {leg.currency} · {leg.airlines} · {stops}")
    # link mascarado; os <> evitam a pré-visualização gigante do Discord
    line += f" · [ver no Google Flights](<{leg.google_flights_url()}>)"
    if ground:
        each_way = " (cada sentido)" if leg.ret_day else ""
        line += (f"\n  + comboio/autocarro{each_way} ≈ {ground:.0f} {leg.currency} · "
                 f"{names.get(other, {}).get('ground_time', '')}")
    return line


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


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Parte o texto em mensagens até `limit` caracteres (o Discord aceita 2000),
    cortando só entre parágrafos para não partir uma viagem ao meio."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # parágrafo sozinho maior que o limite: último recurso, corta à bruta
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        current = para
    if current:
        chunks.append(current)
    return chunks


def send_discord(text: str, dry_run: bool = False) -> None:
    if dry_run:
        print("[dry-run] Discord:\n" + text + "\n")
        return
    # DISCORD_REPLY_URL: resposta a um comando /flight (vai para quem perguntou)
    url = os.environ.get("DISCORD_REPLY_URL") or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise RuntimeError("Falta a variável de ambiente DISCORD_WEBHOOK_URL")
    for chunk in split_message(text):
        r = requests.post(url, json={"content": chunk}, timeout=15)
        r.raise_for_status()
        time.sleep(0.5)


# ---------------------------------------------------------------- execução
def run(cfg: dict, dry_run: bool = False, summary: bool = False,
        fetch=fetch_cheapest, state_path: Path = STATE_PATH,
        history_path: Path = HISTORY_PATH, sleep=time.sleep,
        request: str | None = None) -> int:
    """`request`: título de um pedido à medida. Manda o top N para o Discord
    (mesmo que não haja nada) e não mexe no estado dos alertas."""
    state = load_state(state_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    legs, all_failed = search_all(cfg, fetch, sleep)
    if all_failed:
        print("Todas as pesquisas falharam (o Google pode estar a bloquear este IP).")
        return 1

    if not dry_run and not request:  # o histórico é só da vigilância do Koningsdag
        append_history(legs, now, history_path)

    per_pair = cfg.get("request", {}).get("per_pair", cfg["alerts"]["summary_top_n"]) if request else 1
    trips = build_trips(legs, cfg, per_pair=per_pair)
    for t in trips:
        how = f"ida e volta {t.out.to}" if t.ret is None else f"{t.out.to} / {t.ret.frm}"
        print(f"  viagem {t.key}: {t.total:.0f} ({how})")

    if request:
        top = trips[: cfg["alerts"]["summary_top_n"]]
        extra = " (voos + comboio, por pessoa)" if any(ground_cost(cfg, a) for a in cfg["airports"]) \
            else " (por pessoa)"
        body = f"🔎 **Pedido: {request}**{extra}\n\n"
        body += "\n\n".join(format_alert(t, cfg) for t in top) if top else "Não encontrei voos para essas datas."
        send_discord(body, dry_run)
        return 0

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

    if trips:
        signal = trend_signal(state.setdefault("_trend", {}), trips[0].total, now,
                              cfg["alerts"]["min_drop_to_realert"], "Koningsdag")
        if signal:
            send_discord(signal + "\n\n" + format_alert(trips[0], cfg), dry_run)

    for trip in trips:
        s = state.setdefault(trip.key, {})
        s["last_seen"] = trip.total
        s["best_seen"] = min(trip.total, s.get("best_seen", trip.total))
        s["updated"] = now

    if not dry_run:
        save_state(state, state_path)
    return 0


def search_all(cfg: dict, fetch=fetch_cheapest, sleep=time.sleep) -> tuple[list[Leg], bool]:
    """Corre todas as pesquisas do cfg. Devolve (voos, todas_falharam)."""
    searches = build_searches(cfg)
    print(f"{len(searches)} pesquisas a fazer")
    legs: list[Leg] = []
    failures = 0
    for frm, to, day, ret in searches:
        try:
            found = fetch(frm, to, day, ret, cfg)
        except Exception as e:  # noqa: BLE001 - não queremos parar tudo por uma pesquisa
            failures += 1
            print(f"  ! {frm}-{to} {day}{'->' + ret if ret else ''}: {type(e).__name__}: {str(e)[:120]}")
            sleep(cfg["search"]["delay_seconds"])
            continue
        found = found if isinstance(found, list) else [found] if found else []
        for leg in found:
            legs.append(leg)
            print(f"  {leg.key}: {leg.price:.0f} {leg.currency} ({leg.airlines})")
        sleep(cfg["search"]["delay_seconds"])
    return legs, bool(searches) and failures == len(searches)


# ---------------------------------------------------------------- compra já
def trend_signal(tstate: dict, total: float, now: str, min_drop: float, label: str) -> str | None:
    """Guarda o melhor preço desta corrida e diz se é altura de agir:
    📈 3 subidas seguidas (pelo menos +10 € ou +5% no total), uma vez por subida;
    📉 novo mínimo desde que se começou a vigiar (com pelo menos 6 medições)."""
    series = tstate.setdefault("series", [])
    prev = [v for _, v in series]
    value = round(total)
    series.append([now, value])
    del series[:-300]
    vals = prev + [value]
    if len(vals) >= 2 and vals[-1] < vals[-2]:
        tstate["rise_alerted"] = False
    if (len(vals) >= 4 and vals[-4] < vals[-3] < vals[-2] < vals[-1]
            and vals[-1] - vals[-4] >= max(10, 0.05 * vals[-4]) and not tstate.get("rise_alerted")):
        tstate["rise_alerted"] = True
        return (f"📈 **{label}: o preço está a subir** – {' → '.join(map(str, vals[-4:]))} €/pessoa. "
                "Se gostas de alguma opção, pode ser altura de comprar.")
    if len(prev) >= 6 and value <= min(prev) - min_drop:
        return (f"📉 **{label}: o preço mais baixo desde que comecei a vigiar** – {value} €/pessoa "
                f"(o mínimo era {min(prev)} €).")
    return None


# ---------------------------------------------------------------- vigilâncias
def load_watches(path: Path = WATCHES_PATH) -> list[dict]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return []


def save_watches(watches: list[dict], path: Path = WATCHES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(watches, indent=2, ensure_ascii=False), encoding="utf-8")


def active_watches(watches: list[dict], today: date | None = None) -> list[dict]:
    """As que ainda têm datas de ida no futuro."""
    today = (today or date.today()).isoformat()
    return [w for w in watches if max(o for o, _ in date_pairs({"dates": w["dates"]})) >= today]


def watch_cfg(cfg: dict, w: dict) -> dict:
    return {
        **cfg,
        "origin": w["origin"],
        "airports": {a: {"name": a, "ground_cost": 0} for a in w["airports"]},
        "roundtrip_airports": w["roundtrip"],
        "dates": w["dates"],
        "search": {**cfg["search"], "carry_on_bags": w.get("carry_on_bags", 0), "options_per_search": 1},
        "alerts": {**cfg["alerts"], "max_price_per_person": w["max"]},
        "max_queries_per_run": None,
    }


def check_watch(cfg: dict, w: dict, wstate: dict, now: str, fetch=fetch_cheapest,
                sleep=time.sleep) -> tuple[list[str], Trip | None]:
    """Pesquisa uma vigilância. Devolve as mensagens a enviar e a melhor viagem."""
    wc = watch_cfg(cfg, w)
    legs, _ = search_all(wc, fetch, sleep)
    trips = build_trips(legs, wc)
    if not trips:
        return [], None
    best = trips[0]
    min_drop = cfg["alerts"]["min_drop_to_realert"]
    msgs = []
    under = [t for t in trips if t.total <= w["max"]]
    last = wstate.get("last_alerted")
    if under and (last is None or best.total <= last - min_drop):
        who = f"<@{w['user']}> " if w.get("user") else ""
        msgs.append(f"🔔 {who}**Vigilância #{w['id']}** ({w['label']}): abaixo de {w['max']} €/pessoa!\n\n"
                    + "\n\n".join(format_alert(t, wc) for t in under[:3]))
        wstate["last_alerted"] = best.total
    signal = trend_signal(wstate.setdefault("trend", {}), best.total, now, min_drop, f"Vigilância #{w['id']}")
    if signal:
        msgs.append(signal + "\n\n" + format_alert(best, wc))
    wstate["last_seen"] = best.total
    wstate["best_seen"] = min(best.total, wstate.get("best_seen", best.total))
    wstate["updated"] = now
    return msgs, best


def run_watches(cfg: dict, dry_run: bool = False, summary: bool = False, fetch=fetch_cheapest,
                state_path: Path = STATE_PATH, watches_path: Path = WATCHES_PATH,
                sleep=time.sleep, today: date | None = None) -> int:
    """Corrida agendada: verifica cada vigilância ativa (alertas no canal) e,
    no resumo diário, junta uma linha por vigilância."""
    watches = active_watches(load_watches(watches_path), today)
    if not watches:
        return 0
    state = load_state(state_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = []
    for w in watches:
        print(f"Vigilância #{w['id']}: {w['label']}")
        wstate = state.setdefault(f"watch:{w['id']}", {})
        if wstate.get("last_alerted") is None and w.get("alerted_at_creation") is not None:
            wstate["last_alerted"] = w["alerted_at_creation"]   # já foi mostrado ao criar
        msgs, best = check_watch(cfg, w, wstate, now, fetch, sleep)
        for m in msgs:
            send_discord(m, dry_run)
        now_txt = f"{best.total:.0f} €" if best else "sem voos"
        lines.append(f"**#{w['id']}** {w['label']} · limite {w['max']} € · agora {now_txt}")
    if summary and lines:
        send_discord("👀 **Vigilâncias**\n" + "\n".join(lines), dry_run)
    if not dry_run:
        save_state(state, state_path)
    return 0


def handle_pedido(cfg: dict, req: dict, dry_run: bool = False, fetch=fetch_cheapest,
                  sleep=time.sleep, state_path: Path = STATE_PATH,
                  watches_path: Path = WATCHES_PATH, today: date | None = None) -> int:
    """Pedidos vindos do Discord: /flight, /vigiar, /vigilancias e /parar.
    A resposta vai para quem perguntou (DISCORD_REPLY_URL)."""
    acao = req.get("acao") or "flight"
    user = req.get("user")

    def num(key: str) -> int | None:
        return None if req.get(key) in (None, "") else int(req[key])

    try:
        if acao == "vigilancias":
            watches = active_watches(load_watches(watches_path), today)
            if not watches:
                send_discord("📋 Não há vigilâncias ativas. Cria uma com `/vigiar`.", dry_run)
                return 0
            state = load_state(state_path)
            lines = []
            for w in watches:
                seen = state.get(f"watch:{w['id']}", {}).get("last_seen")
                now_txt = f" · agora {seen:.0f} €" if seen is not None else ""
                who = f" · <@{w['user']}>" if w.get("user") else ""
                lines.append(f"**#{w['id']}** {w['label']} · limite {w['max']} €{now_txt}{who}")
            send_discord("📋 **Vigilâncias ativas**\n" + "\n".join(lines)
                         + "\n\nPara parar uma: `/parar id:N`", dry_run)
            return 0

        if acao == "parar":
            wid = num("id")
            watches = load_watches(watches_path)
            w = next((x for x in watches if x["id"] == wid), None)
            if w is None:
                raise ValueError(f"Não há nenhuma vigilância #{wid}. Vê a lista com `/vigilancias`.")
            if w.get("user") and user and w["user"] != user:
                raise ValueError(f"A vigilância #{wid} é de <@{w['user']}>; só essa pessoa a pode parar.")
            keep = [x for x in active_watches(watches, today) if x["id"] != wid]
            if not dry_run:
                save_watches(keep, watches_path)
            send_discord(f"🛑 Vigilância #{wid} parada ({w['label']}).", dry_run)
            return 0

        rc = apply_request(cfg, req.get("ida"), req.get("volta"), num("top"), num("sair_depois"),
                           destino=req.get("destino"), chegar_ate=num("chegar_ate"),
                           origem=req.get("origem"), today=today, quando=req.get("quando"),
                           bagagem=req.get("bagagem"))

        if acao == "flight":
            return run(rc, dry_run=dry_run, fetch=fetch, sleep=sleep, state_path=state_path,
                       request=request_title(rc, req.get("destino")))

        if acao == "vigiar":
            limit = num("max")
            if not limit or limit <= 0:
                raise ValueError("Falta o preço máximo por pessoa (max).")
            watches = active_watches(load_watches(watches_path), today)
            if len(watches) >= MAX_WATCHES:
                raise ValueError(f"Já há {MAX_WATCHES} vigilâncias ativas. Para uma com `/parar` primeiro.")
            w = {
                "id": max((x["id"] for x in load_watches(watches_path)), default=0) + 1,
                "user": user,
                "label": request_title(rc, req.get("destino"), with_top=False),
                "max": limit,
                "origin": rc["origin"],
                "airports": list(rc["airports"]),
                "roundtrip": rc["roundtrip_airports"],
                "dates": rc["dates"],
                "carry_on_bags": rc["search"]["carry_on_bags"],
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            legs, _ = search_all(watch_cfg(cfg, w), fetch, sleep)
            trips = build_trips(legs, watch_cfg(cfg, w))
            best = trips[0] if trips else None
            if best and best.total <= limit:
                w["alerted_at_creation"] = best.total
            if not dry_run:
                save_watches(watches + [w], watches_path)
            body = (f"👀 **Vigilância #{w['id']} criada**: {w['label']}, até {limit} €/pessoa.\n"
                    "Vou ver 3 vezes por dia e aviso no canal quando baixar do limite "
                    "(e se o preço começar a subir).")
            if best:
                state_txt = "✅ já está abaixo do limite!" if best.total <= limit else "ainda acima do limite."
                body += f"\n\nAgora o melhor está a **{best.total:.0f} €/pessoa**, {state_txt}\n\n"
                body += format_alert(best, watch_cfg(cfg, w))
            else:
                body += "\n\nPara já não encontrei voos para essas datas."
            send_discord(body, dry_run)
            return 0

        raise ValueError(f"Ação desconhecida: {acao}")
    except ValueError as e:
        # erro de quem pediu (datas, destino...): responde, mas o workflow não falha
        send_discord(f"❌ {e}", dry_run)
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
    p.add_argument("--ida", help="pedido à medida: dia ou intervalo de ida, ex. '23/04' ou '23/04 a 24/04'")
    p.add_argument("--volta", help="pedido à medida: dia ou intervalo de volta")
    p.add_argument("--top", type=int, help="pedido à medida: quantas viagens mostrar (por defeito 3)")
    p.add_argument("--hora", type=int, help="pedido à medida: só idas a partir desta hora (ex. 17)")
    p.add_argument("--chegar-ate", type=int, help="pedido à medida: voltas a chegar a casa até esta hora")
    p.add_argument("--destino", help="pedido à medida: cidade ou código(s), ex. Madrid ou MAD,BCN")
    p.add_argument("--origem", help="pedido à medida: aeroporto de partida (por defeito o do config)")
    p.add_argument("--quando", help="pedido à medida: ex. 'fins de semana de novembro'")
    p.add_argument("--bagagem", help="pedido à medida: 'cabine' para incluir mala de cabine")
    p.add_argument("--pedido-json", help="pedido vindo do Discord (/flight, /vigiar...), em JSON")
    args = p.parse_args()

    if args.test_discord:
        send_discord("✅ Koningsdag flight watch: ligação ao Discord a funcionar.")
        return 0
    cfg = load_config(Path(args.config))

    if args.pedido_json:
        req = json.loads(args.pedido_json)
        if req.get("app_id") and req.get("token"):
            os.environ["DISCORD_REPLY_URL"] = (
                f"https://discord.com/api/v10/webhooks/{req['app_id']}/{req['token']}")
        return handle_pedido(cfg, req, dry_run=args.dry_run)

    if args.ida or args.volta or args.destino or args.quando:
        req = {"acao": "flight", "ida": args.ida, "volta": args.volta, "destino": args.destino,
               "origem": args.origem, "top": args.top, "sair_depois": args.hora,
               "chegar_ate": args.chegar_ate, "quando": args.quando, "bagagem": args.bagagem}
        return handle_pedido(cfg, req, dry_run=args.dry_run)

    rc = run(cfg, dry_run=args.dry_run, summary=args.summary)
    return run_watches(cfg, dry_run=args.dry_run, summary=args.summary) or rc


def request_title(cfg: dict, destino: str | None, with_top: bool = True) -> str:
    d = cfg["dates"]

    def span(a: str, b: str) -> str:
        return fmt_day(a) if a == b else f"{fmt_day(a)} a {fmt_day(b)}"

    codes = ", ".join(cfg["airports"])
    if not destino:
        where = "Koningsdag"
    elif is_country(destino):
        where = f"{cfg['origin']} ⇄ {destino.strip().title()} ({codes})"
    else:
        where = f"{cfg['origin']} ⇄ {codes}"
    if d.get("pairs"):
        when = "fins de semana " + ", ".join(fmt_day(o) for o, _ in d["pairs"]) + " (sex → dom)"
    else:
        when = f"{span(*d['outbound'])} → {span(*d['return'])}"
    title = f"{where}, {when}"
    if with_top:
        title += f", top {cfg['alerts']['summary_top_n']}"
    if cfg["search"].get("carry_on_bags"):
        title += ", com mala de cabine"
    hours = {v for v in d.get("earliest_departure", {}).values()}
    if hours:
        title += f", ida a partir das {hours.pop()}h"
    hours = {v for v in d.get("latest_arrival", {}).values()}
    if hours:
        title += f", volta a chegar até às {hours.pop()}h"
    return title


if __name__ == "__main__":
    sys.exit(main())
