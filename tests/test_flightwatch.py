import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import flightwatch as fw  # noqa: E402


@pytest.fixture
def cfg():
    return {
        "origin": "OPO",
        "destinations": ["AMS", "EIN"],
        "dates": {"outbound": ["2027-04-25", "2027-04-26"], "return": ["2027-04-28", "2027-04-29"],
                  "min_nights": 2, "max_nights": 4},
        "search": {"passengers": 2, "currency": "EUR", "max_stops": 1,
                   "carry_on_bags": 0, "checked_bags": 0, "delay_seconds": 0},
        "alerts": {"max_price_per_person": 90, "min_drop_to_realert": 5, "summary_top_n": 2},
        "max_queries_per_run": 40,
    }


def offer(price, dest="AMS", o="2027-04-26", r="2027-04-28"):
    return fw.Offer("OPO", dest, o, r, price, "Ryanair", 0, "EUR")


def test_daterange_inclusive():
    assert fw.daterange("2027-04-26", "2027-04-28") == ["2027-04-26", "2027-04-27", "2027-04-28"]


def test_combinations_respect_nights(cfg):
    combos = fw.build_combinations(cfg)
    # 2 destinos x (25->28:3, 25->29:4, 26->28:2, 26->29:3) = 8, todas dentro de 2..4 noites
    assert len(combos) == 8
    cfg["dates"]["min_nights"] = 3
    # 25->28, 25->29, 26->29 por destino
    assert len(fw.build_combinations(cfg)) == 6


def test_combinations_limit(cfg):
    cfg["max_queries_per_run"] = 3
    assert len(fw.build_combinations(cfg)) == 3


def test_should_alert_rules(cfg):
    assert not fw.should_alert(offer(95), {}, cfg)            # acima do limite
    assert fw.should_alert(offer(80), {}, cfg)                # novo e barato
    st = {offer(80).key: {"last_alerted": 80}}
    assert not fw.should_alert(offer(78), st, cfg)            # baixou menos de 5
    assert fw.should_alert(offer(75), st, cfg)                # baixou >= 5


def test_run_alerts_and_state(cfg, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(fw, "send_discord", lambda text, dry=False: sent.append(text))
    monkeypatch.setattr(fw.Offer, "google_flights_url", lambda self: "http://example/x")
    prices = {"AMS": 70, "EIN": 120}

    def fake_fetch(origin, dest, o, r, cfg):
        return offer(prices[dest], dest, o, r)

    state, hist = tmp_path / "state.json", tmp_path / "h.csv"
    rc = fw.run(cfg, fetch=fake_fetch, state_path=state, history_path=hist, sleep=lambda s: None)
    assert rc == 0
    assert sent and all("AMS" in m for m in sent)             # só AMS está abaixo de 90
    n_first = len(sent)
    assert hist.read_text().count("\n") == 1 + 8              # cabeçalho + 8 linhas

    # segunda corrida com os mesmos preços: sem novos alertas (anti-spam)
    fw.run(cfg, fetch=fake_fetch, state_path=state, history_path=hist, sleep=lambda s: None)
    assert len(sent) == n_first


def test_run_all_failures_returns_1(cfg, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("bloqueado")

    rc = fw.run(cfg, fetch=boom, state_path=tmp_path / "s.json",
                history_path=tmp_path / "h.csv", sleep=lambda s: None)
    assert rc == 1


def test_summary_sends_top_n(cfg, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(fw, "send_discord", lambda text, dry=False: sent.append(text))
    monkeypatch.setattr(fw.Offer, "google_flights_url", lambda self: "http://example/x")
    fw.run(cfg, summary=True, fetch=lambda o, d, a, b, c: offer(100 if d == "EIN" else 60, d, a, b),
           state_path=tmp_path / "s.json", history_path=tmp_path / "h.csv", sleep=lambda s: None)
    assert len(sent) == 1 and sent[0].count("✈️") == 2


def test_real_config_loads_and_builds():
    cfg = fw.load_config()
    assert fw.build_combinations(cfg)
