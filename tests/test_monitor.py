import csv
import datetime as dt
import json

import pytest

import flight_monitor as fm
from engines import Offer, SweepResult

D1 = dt.date(2026, 12, 1)


def search(**kw):
    base = dict(origin="AMS", destination="GRU", airlines=("KL",), max_stops=0,
                start=D1, end=dt.date(2026, 12, 2), stay_days=15, step_days=1,
                currencies=("EUR", "BRL"))
    base.update(kw)
    return fm.build_search(**base)


def offer(cur, price, day=1):
    d = dt.date(2026, 12, day)
    return Offer(cur, d, d + dt.timedelta(days=15), float(price), airline="KL", stops=0,
                 out_dep_time="10:55")


@pytest.fixture
def fake_sweep(monkeypatch):
    def install(offers, failure=""):
        monkeypatch.setattr(fm, "sweep", lambda *a, **k: SweepResult(
            offers=list(offers), engine="google-calendar", failure=failure))
    return install


@pytest.fixture
def sent(monkeypatch):
    mails = []
    monkeypatch.setattr(fm, "send_email", lambda to, subject, body: mails.append(
        (to, subject, body)))
    return mails


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, codes", [
    ("KL", ("KL",)), ("kl, af", ("KL", "AF")), ("KL+AF", ("KL", "AF")),
    ("ANY", ()), ("", ()),
])
def test_parse_airlines(text, codes):
    assert fm.parse_airlines(text) == codes


@pytest.mark.parametrize("text, stops", [("0", 0), (2, 2), ("any", None), ("-1", None)])
def test_parse_stops(text, stops):
    assert fm.parse_stops(text) == stops


def test_settings_file_sets_defaults_and_cli_overrides(data_dir):
    fm.save_settings({**fm.DEFAULTS, "destination": "GIG", "max_stops": "any",
                      "engine": "google-scrape", "airline": "KL,AF"})
    args = fm.parse_args([])
    assert (args.destination, args.max_stops, args.engine, args.airline) == \
        ("GIG", None, "google-scrape", ("KL", "AF"))
    args = fm.parse_args(["--destination", "GRU", "--max-stops", "1", "--airline", "ANY"])
    assert (args.destination, args.max_stops, args.airline) == ("GRU", 1, ())


def test_unreadable_settings_fall_back_to_defaults(data_dir):
    data_dir.mkdir()
    fm.SETTINGS_JSON.write_text("{not json", encoding="utf-8")
    assert fm.load_settings() == fm.DEFAULTS


def test_unknown_engine_in_settings_falls_back_to_auto(data_dir):
    fm.save_settings({**fm.DEFAULTS, "engine": "amadeus"})
    assert fm.parse_args([]).engine == "auto"


@pytest.mark.parametrize("kw, msg", [
    (dict(origin="AM"), "origin"),
    (dict(destination="AMS"), "same airport"),
    (dict(start=dt.date(2027, 1, 1)), "after"),
    (dict(stay_days=0), "at least 1"),
])
def test_build_search_rejects_bad_input(kw, msg):
    with pytest.raises(ValueError, match=msg):
        search(**kw)


def test_set_env_value_replaces_and_appends(data_dir, monkeypatch):
    fm.ENV_FILE.write_text("# comment\nGMAIL_USER=a@b.c\nSERPAPI_KEY=old\n", encoding="utf-8")
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    monkeypatch.delenv("IGNAV_API_KEY", raising=False)
    fm.set_env_value("SERPAPI_KEY", "new")
    fm.set_env_value("IGNAV_API_KEY", "k")
    assert fm.ENV_FILE.read_text(encoding="utf-8").splitlines() == [
        "# comment", "GMAIL_USER=a@b.c", "SERPAPI_KEY=new", "IGNAV_API_KEY=k"]


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def test_run_records_classifies_and_reports(data_dir, fake_sweep, sent):
    fake_sweep([offer("EUR", 1100), offer("EUR", 995, day=2), offer("BRL", 5619)])
    r = fm.run_monitor(search(), recipient="me@x.y", send=True, record=True)

    assert r.best["EUR"].price == 995 and r.light == "LOW" and r.mode == "fixed"
    assert r.new_low
    with fm.HISTORY_CSV.open(encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 3
    with fm.RUNS_CSV.open(encoding="utf-8") as f:
        (run,) = list(csv.DictReader(f))
    assert (run["low_eur"], run["low_eur_depart"], run["low_brl"]) == \
        ("995.0", "2026-12-02", "5619.0")
    assert json.loads(fm.BEST_JSON.read_text(encoding="utf-8"))["EUR"]["price"] == 995

    (to, subject, body), = sent
    assert to == "me@x.y" and subject == "[LOW] AMS-GRU EUR 995 (out 2026-12-02)"
    assert "Engine: Google Flights - price calendar." in body
    assert "Cheapest BRL: R$ 5619" in body and "(dep 10:55, KL, direct)" in body


def test_run_without_record_writes_nothing(data_dir, fake_sweep, sent):
    fake_sweep([offer("EUR", 995)])
    r = fm.run_monitor(search(), send=False, record=False)
    assert r.light == "LOW" and sent == []
    assert not fm.HISTORY_CSV.exists() and not fm.RUNS_CSV.exists()
    assert not fm.BEST_JSON.exists()


def test_run_without_offers_warns(data_dir, fake_sweep, sent):
    fake_sweep([], failure="Hit Google consent wall")
    r = fm.run_monitor(search(), recipient="me@x.y", send=True)
    assert r.failure and not r.best
    (_, subject, body), = sent
    assert subject.startswith("[WARN]") and "consent wall" in body


def test_run_without_eur_has_no_light(data_dir, fake_sweep, sent):
    fake_sweep([offer("BRL", 5619)])
    r = fm.run_monitor(search(currencies=("BRL",)), send=True, record=True)
    assert r.light == "" and not fm.RUNS_CSV.exists()     # baseline is EUR-only
    assert sent[0][1] == "[INFO] AMS-GRU BRL 5619 (out 2026-12-01)"


def test_main_exit_codes(data_dir, fake_sweep, sent):
    fake_sweep([offer("EUR", 995)])
    assert fm.main(["--no-email"]) == 0
    fake_sweep([], failure="quota")
    assert fm.main(["--no-email"]) == 2
    assert fm.main(["--origin", "XX"]) == 1
