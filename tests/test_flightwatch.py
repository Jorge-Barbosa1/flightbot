import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import flightwatch as fw  # noqa: E402


@pytest.fixture
def cfg():
    return {
        "origin": "OPO",
        "airports": {"AMS": {"name": "Amesterdão", "ground_cost": 0},
                     "NRN": {"name": "Weeze", "ground_cost": 30, "ground_time": "~2h30"}},
        "dates": {"outbound": ["2027-04-25", "2027-04-26"], "return": ["2027-04-28", "2027-04-29"],
                  "min_nights": 2, "max_nights": 4},
        "search": {"passengers": 2, "currency": "EUR", "max_stops": 1,
                   "carry_on_bags": 0, "checked_bags": 0, "delay_seconds": 0},
        "alerts": {"max_price_per_person": 180, "min_drop_to_realert": 5, "summary_top_n": 2},
        "max_queries_per_run": 60,
    }


@pytest.fixture(autouse=True)
def no_urls(monkeypatch):
    monkeypatch.setattr(fw.Leg, "google_flights_url", lambda self: "http://example/x")


def leg(frm, to, day, price, airline="Ryanair", ret=None):
    return fw.Leg(frm, to, day, price, airline, 0, "EUR", ret)


def fake_fetch(prices):
    """prices: {(de, para): preço}; o mesmo preço para todos os dias."""
    def fetch(frm, to, day, ret, cfg):
        p = prices.get((frm, to, "rt") if ret else (frm, to))
        return leg(frm, to, day, p, ret=ret) if p is not None else None
    return fetch


def test_daterange_inclusive():
    assert fw.daterange("2027-04-26", "2027-04-28") == ["2027-04-26", "2027-04-27", "2027-04-28"]


def test_date_pairs_respect_nights(cfg):
    # 25->28:3, 25->29:4, 26->28:2, 26->29:3
    assert len(fw.date_pairs(cfg)) == 4
    cfg["dates"]["min_nights"] = 3
    assert fw.date_pairs(cfg) == [("2027-04-25", "2027-04-28"), ("2027-04-25", "2027-04-29"),
                                  ("2027-04-26", "2027-04-29")]


def test_searches_are_one_way_both_directions(cfg):
    s = fw.build_searches(cfg)
    # 2 dias de ida x 2 aeroportos + 2 dias de volta x 2 aeroportos
    assert len(s) == 8
    assert ("OPO", "NRN", "2027-04-25", None) in s and ("AMS", "OPO", "2027-04-29", None) in s
    cfg["roundtrip_airports"] = ["AMS"]
    s = fw.build_searches(cfg)
    assert len(s) == 8 + 4                                    # + 1 ida e volta por par de datas
    assert ("OPO", "AMS", "2027-04-25", "2027-04-28") in s
    cfg["max_queries_per_run"] = 3
    assert len(fw.build_searches(cfg)) == 3


def test_trips_mix_airports_and_add_ground_cost(cfg):
    legs = [
        leg("OPO", "AMS", "2027-04-25", 100),
        leg("OPO", "NRN", "2027-04-25", 60),      # 60 + 30 comboio = 90 < 100
        leg("AMS", "OPO", "2027-04-28", 70),      # 70 < 50 + 30
        leg("NRN", "OPO", "2027-04-28", 50),
    ]
    trips = fw.build_trips(legs, cfg)
    assert len(trips) == 1                        # só 25->28 tem ida e volta
    t = trips[0]
    assert (t.out.to, t.ret.frm) == ("NRN", "AMS")
    assert t.total == 60 + 30 + 70
    assert t.nights == 3


def test_roundtrip_wins_when_cheaper_or_equal(cfg):
    one_way = [leg("OPO", "AMS", "2027-04-25", 100), leg("AMS", "OPO", "2027-04-28", 80)]
    rt = leg("OPO", "AMS", "2027-04-25", 180, "KLM", ret="2027-04-28")
    t = fw.build_trips(one_way + [rt], cfg)[0]
    assert t.ret is None and t.total == 180 and t.ret_day == "2027-04-28"   # empate: ida e volta
    pricier_rt = leg("OPO", "AMS", "2027-04-25", 181, "KLM", ret="2027-04-28")
    t = fw.build_trips(one_way + [pricier_rt], cfg)[0]
    assert t.ret is not None and t.total == 180


def test_roundtrip_ground_cost_counts_both_ways(cfg):
    rt = leg("OPO", "NRN", "2027-04-25", 100, ret="2027-04-28")
    t = fw.build_trips([rt], cfg)[0]
    assert t.total == 100 + 2 * 30
    msg = fw.format_alert(t, cfg)
    assert "Ida e volta" in msg and "OPO ⇄ NRN" in msg and "cada sentido" in msg


def test_should_alert_rules(cfg):
    def trip(total):
        return fw.Trip(leg("OPO", "AMS", "2027-04-25", total - 50),
                       leg("AMS", "OPO", "2027-04-28", 50), 0, 0)

    assert not fw.should_alert(trip(190), {}, cfg)            # acima do limite
    assert fw.should_alert(trip(170), {}, cfg)                # novo e barato
    st = {trip(170).key: {"last_alerted": 170}}
    assert not fw.should_alert(trip(168), st, cfg)            # baixou menos de 5
    assert fw.should_alert(trip(165), st, cfg)                # baixou >= 5


def test_format_alert_shows_both_legs_and_train(cfg):
    t = fw.Trip(leg("OPO", "NRN", "2027-04-25", 60), leg("AMS", "OPO", "2027-04-28", 70, "Vueling"), 30, 0)
    msg = fw.format_alert(t, cfg)
    assert "25 abr → 28 abr" in msg and "3 noites" in msg
    assert "160 EUR/pessoa" in msg and "320 EUR para 2" in msg
    assert "Weeze" in msg and "comboio/autocarro ≈ 30 EUR · ~2h30" in msg
    assert msg.count("comboio") == 1                          # volta é de AMS, sem comboio


def test_run_alerts_once_and_state(cfg, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(fw, "send_discord", lambda text, dry=False: sent.append(text))
    fetch = fake_fetch({("OPO", "NRN"): 60, ("OPO", "AMS"): 120,
                        ("AMS", "OPO"): 70, ("NRN", "OPO"): 200})
    state, hist = tmp_path / "state.json", tmp_path / "h.csv"
    rc = fw.run(cfg, fetch=fetch, state_path=state, history_path=hist, sleep=lambda s: None)
    assert rc == 0
    assert len(sent) == 1 and sent[0].count("✈️") == 4        # 4 pares de datas, 160 cada
    assert hist.read_text(encoding="utf-8").count("\n") == 1 + 8

    # segunda corrida com os mesmos preços: sem novos alertas (anti-spam)
    fw.run(cfg, fetch=fetch, state_path=state, history_path=hist, sleep=lambda s: None)
    assert len(sent) == 1


def test_run_all_failures_returns_1(cfg, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("bloqueado")

    rc = fw.run(cfg, fetch=boom, state_path=tmp_path / "s.json",
                history_path=tmp_path / "h.csv", sleep=lambda s: None)
    assert rc == 1


def test_summary_sends_top_n(cfg, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(fw, "send_discord", lambda text, dry=False: sent.append(text))
    fetch = fake_fetch({("OPO", "AMS"): 200, ("AMS", "OPO"): 200})
    fw.run(cfg, summary=True, fetch=fetch, state_path=tmp_path / "s.json",
           history_path=tmp_path / "h.csv", sleep=lambda s: None)
    assert len(sent) == 1 and sent[0].count("✈️") == 2


def test_old_state_keys_are_dropped(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"OPO-AMS|2027-04-24|2027-04-28": {"last_seen": 1}, '
                 '"2027-04-24>2027-04-28": {"last_seen": 2}}', encoding="utf-8")
    assert list(fw.load_state(p)) == ["2027-04-24>2027-04-28"]


def test_real_config_loads_and_builds():
    cfg = fw.load_config()
    assert fw.build_searches(cfg)


def test_split_message_never_cuts_a_trip():
    trips = [f"✈️ viagem {i}\n" + "x" * 500 for i in range(5)]
    chunks = fw.split_message("📊 Resumo\n\n" + "\n\n".join(trips))
    assert all(len(c) <= 1900 for c in chunks)
    assert len(chunks) > 1
    for t in trips:                                           # cada viagem inteira numa só mensagem
        assert sum(t in c for c in chunks) == 1
    assert fw.split_message("y" * 4000) == ["y" * 1900, "y" * 1900, "y" * 200]
