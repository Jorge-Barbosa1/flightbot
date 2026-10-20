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


TODAY = __import__("datetime").date(2026, 10, 20)


def test_parse_dates():
    assert fw.parse_day("23/04", TODAY) == "2027-04-23"          # próxima ocorrência
    assert fw.parse_day("23/10", TODAY) == "2026-10-23"
    assert fw.parse_day("1/5/27") == "2027-05-01"
    assert fw.parse_day("2027-04-23") == "2027-04-23"
    assert fw.parse_span("23/04", TODAY) == ["2027-04-23", "2027-04-23"]
    assert fw.parse_span("23/04 a 24/04", TODAY) == ["2027-04-23", "2027-04-24"]


def test_apply_request(cfg):
    r = fw.apply_request(cfg, "23/04", "01/05", 3, 17, today=TODAY)
    assert fw.date_pairs(r) == [("2027-04-23", "2027-05-01")]
    assert fw.earliest_hour(r, "2027-04-23") == 17 and fw.earliest_hour(r, "2027-04-24") == 0
    assert r["alerts"]["summary_top_n"] == 3
    assert cfg["dates"]["outbound"] == ["2027-04-25", "2027-04-26"]   # o original não muda


def test_request_other_destination_with_words(cfg):
    r = fw.apply_request(cfg, "tarde 23/10/2026", "early 25/10/2026", 3, None, destino="Madrid")
    assert r["airports"] == {"MAD": {"name": "MAD", "ground_cost": 0}}
    assert fw.date_pairs(r) == [("2026-10-23", "2026-10-25")]
    assert fw.earliest_hour(r, "2026-10-23") == fw.LATE_HOUR
    assert fw.latest_arrival(r, "2026-10-25") == fw.EARLY_HOUR
    assert r["roundtrip_airports"] == []          # com hora de chegada não dá para usar ida e volta
    assert [s[:2] for s in fw.build_searches(r)] == [("OPO", "MAD"), ("MAD", "OPO")]


def test_resolve_airports():
    assert fw.resolve_airports("Londres") == ["LHR", "LGW", "STN", "LTN"]
    assert fw.resolve_airports("mad, bcn") == ["MAD", "BCN"]
    with pytest.raises(ValueError):
        fw.resolve_airports("Atlântida")


def test_bad_request_dates(cfg):
    with pytest.raises(ValueError):
        fw.apply_request(cfg, "25/10/2026", "23/10/2026", 3, None, destino="MAD")


def test_arrives_by():
    class A:
        date, time = (2026, 10, 25), (14, 59)
    assert fw.arrives_by(A, "2026-10-25", 15)
    A.time = (15, 1)
    assert not fw.arrives_by(A, "2026-10-25", 15)
    A.date, A.time = (2026, 10, 26), (8, 0)                          # chega no dia seguinte
    assert not fw.arrives_by(A, "2026-10-25", 15)


def test_earliest_hour_from_yaml_date_keys(cfg):
    import datetime
    cfg["dates"]["earliest_departure"] = {datetime.date(2027, 4, 23): 17}   # o YAML lê datas assim
    assert fw.earliest_hour(cfg, "2027-04-23") == 17


def test_per_pair_gives_several_options(cfg):
    legs = [leg("OPO", "AMS", "2027-04-25", 100), leg("OPO", "NRN", "2027-04-25", 60),
            leg("AMS", "OPO", "2027-04-28", 70), leg("NRN", "OPO", "2027-04-28", 50)]
    assert len(fw.build_trips(legs, cfg)) == 1
    trips = fw.build_trips(legs, cfg, per_pair=3)
    assert [t.total for t in trips] == [160, 170, 170]


def test_request_sends_top_n_and_keeps_state(cfg, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(fw, "send_discord", lambda text, dry=False: sent.append(text))
    r = fw.apply_request(cfg, "25/04", "28/04", 2, None, today=TODAY)
    fetch = fake_fetch({("OPO", "NRN"): 60, ("OPO", "AMS"): 120, ("AMS", "OPO"): 70, ("NRN", "OPO"): 200})
    state = tmp_path / "s.json"
    fw.run(r, fetch=fetch, state_path=state, history_path=tmp_path / "h.csv",
           sleep=lambda s: None, request="teste")
    assert len(sent) == 1 and "Pedido: teste" in sent[0] and sent[0].count("✈️") == 2
    assert not state.exists()


def test_request_with_no_flights_says_so(cfg, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(fw, "send_discord", lambda text, dry=False: sent.append(text))
    fw.run(cfg, fetch=fake_fetch({}), state_path=tmp_path / "s.json", history_path=tmp_path / "h.csv",
           sleep=lambda s: None, request="vazio")
    assert "Não encontrei" in sent[0]


# ---------------------------------------------------------------- fins de semana / bagagem
def test_parse_when_weekends():
    # TODAY = terça 20 out 2026
    assert fw.parse_when("fins de semana de novembro", TODAY) == [
        ("2026-11-06", "2026-11-08"), ("2026-11-13", "2026-11-15"),
        ("2026-11-20", "2026-11-22"), ("2026-11-27", "2026-11-29")]
    assert fw.parse_when("este fim de semana", TODAY) == [("2026-10-23", "2026-10-25")]
    assert len(fw.parse_when("próximos 3 fins de semana", TODAY)) == 3
    assert fw.parse_when("fds de outubro", TODAY) == [("2026-10-23", "2026-10-25"), ("2026-10-30", "2026-11-01")]
    assert fw.parse_when("março", TODAY)[0][0].startswith("2027-03")     # mês já passado -> ano seguinte
    with pytest.raises(ValueError):
        fw.parse_when("quando calhar", TODAY)


def test_request_with_quando(cfg):
    r = fw.apply_request(cfg, None, None, None, None, destino="Roma", quando="novembro", today=TODAY)
    assert fw.date_pairs(r)[0] == ("2026-11-06", "2026-11-08")
    assert fw.earliest_hour(r, "2026-11-06") == fw.LATE_HOUR          # sexta à noite por defeito
    assert r["alerts"]["summary_top_n"] == 4 and r["request"]["per_pair"] == 1
    assert "fins de semana 6 nov, 13 nov" in fw.request_title(r, "Roma")
    with pytest.raises(ValueError):
        fw.apply_request(cfg, "23/10", None, None, None, destino="Roma", quando="novembro", today=TODAY)


def test_bagagem(cfg):
    r = fw.apply_request(cfg, "23/10", "25/10", None, None, destino="MAD", bagagem="cabine", today=TODAY)
    assert r["search"]["carry_on_bags"] == 1 and "mala de cabine" in fw.request_title(r, "MAD")
    assert fw.parse_bags(None) == 0 and fw.parse_bags("nenhuma") == 0
    with pytest.raises(ValueError):
        fw.parse_bags("porão")


# ---------------------------------------------------------------- compra já
def test_trend_rise_alerts_once_per_streak():
    st = {}
    msgs = [fw.trend_signal(st, v, "t", 5, "X") for v in [200, 205, 212, 220, 230]]
    assert msgs[:3] == [None, None, None]
    assert "a subir" in msgs[3] and "200 → 205 → 212 → 220" in msgs[3]
    assert msgs[4] is None                                    # mesma subida, não repete
    fw.trend_signal(st, 210, "t", 5, "X")                     # desce: volta a poder avisar
    assert st["rise_alerted"] is False


def test_trend_small_rise_is_noise():
    st = {}
    msgs = [fw.trend_signal(st, v, "t", 5, "X") for v in [237, 238, 239, 240]]
    assert msgs == [None] * 4


def test_trend_new_low():
    st = {}
    for v in [240, 241, 239, 242, 240, 238]:
        assert fw.trend_signal(st, v, "t", 5, "X") is None
    assert "mais baixo" in fw.trend_signal(st, 230, "t", 5, "X")
    assert fw.trend_signal(st, 228, "t", 5, "X") is None      # só 2 € abaixo do novo mínimo


# ---------------------------------------------------------------- vigilâncias
@pytest.fixture
def discord(monkeypatch):
    sent = []
    monkeypatch.setattr(fw, "send_discord", lambda text, dry=False: sent.append(text))
    return sent


def watch_paths(tmp_path):
    return {"state_path": tmp_path / "s.json", "watches_path": tmp_path / "w.json"}


def test_vigiar_list_parar(cfg, tmp_path, discord):
    paths = watch_paths(tmp_path)
    fetch = fake_fetch({("OPO", "MAD"): 60, ("MAD", "OPO"): 50})
    req = {"acao": "vigiar", "destino": "Madrid", "ida": "20/11", "volta": "22/11", "max": 100, "user": "42"}
    assert fw.handle_pedido(cfg, req, fetch=fetch, sleep=lambda s: None, today=TODAY, **paths) == 0
    assert "Vigilância #1 criada" in discord[-1] and "110 €/pessoa" in discord[-1]
    watches = fw.load_watches(paths["watches_path"])
    assert watches[0]["max"] == 100 and "alerted_at_creation" not in watches[0]   # 110 > 100

    fw.handle_pedido(cfg, {"acao": "vigilancias"}, today=TODAY, **paths)
    assert "#1" in discord[-1] and "<@42>" in discord[-1]

    fw.handle_pedido(cfg, {"acao": "parar", "id": 1, "user": "99"}, today=TODAY, **paths)
    assert "só essa pessoa" in discord[-1]
    fw.handle_pedido(cfg, {"acao": "parar", "id": 1, "user": "42"}, today=TODAY, **paths)
    assert "parada" in discord[-1] and fw.load_watches(paths["watches_path"]) == []


def test_run_watches_alerts_once(cfg, tmp_path, discord):
    paths = watch_paths(tmp_path)
    cheap = fake_fetch({("OPO", "MAD"): 40, ("MAD", "OPO"): 40})
    fw.handle_pedido(cfg, {"acao": "vigiar", "destino": "MAD", "ida": "20/11", "volta": "22/11",
                           "max": 100, "user": "42"},
                     fetch=fake_fetch({("OPO", "MAD"): 90, ("MAD", "OPO"): 90}),
                     sleep=lambda s: None, today=TODAY, **paths)
    discord.clear()
    fw.run_watches(cfg, fetch=cheap, sleep=lambda s: None, today=TODAY, **paths)
    assert len(discord) == 1 and "<@42>" in discord[0] and "abaixo de 100" in discord[0]
    fw.run_watches(cfg, fetch=cheap, sleep=lambda s: None, today=TODAY, **paths)
    assert len(discord) == 1                                   # mesmo preço: não repete
    fw.run_watches(cfg, summary=True, fetch=cheap, sleep=lambda s: None, today=TODAY, **paths)
    assert "👀 **Vigilâncias**" in discord[-1] and "agora 80 €" in discord[-1]


def test_watch_already_under_limit_not_realerted(cfg, tmp_path, discord):
    paths = watch_paths(tmp_path)
    fetch = fake_fetch({("OPO", "MAD"): 40, ("MAD", "OPO"): 40})
    fw.handle_pedido(cfg, {"acao": "vigiar", "destino": "MAD", "ida": "20/11", "volta": "22/11", "max": 100},
                     fetch=fetch, sleep=lambda s: None, today=TODAY, **paths)
    assert "já está abaixo" in discord[-1]
    discord.clear()
    fw.run_watches(cfg, fetch=fetch, sleep=lambda s: None, today=TODAY, **paths)
    assert discord == []                                       # já tinha sido mostrado ao criar


def test_expired_watches_are_ignored(cfg, tmp_path, discord):
    paths = watch_paths(tmp_path)
    fw.handle_pedido(cfg, {"acao": "vigiar", "destino": "MAD", "ida": "20/11", "volta": "22/11", "max": 100},
                     fetch=fake_fetch({}), sleep=lambda s: None, today=TODAY, **paths)
    later = __import__("datetime").date(2026, 12, 1)
    assert fw.active_watches(fw.load_watches(paths["watches_path"]), later) == []


def test_vigiar_needs_max(cfg, tmp_path, discord):
    fw.handle_pedido(cfg, {"acao": "vigiar", "destino": "MAD", "ida": "20/11", "volta": "22/11"},
                     today=TODAY, **watch_paths(tmp_path))
    assert discord[-1].startswith("❌")


def test_format_arrival_next_day():
    class A:
        date, time = (2026, 11, 16), (8, 10)
    assert fw.format_arrival(A, "2026-11-15") == "08:10 (+1 dia)"
    A.date = (2026, 11, 15)
    assert fw.format_arrival(A, "2026-11-15") == "08:10"
