import datetime as dt

import pytest

import engines
from engines import (
    AUTO,
    EngineError,
    IgnavEngine,
    Offer,
    Search,
    SerpApiEngine,
    TravelpayoutsEngine,
    sweep,
)

D1 = dt.date(2026, 12, 1)
R1 = dt.date(2026, 12, 16)


def make_search(**kw):
    base = {
        "origin": "AMS",
        "destination": "GRU",
        "start": D1,
        "end": dt.date(2026, 12, 3),
        "stay_days": 15,
        "airlines": ("KL",),
        "max_stops": 0,
        "currencies": ("EUR",),
    }
    base.update(kw)
    return Search(**base)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def test_departures_honor_step():
    s = make_search(end=dt.date(2026, 12, 7), step_days=3)
    assert s.departures() == [D1, dt.date(2026, 12, 4), dt.date(2026, 12, 7)]
    assert s.return_of(D1) == R1


@pytest.mark.parametrize(
    "carriers, stops, ok",
    [
        (["KL"], 0, True),
        (["KL"], 1, False),  # too many stops
        (["AF"], 0, False),  # other airline
        (["KL", "AF"], 0, False),  # codeshare leg outside the filter
        ([], None, True),  # unknown: trust the engine's own filter
    ],
)
def test_accepts(carriers, stops, ok):
    assert make_search().accepts(carriers, stops) is ok


def test_any_airline_any_stops_accepts_everything():
    s = make_search(airlines=(), max_stops=None)
    assert s.accepts(["AF", "LA"], 3)
    assert s.label == "AMS<->GRU any airline any stops"


# --------------------------------------------------------------------------- #
# Travelpayouts
# --------------------------------------------------------------------------- #
def tp_row(
    price,
    airline="KL",
    transfers=0,
    ret_transfers=0,
    dep="2026-12-01T10:55:00+01:00",
    ret="2026-12-16T20:59:00-03:00",
):
    return {
        "price": price,
        "airline": airline,
        "transfers": transfers,
        "return_transfers": ret_transfers,
        "departure_at": dep,
        "return_at": ret,
    }


def test_travelpayouts_picks_cheapest_matching_row(monkeypatch):
    eng = TravelpayoutsEngine("tok")
    seen = {}

    def fake_get(url, params):
        seen.update(params)
        return {
            "success": True,
            "currency": "eur",
            "data": [
                tp_row(700, airline="AF"),  # wrong airline
                tp_row(650, transfers=1),  # not direct
                tp_row(600, ret="2026-12-17T10:00:00-03:00"),  # wrong return date
                tp_row(990),
                tp_row(950),
            ],
        }

    monkeypatch.setattr(eng, "_get_json", fake_get)
    offer = eng.fetch(make_search(), D1, "EUR")
    assert (offer.price, offer.airline, offer.stops, offer.out_dep_time) == (950, "KL", 0, "10:55")
    assert seen["direct"] == "true" and seen["currency"] == "eur" and seen["token"] == "tok"


def test_travelpayouts_currency_mismatch_is_an_error(monkeypatch):
    eng = TravelpayoutsEngine("tok")
    monkeypatch.setattr(eng, "_get_json", lambda url, params: {"currency": "rub", "data": []})
    with pytest.raises(RuntimeError, match="RUB"):
        eng.fetch(make_search(), D1, "EUR")


def test_missing_key_stops_the_engine(monkeypatch):
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    with pytest.raises(EngineError, match="TRAVELPAYOUTS_TOKEN"):
        list(TravelpayoutsEngine().offers(make_search()))


# --------------------------------------------------------------------------- #
# Ignav
# --------------------------------------------------------------------------- #
def ignav_itinerary(amount, out_codes=("KL",), back_codes=("KL",), currency="EUR"):
    def segs(codes, day):
        return [
            {"marketing_carrier_code": c, "departure_time_local": f"{day}T10:55:00"} for c in codes
        ]

    return {
        "price": {"amount": amount, "currency": currency},
        "outbound": {"segments": segs(out_codes, "2026-12-01")},
        "inbound": {"segments": segs(back_codes, "2026-12-16")},
    }


def test_ignav_request_and_parse(monkeypatch):
    eng = IgnavEngine("key")
    sent = {}

    def fake_post(url, body, headers):
        sent.update(body=body, headers=headers)
        return {
            "itineraries": [
                ignav_itinerary(800, out_codes=("KL", "AF")),  # 1 stop + other airline
                ignav_itinerary(500, currency="USD"),  # other currency
                ignav_itinerary(1010),
                ignav_itinerary(995),
            ]
        }

    monkeypatch.setattr(eng, "_post_json", fake_post)
    offer = eng.fetch(make_search(), D1, "EUR")
    assert (offer.price, offer.airline, offer.stops) == (995, "KL", 0)
    assert sent["headers"] == {"X-Api-Key": "key"}
    assert sent["body"]["market"] == "NL"
    assert sent["body"]["max_stops"] == 0 and sent["body"]["airlines_include"] == ["KL"]


def test_http_auth_and_quota_errors_stop_the_engine():
    eng = IgnavEngine("key")
    with pytest.raises(EngineError, match="rejected"):
        eng._json(FakeResponse(401))
    with pytest.raises(EngineError, match="quota"):
        eng._json(FakeResponse(429))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        eng._json(FakeResponse(500, text="boom"))


# --------------------------------------------------------------------------- #
# SerpApi
# --------------------------------------------------------------------------- #
def serp_option(price, numbers=("KL 791",)):
    legs = [
        {"flight_number": n, "departure_airport": {"time": "2026-12-01 10:55"}} for n in numbers
    ]
    return {"price": price, "flights": legs}


def test_serpapi_request_and_parse(monkeypatch):
    eng = SerpApiEngine("key")
    sent = {}

    def fake_get(url, params):
        sent.update(params)
        return {
            "best_flights": [serp_option(1100)],
            "other_flights": [serp_option(990), serp_option(700, ("KL 1", "AF 2"))],
        }

    monkeypatch.setattr(eng, "_get_json", fake_get)
    offer = eng.fetch(make_search(), D1, "EUR")
    assert (offer.price, offer.airline, offer.out_dep_time) == (990, "KL", "10:55")
    assert sent["stops"] == 1 and sent["include_airlines"] == "KL" and sent["type"] == 1


def test_serpapi_no_results_and_out_of_searches(monkeypatch):
    eng = SerpApiEngine("key")
    monkeypatch.setattr(
        eng,
        "_get_json",
        lambda url, params: {"error": "Google Flights hasn't returned any results for this query."},
    )
    assert eng.fetch(make_search(), D1, "EUR") is None
    monkeypatch.setattr(
        eng, "_get_json", lambda url, params: {"error": "Your account has run out of searches."}
    )
    with pytest.raises(EngineError):
        eng.fetch(make_search(), D1, "EUR")


# --------------------------------------------------------------------------- #
# sweep / auto fallback
# --------------------------------------------------------------------------- #
def fake_engine(key, behavior):
    """behavior(search) -> list of offers, or raises EngineError."""

    class Fake(engines.Engine):
        label = key
        cost = "free"

        def offers(self, search, stop=None):
            yield from behavior(search)

    Fake.key = key
    return Fake


@pytest.fixture
def fakes(monkeypatch):
    def install(**behaviors):
        registry = {k: fake_engine(k, b) for k, b in behaviors.items()}
        monkeypatch.setattr(engines, "ENGINES", registry)
        monkeypatch.setattr(engines, "AUTO_ORDER", list(registry))

    return install


def offers_for(*currencies):
    return lambda search: [Offer(c, D1, R1, 100.0) for c in search.currencies if c in currencies]


def test_auto_falls_back_per_currency(fakes):
    asked = []

    def second(search):
        asked.append(search.currencies)
        return offers_for("EUR")(search)

    fakes(first=offers_for("BRL"), second=second)
    r = sweep(make_search(currencies=("EUR", "BRL")), AUTO)
    assert asked == [("EUR",)]  # second engine only for what's missing
    assert sorted(o.currency for o in r.offers) == ["BRL", "EUR"]
    assert r.engine == "first+second" and r.failure == ""


def test_auto_keeps_going_after_engine_error_and_keeps_partial_offers(fakes):
    def broken(search):
        yield Offer("EUR", D1, R1, 90.0)
        raise EngineError("consent wall")

    fakes(first=broken, second=offers_for("BRL"))
    r = sweep(make_search(currencies=("EUR", "BRL")), AUTO)
    assert sorted(o.currency for o in r.offers) == ["BRL", "EUR"]
    assert r.failure == ""  # everything got covered
    assert r.errors == 1


def test_explicit_engine_reports_failure(fakes):
    def broken(search):
        raise EngineError("quota")
        yield

    fakes(first=broken, second=offers_for("EUR"))
    r = sweep(make_search(), "first")
    assert r.offers == [] and r.failure == "quota" and r.engine == "first"


def test_auto_skips_engines_without_key(fakes, monkeypatch):
    fakes(first=offers_for(), keyed=offers_for("EUR"))
    monkeypatch.setattr(engines.ENGINES["keyed"], "key_env", "SOME_UNSET_TOKEN")
    monkeypatch.delenv("SOME_UNSET_TOKEN", raising=False)
    r = sweep(make_search(), AUTO)
    assert r.offers == [] and r.engine == AUTO


@pytest.mark.parametrize(
    "url, html, wall",
    [
        ("https://consent.google.com/ml?continue=https://www.google.com/travel/flights", "", True),
        (
            "https://www.google.com/travel/flights",
            '<html><base href="https://consent.google.com/">',
            True,
        ),
        (
            "https://www.google.com/travel/flights",
            '<html><base href="https://www.google.com/">',
            False,
        ),
        (
            "https://www.google.com/travel/flights",
            "<p>see consent.google.com.evil.example</p>",
            False,
        ),
        ("https://consent.google.com.evil.example/", "", False),
    ],
)
def test_consent_wall_detection(url, html, wall):
    assert engines._on_consent_wall(url, html) is wall
