"""
Price engines: interchangeable sources for round-trip flight prices.

Every engine answers the same question for a Search -- the cheapest round trip
per outbound date and currency that honors the route filters -- from a
different data source. ENGINES lists the free ones first; "auto" runs the
free engines in that order, each one covering whatever currency the previous
ones found nothing for (blocked, rate-limited, no data).

  key              source                                     cost
  google-calendar  Google Flights price calendar (fli)        free, no key
  google-scrape    Google Flights result page (fast-flights)  free, no key
  travelpayouts    Aviasales price cache (Travelpayouts)      free, token
  ignav            Ignav live fare API                        1,000 free requests, then paid
  serpapi          Google Flights via SerpApi                 250 free searches/month

API keys come from the environment (the .env file): TRAVELPAYOUTS_TOKEN,
IGNAV_API_KEY, SERPAPI_KEY.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primp import Client

logger = logging.getLogger("flight_monitor")

# Google consent-accept cookie (bypasses consent.google.com EU wall) for the
# google-scrape engine. Refresh if runs abort "consent wall": accept cookies on
# google.com in a browser, copy the SOCS cookie value here.
SOCS_COOKIE = "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"


class EngineError(RuntimeError):
    """Stops the whole sweep (missing key, quota, blocked) -- not just one date."""


# --------------------------------------------------------------------------- #
# What to search, and what comes back
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Search:
    origin: str
    destination: str
    start: dt.date  # first outbound date
    end: dt.date  # last outbound date
    stay_days: int  # nights between outbound and return
    airlines: tuple[str, ...] = ()  # carrier codes; empty = any airline
    max_stops: int | None = 0  # per leg; 0 = direct, None = any
    step_days: int = 1
    currencies: tuple[str, ...] = ("EUR", "BRL")
    adults: int = 1

    def departures(self) -> list[dt.date]:
        out, d = [], self.start
        while d <= self.end:
            out.append(d)
            d += dt.timedelta(days=self.step_days)
        return out

    def return_of(self, depart: dt.date) -> dt.date:
        return depart + dt.timedelta(days=self.stay_days)

    def accepts(self, carriers: Iterable[str], stops: int | None) -> bool:
        """Belt & suspenders on top of each engine's own filters."""
        if self.max_stops is not None and stops is not None and stops > self.max_stops:
            return False
        carriers = {c for c in carriers if c}
        return not (self.airlines and carriers and not carriers <= set(self.airlines))

    @property
    def label(self) -> str:
        airline = "+".join(self.airlines) if self.airlines else "any airline"
        if self.max_stops is None:
            stops = "any stops"
        elif self.max_stops == 0:
            stops = "direct"
        else:
            stops = f"<={self.max_stops} stop(s)"
        return f"{self.origin}<->{self.destination} {airline} {stops}"


@dataclass
class Offer:
    currency: str
    depart: dt.date
    ret: dt.date
    price: float  # round-trip total
    airline: str = ""  # carrier code(s), e.g. "KL" or "KL+AF"
    stops: int | None = None  # most stops on either leg
    out_dep_time: str = "--:--"  # outbound departure, local time


def _cheapest(offers: Iterable[Offer]) -> Offer | None:
    return min(offers, key=lambda o: o.price, default=None)


def _carriers(codes: Iterable[str]) -> str:
    return "+".join(sorted({c for c in codes if c}))


def _wait(stop: threading.Event | None, seconds: float) -> None:
    if stop is not None:
        stop.wait(seconds)  # returns early when a stop is requested
    else:
        time.sleep(seconds)


# --------------------------------------------------------------------------- #
# Engine base
# --------------------------------------------------------------------------- #
class Engine:
    key = ""
    label = ""
    cost = ""  # short price tag for the picker
    about = ""  # what it reads and its catch, one or two sentences
    key_env = ""  # env var holding the API key; "" = no key needed
    signup_url = ""
    pause = (0.0, 0.0)  # politeness between requests (seconds, min/max)

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or (os.environ.get(self.key_env, "") if self.key_env else "")
        self.errors = 0
        self._http: Client | None = None

    @property
    def ready(self) -> bool:
        return not self.key_env or bool(self.api_key)

    @classmethod
    def requests_for(cls, search: Search) -> int:
        """Rough request count for a sweep (to size it against a free quota)."""
        return len(search.departures()) * len(search.currencies)

    def offers(self, search: Search, stop: threading.Event | None = None) -> Iterator[Offer]:
        """Yield the cheapest offer per (outbound date, currency) found."""
        if not self.ready:
            raise EngineError(
                f"{self.label} needs an API key: set {self.key_env} in .env "
                f"(sign up: {self.signup_url})."
            )
        self.setup(search)
        pairs = [(d, c) for d in search.departures() for c in search.currencies]
        for i, (depart, currency) in enumerate(pairs):
            if stop is not None and stop.is_set():
                return
            if i:
                _wait(stop, random.uniform(*self.pause))
            try:
                offer = self.fetch(search, depart, currency)
            except EngineError:
                raise
            except Exception as e:
                self.errors += 1
                logger.info(f"  {depart} {currency}: error {e}")
                continue
            if offer:
                yield offer

    def setup(self, search: Search) -> None:
        """Per-sweep preparation (clients, cookies)."""

    def fetch(self, search: Search, depart: dt.date, currency: str) -> Offer | None:
        raise NotImplementedError

    # -- JSON-over-HTTP helpers for the API engines --------------------------
    def _client(self) -> Client:
        if self._http is None:
            from primp import Client

            self._http = Client(timeout=30)
        return self._http

    def _get_json(self, url: str, params: dict) -> dict:
        return self._json(self._client().get(url, params=params))

    def _post_json(self, url: str, body: dict, headers: dict) -> dict:
        return self._json(self._client().post(url, json=body, headers=headers))

    def _json(self, resp) -> dict:
        if resp.status_code in (401, 403):
            raise EngineError(
                f"{self.label}: API key rejected (HTTP {resp.status_code}); check {self.key_env}."
            )
        if resp.status_code == 429:
            raise EngineError(f"{self.label}: rate limit or quota reached (HTTP 429).")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


# --------------------------------------------------------------------------- #
# Free, no key
# --------------------------------------------------------------------------- #
class GoogleCalendarEngine(Engine):
    key = "google-calendar"
    label = "Google Flights - price calendar"
    cost = "free, no key"
    about = (
        "Reads Google Flights' price calendar (via the fli library): the whole "
        "window in 1-2 requests per currency, plus one detail lookup for the "
        "cheapest date. Fastest, and the least likely to get blocked."
    )

    @classmethod
    def requests_for(cls, search: Search) -> int:
        chunks = -(-((search.end - search.start).days + 1) // 61)  # fli: 61 days a call
        return (chunks + 1) * len(search.currencies)

    def offers(self, search: Search, stop: threading.Event | None = None) -> Iterator[Offer]:
        try:
            from fli.models import (
                Airline,
                Airport,
                DateSearchFilters,
                FlightSegment,
                MaxStops,
                PassengerInfo,
                SeatType,
                TripType,
            )
            from fli.search import SearchDates
        except ImportError as e:
            raise EngineError(
                "google-calendar needs the 'flights' package: pip install -r requirements.txt"
            ) from e

        def enum(kind, code: str, what: str):
            try:
                return kind[code] if code in kind.__members__ else kind["_" + code]
            except KeyError:
                raise EngineError(f"{self.key}: unknown {what} code {code!r}.") from None

        # Google's calendar only covers future dates.
        today = dt.date.today()
        wanted = {d for d in search.departures() if d > today}
        if not wanted:
            return
        first, last = min(wanted), max(wanted)
        origin = enum(Airport, search.origin, "airport")
        dest = enum(Airport, search.destination, "airport")
        stops = {
            None: MaxStops.ANY,
            0: MaxStops.NON_STOP,
            1: MaxStops.ONE_STOP_OR_FEWER,
            2: MaxStops.TWO_OR_FEWER_STOPS,
        }
        filters = DateSearchFilters(
            trip_type=TripType.ROUND_TRIP,
            passenger_info=PassengerInfo(adults=search.adults),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[origin, 0]],
                    arrival_airport=[[dest, 0]],
                    travel_date=first.isoformat(),
                ),
                FlightSegment(
                    departure_airport=[[dest, 0]],
                    arrival_airport=[[origin, 0]],
                    travel_date=search.return_of(first).isoformat(),
                ),
            ],
            stops=stops.get(search.max_stops, MaxStops.ANY),
            seat_type=SeatType.ECONOMY,
            airlines=[enum(Airline, a, "airline") for a in search.airlines] or None,
            from_date=first.isoformat(),
            to_date=last.isoformat(),
            duration=search.stay_days,
        )

        for currency in search.currencies:
            if stop is not None and stop.is_set():
                return
            try:
                prices = SearchDates().search(filters, currency=currency) or []
            except Exception as e:
                self.errors += 1
                logger.info(f"  calendar {currency}: error {e}")
                continue
            found = []
            for p in prices:
                depart = p.date[0].date()
                if depart not in wanted or not p.price:
                    continue
                if p.currency and p.currency.upper() != currency:
                    logger.info(f"  calendar {currency}: answered in {p.currency}, skipped.")
                    break
                # The calendar has prices only; carry over what the filters pin down.
                found.append(
                    Offer(
                        currency,
                        depart,
                        search.return_of(depart),
                        float(p.price),
                        airline=search.airlines[0] if len(search.airlines) == 1 else "",
                        stops=0 if search.max_stops == 0 else None,
                    )
                )
            best = _cheapest(found)
            if best:
                self._detail(best, search, filters, stop)
            yield from sorted(found, key=lambda o: o.depart)

    def _detail(self, offer: Offer, search: Search, filters, stop) -> None:
        """Fill airline / stops / departure time for one date via the exact search."""
        from fli.models import FlightSearchFilters, SortBy
        from fli.search import SearchFlights

        if stop is not None and stop.is_set():
            return
        segments = [s.model_copy() for s in filters.flight_segments]
        segments[0].travel_date = offer.depart.isoformat()
        segments[1].travel_date = offer.ret.isoformat()
        exact = FlightSearchFilters(
            trip_type=filters.trip_type,
            passenger_info=filters.passenger_info,
            flight_segments=segments,
            stops=filters.stops,
            seat_type=filters.seat_type,
            airlines=filters.airlines,
            sort_by=SortBy.CHEAPEST,
        )
        try:
            results = SearchFlights().search(exact, top_n=1, currency=offer.currency) or []
        except Exception as e:
            logger.info(f"  {offer.depart} {offer.currency}: detail lookup failed ({e}).")
            return
        trips = []
        for trip in results:
            # (outbound, return) pair; the return leg carries the pair's total --
            # the outbound's price is only the cheapest total it can be part of.
            legs = [leg for part in trip for leg in part.legs]
            stops = max(part.stops for part in trip)
            carriers = [leg.airline.name.removeprefix("_") for leg in legs]
            if trip[-1].price is not None and search.accepts(carriers, stops):
                trips.append((trip[-1].price, trip, carriers, stops))
        if not trips:
            return
        price, trip, carriers, stops = min(trips, key=lambda t: t[0])
        if abs(price - offer.price) >= 1:
            logger.info(
                f"  {offer.depart} {offer.currency}: calendar said {offer.price:.0f}, "
                f"live price {price:.0f}."
            )
            offer.price = float(price)
        offer.airline = _carriers(carriers)
        offer.stops = stops
        offer.out_dep_time = trip[0].legs[0].departure_datetime.strftime("%H:%M")


class GoogleScrapeEngine(Engine):
    key = "google-scrape"
    label = "Google Flights - result page"
    cost = "free, no key"
    about = (
        "Loads one Google Flights result page per date and currency (via "
        "fast-flights). The original engine; slow (~2 requests per date) and the "
        "most exposed to blocking, the consent wall and page changes."
    )
    pause = (1.2, 2.6)

    def setup(self, search: Search) -> None:
        from primp import Client

        client = Client(
            impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True
        )
        client.set_cookies("https://www.google.com", {"SOCS": SOCS_COOKIE})
        self._http = client

    def fetch(self, search: Search, depart: dt.date, currency: str) -> Offer | None:
        from fast_flights import FlightQuery, Passengers, create_query
        from fast_flights.fetcher import URL
        from fast_flights.parser import parse

        ret = search.return_of(depart)
        airlines = list(search.airlines) or None
        q = create_query(
            flights=[
                FlightQuery(
                    date=depart.isoformat(),
                    from_airport=search.origin,
                    to_airport=search.destination,
                    max_stops=search.max_stops,
                    airlines=airlines,
                ),
                FlightQuery(
                    date=ret.isoformat(),
                    from_airport=search.destination,
                    to_airport=search.origin,
                    max_stops=search.max_stops,
                    airlines=airlines,
                ),
            ],
            trip="round-trip",
            seat="economy",
            passengers=Passengers(adults=search.adults),
            currency=currency,
            max_stops=search.max_stops,
        )
        html = self._client().get(URL, params=q.params()).text
        if "consent.google.com" in html[:400]:
            raise EngineError("Hit Google consent wall (refresh SOCS_COOKIE in engines.py).")
        # fast-flights' parser raises (TypeError/IndexError) when Google returns an
        # empty result set -- i.e. no matching combo for this date pair. Treat as
        # "no availability", not an error.
        try:
            results = list(parse(html))
        except (TypeError, IndexError, AttributeError):
            return None
        found = []
        for f in results:
            if f.price is None or not f.flights:
                continue
            first, last = f.flights[0], f.flights[-1]
            if (
                first.from_airport.code != search.origin
                or last.to_airport.code != search.destination
            ):
                continue
            stops = len(f.flights) - 1
            if not search.accepts([f.type], stops):
                continue
            try:
                price = float(f.price)
            except (TypeError, ValueError):
                continue
            t = first.departure.time if first.departure else None
            found.append(
                Offer(
                    currency,
                    depart,
                    ret,
                    price,
                    airline=f.type or "",
                    stops=stops,
                    out_dep_time=f"{t[0]:02d}:{t[1]:02d}" if t else "--:--",
                )
            )
        return _cheapest(found)


# --------------------------------------------------------------------------- #
# Free with a token
# --------------------------------------------------------------------------- #
class TravelpayoutsEngine(Engine):
    key = "travelpayouts"
    label = "Aviasales price cache (Travelpayouts)"
    cost = "free, token"
    about = (
        "Cheapest fares Aviasales users found in the last ~48 h: cached, not "
        "live, and dates nobody searched come back empty. Free token from a "
        "Travelpayouts account (Profile > API token)."
    )
    key_env = "TRAVELPAYOUTS_TOKEN"
    signup_url = "https://www.travelpayouts.com/"
    pause = (0.2, 0.5)
    URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

    def fetch(self, search: Search, depart: dt.date, currency: str) -> Offer | None:
        ret = search.return_of(depart)
        data = self._get_json(
            self.URL,
            {
                "origin": search.origin,
                "destination": search.destination,
                "departure_at": depart.isoformat(),
                "return_at": ret.isoformat(),
                "one_way": "false",
                "direct": "true" if search.max_stops == 0 else "false",
                "currency": currency.lower(),
                "sorting": "price",
                "unique": "false",
                "limit": 100,
                "page": 1,
                "token": self.api_key,
            },
        )
        if data.get("success") is False:
            raise RuntimeError(data.get("error") or "request failed")
        got = (data.get("currency") or currency).upper()
        if got != currency:
            raise RuntimeError(f"answered in {got}, not {currency}")
        found = []
        for row in data.get("data") or []:
            if (
                str(row.get("departure_at", ""))[:10] != depart.isoformat()
                or str(row.get("return_at", ""))[:10] != ret.isoformat()
            ):
                continue
            stops = max(int(row.get("transfers") or 0), int(row.get("return_transfers") or 0))
            airline = row.get("airline") or ""
            if row.get("price") is None or not search.accepts([airline], stops):
                continue
            found.append(
                Offer(
                    currency,
                    depart,
                    ret,
                    float(row["price"]),
                    airline=airline,
                    stops=stops,
                    out_dep_time=str(row["departure_at"])[11:16] or "--:--",
                )
            )
        return _cheapest(found)


# --------------------------------------------------------------------------- #
# Free tier, then paid
# --------------------------------------------------------------------------- #
class IgnavEngine(Engine):
    key = "ignav"
    label = "Ignav fare API"
    cost = "1,000 free requests, then $2 per 1,000"
    about = (
        "Live fares. One request per date and currency, so the one-time free "
        "allowance covers about 8 full default sweeps. Currency follows the "
        "market (EUR = NL, BRL = BR point of sale)."
    )
    key_env = "IGNAV_API_KEY"
    signup_url = "https://ignav.com/signup"
    URL = "https://ignav.com/api/fares/round-trip"
    MARKETS = {
        "EUR": "NL",
        "BRL": "BR",
        "USD": "US",
        "GBP": "GB",
        "CHF": "CH",
        "CAD": "CA",
        "AUD": "AU",
        "JPY": "JP",
        "SEK": "SE",
        "NOK": "NO",
        "DKK": "DK",
    }

    def fetch(self, search: Search, depart: dt.date, currency: str) -> Offer | None:
        market = self.MARKETS.get(currency)
        if market is None:
            raise EngineError(f"{self.key} prices by market; none mapped for {currency}.")
        ret = search.return_of(depart)
        body = {
            "origin": search.origin,
            "destination": search.destination,
            "departure_date": depart.isoformat(),
            "return_date": ret.isoformat(),
            "adults": search.adults,
            "cabin_class": "economy",
            "market": market,
        }
        if search.max_stops is not None and search.max_stops <= 2:
            body["max_stops"] = search.max_stops
        if search.airlines:
            body["airlines_include"] = list(search.airlines)
        data = self._post_json(self.URL, body, {"X-Api-Key": self.api_key})
        found = []
        for it in data.get("itineraries") or []:
            price = it.get("price") or {}
            if price.get("amount") is None or str(price.get("currency", "")).upper() != currency:
                continue
            out = (it.get("outbound") or {}).get("segments") or []
            back = (it.get("inbound") or {}).get("segments") or []
            if not out:
                continue
            stops = max(len(out), len(back)) - 1
            carriers = [s.get("marketing_carrier_code", "") for s in out + back]
            if not search.accepts(carriers, stops):
                continue
            found.append(
                Offer(
                    currency,
                    depart,
                    ret,
                    float(price["amount"]),
                    airline=_carriers(carriers),
                    stops=stops,
                    out_dep_time=str(out[0].get("departure_time_local", ""))[11:16] or "--:--",
                )
            )
        return _cheapest(found)


class SerpApiEngine(Engine):
    key = "serpapi"
    label = "Google Flights via SerpApi"
    cost = "250 free searches/month"
    about = (
        "Google Flights results through a maintained scraping API. One search "
        "per date and currency; the free plan also caps 50/hour, so use a 3+ day "
        "step and one currency."
    )
    key_env = "SERPAPI_KEY"
    signup_url = "https://serpapi.com/users/sign_up"
    URL = "https://serpapi.com/search.json"
    STOPS = {None: 0, 0: 1, 1: 2, 2: 3}  # SerpApi: 0 any, 1 nonstop, 2 <=1, 3 <=2

    def fetch(self, search: Search, depart: dt.date, currency: str) -> Offer | None:
        ret = search.return_of(depart)
        params = {
            "engine": "google_flights",
            "type": 1,
            "hl": "en",
            "departure_id": search.origin,
            "arrival_id": search.destination,
            "outbound_date": depart.isoformat(),
            "return_date": ret.isoformat(),
            "currency": currency,
            "adults": search.adults,
            "stops": self.STOPS.get(search.max_stops, 0),
            "api_key": self.api_key,
        }
        if search.airlines:
            params["include_airlines"] = ",".join(search.airlines)
        data = self._get_json(self.URL, params)
        if data.get("error"):
            msg = str(data["error"])
            if "run out of searches" in msg:
                raise EngineError(f"{self.label}: {msg}")
            if "returned any results" in msg:
                return None
            raise RuntimeError(msg)
        found = []
        for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
            legs = it.get("flights") or []
            if not legs or it.get("price") is None:
                continue
            stops = len(legs) - 1
            carriers = [str(leg.get("flight_number", "")).split(" ")[0] for leg in legs]
            if not search.accepts(carriers, stops):
                continue
            dep = str((legs[0].get("departure_airport") or {}).get("time", ""))
            found.append(
                Offer(
                    currency,
                    depart,
                    ret,
                    float(it["price"]),
                    airline=_carriers(carriers),
                    stops=stops,
                    out_dep_time=dep[11:16] or "--:--",
                )
            )
        return _cheapest(found)


# --------------------------------------------------------------------------- #
# Registry and sweep
# --------------------------------------------------------------------------- #
ENGINES: dict[str, type[Engine]] = {
    e.key: e
    for e in (
        GoogleCalendarEngine,
        GoogleScrapeEngine,
        TravelpayoutsEngine,
        IgnavEngine,
        SerpApiEngine,
    )
}
AUTO = "auto"
# Free engines only: "auto" never spends a paid quota.
AUTO_ORDER = ["google-calendar", "google-scrape", "travelpayouts"]


@dataclass
class SweepResult:
    offers: list[Offer] = field(default_factory=list)
    errors: int = 0
    engine: str = ""  # engine key(s) that supplied offers, "+"-joined
    failure: str = ""  # why currencies are missing (EngineError), if they are


def sweep(
    search: Search,
    engine: str = AUTO,
    *,
    api_keys: dict[str, str] | None = None,
    stop: threading.Event | None = None,
    on_engine: Callable[[str], None] | None = None,
    on_offer: Callable[[Offer], None] | None = None,
) -> SweepResult:
    """Run one engine over the search window -- or "auto": the free engines in
    order, each covering the currencies the previous ones found nothing for.

    Never raises EngineError: offers found before one stopped an engine are
    kept, and the reason lands in SweepResult.failure.
    """
    keys = AUTO_ORDER if engine == AUTO else [engine]
    result = SweepResult()
    used, failures = [], []
    missing = list(search.currencies)
    for key in keys:
        if not missing or (stop is not None and stop.is_set()):
            break
        eng = ENGINES[key]((api_keys or {}).get(key))
        if engine == AUTO and not eng.ready:
            continue
        part = replace(search, currencies=tuple(missing))
        only = f", for {'/'.join(missing)}" if len(missing) < len(search.currencies) else ""
        logger.info(f"Engine: {eng.label} ({eng.cost}){only}.")
        if on_engine:
            on_engine(key)
        found: list[Offer] = []
        try:
            for offer in eng.offers(part, stop):
                found.append(offer)
                if on_offer:
                    on_offer(offer)
        except EngineError as e:
            result.errors += 1
            failures.append(str(e))
            logger.info(f"  {key} stopped: {e}")
        result.errors += eng.errors
        if found:
            used.append(key)
            result.offers += found
        missing = [c for c in missing if not any(o.currency == c for o in found)]
        if missing and found:
            logger.info(f"  {key} found nothing for {'/'.join(missing)}.")
    result.engine = "+".join(used) or engine
    if failures and (missing or engine != AUTO):
        result.failure = "; ".join(failures)
    return result
