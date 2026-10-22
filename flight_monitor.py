#!/usr/bin/env python3
"""
Flight price monitor: round trip, by default AMS <-> GRU, direct KLM only.

Each run sweeps every outbound date in the window (default 01-12-2026 ..
28-01-2027), each with a +STAY nights return, in EUR and BRL, keeps the
cheapest offer that honors the route filters, classifies it with a traffic
light (LOW / NORMAL / HIGH), and emails the result.

Prices come from a selectable engine (--engine; see engines.py or run
--list-engines). The default "auto" runs the free engines in order, falling
back to the next one for any currency the previous one found nothing for.

Traffic light ("both" mode):
  - First MIN_HISTORY_RUNS runs: fixed EUR thresholds (--low-eur / --high-eur).
  - After that: rolling median of past runs' lowest EUR price.
      LOW  = current <= median * (1 - BAND)
      HIGH = current >= median * (1 + BAND)
      else NORMAL

Defaults: built in (DEFAULTS below) < data/settings.json (written by the GUI's
"Save as default") < command-line arguments.

Email: Gmail SMTP + App Password. Put creds in a .env file next to this script:
  GMAIL_USER=you@example.com
  GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx      # 16-char Google App Password

Set the report recipient with --email or the REPORT_EMAIL environment variable.

Run:  python flight_monitor.py            (uses defaults)
      python flight_monitor.py --help     (all overridable parameters)
      python flight_monitor_gui.py        (the same, from a window)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import smtplib
import ssl
import statistics
import sys
import threading
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path

from engines import AUTO, AUTO_ORDER, ENGINES, Offer, Search, sweep

# --------------------------------------------------------------------------- #
# Defaults -- overridden by data/settings.json, then by CLI args.
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "origin": "AMS",
    "destination": "GRU",
    "airline": "KL",            # carrier code(s), comma-separated; "ANY" = all
    "max_stops": "0",           # 0 = direct only; 1 = up to one stopover; "any"
    "start": "2026-12-01",
    "end": "2027-01-28",
    "stay_days": 15,
    "step_days": 1,
    "currencies": "EUR,BRL",
    "engine": AUTO,
    "low_eur": 1000.0,
    "high_eur": 1250.0,
}

# --------------------------------------------------------------------------- #
# Fixed (non-parameter) config
# --------------------------------------------------------------------------- #
ADULTS = 1

MIN_HISTORY_RUNS = 6          # runs of fixed thresholds before auto median
BAND = 0.10                   # +/-10% band around the median for LOW/HIGH

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
DATA_DIR = HERE / "data"
HISTORY_CSV = DATA_DIR / "price_history.csv"      # every offer, every run
RUNS_CSV = DATA_DIR / "run_summary.csv"           # one row per run (for median)
BEST_JSON = DATA_DIR / "best_prices.json"         # all-time low per currency
SETTINGS_JSON = DATA_DIR / "settings.json"        # saved defaults (GUI)
LOG_FILE = DATA_DIR / "monitor.log"

logger = logging.getLogger("flight_monitor")


# --------------------------------------------------------------------------- #
# Parameters (CLI, with defaults)
# --------------------------------------------------------------------------- #
def parse_airlines(text: str) -> tuple[str, ...]:
    codes = tuple(c.strip().upper() for c in str(text).replace("+", ",").split(",")
                  if c.strip())
    return () if "ANY" in codes else codes


def parse_stops(text) -> int | None:
    s = str(text).strip().lower()
    if s in ("", "any", "-1"):
        return None
    n = int(s)
    if n < 0:
        raise ValueError(f"max stops must be >= 0 or 'any', got {text!r}")
    return n


def parse_currencies(text: str) -> tuple[str, ...]:
    out = tuple(dict.fromkeys(c.strip().upper() for c in str(text).split(",") if c.strip()))
    if not out:
        raise ValueError("at least one currency is needed")
    return out


def load_settings() -> dict:
    """DEFAULTS overlaid with data/settings.json, if present and readable."""
    settings = dict(DEFAULTS)
    if SETTINGS_JSON.exists():
        try:
            saved = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log(f"Ignoring {SETTINGS_JSON.name}: {e}")
        else:
            settings.update({k: v for k, v in saved.items() if k in DEFAULTS})
    if settings["engine"] not in (AUTO, *ENGINES):
        log(f"Unknown engine {settings['engine']!r} in {SETTINGS_JSON.name}; using {AUTO}.")
        settings["engine"] = AUTO
    return settings


def save_settings(values: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    keep = {k: values[k] for k in DEFAULTS if k in values}
    SETTINGS_JSON.write_text(json.dumps(keep, indent=2), encoding="utf-8")


def parse_args(argv=None, settings: dict | None = None) -> argparse.Namespace:
    s = settings if settings is not None else load_settings()
    p = argparse.ArgumentParser(description="Round-trip flight price monitor.")
    p.add_argument("--origin", default=s["origin"],
                   help=f"Origin airport IATA code (default {s['origin']}).")
    p.add_argument("--destination", default=s["destination"],
                   help=f"Destination airport IATA code (default {s['destination']}).")
    p.add_argument("--airline", type=parse_airlines, default=s["airline"],
                   help="Airline code(s), comma-separated, or ANY for all "
                        f"(default {s['airline']}).")
    p.add_argument("--max-stops", type=parse_stops, default=s["max_stops"],
                   help="Max stopovers per leg: 0 = direct, 1, 2, or 'any' "
                        f"(default {s['max_stops']}).")
    p.add_argument("--start", default=s["start"],
                   help=f"First outbound date, YYYY-MM-DD (default {s['start']}).")
    p.add_argument("--end", default=s["end"],
                   help=f"Last outbound date, YYYY-MM-DD (default {s['end']}).")
    p.add_argument("--stay-days", type=int, default=s["stay_days"],
                   help=f"Nights at destination / return offset (default {s['stay_days']}).")
    p.add_argument("--step-days", type=int, default=s["step_days"],
                   help=f"Days between probed outbound dates (default {s['step_days']}).")
    p.add_argument("--currencies", type=parse_currencies, default=s["currencies"],
                   help=f"Comma-separated currencies (default {s['currencies']}).")
    p.add_argument("--engine", choices=[AUTO, *ENGINES], default=s["engine"],
                   help=f"Price source (default {s['engine']}); see --list-engines.")
    p.add_argument("--list-engines", action="store_true",
                   help="Show the price engines, free first, and exit.")
    p.add_argument("--email", default=os.environ.get("REPORT_EMAIL", "you@example.com"),
                   help="Recipient for the report (or set REPORT_EMAIL env var).")
    p.add_argument("--low-eur", type=float, default=s["low_eur"],
                   help=f"Fixed-mode: EUR at/below this = LOW (default {s['low_eur']:.0f}).")
    p.add_argument("--high-eur", type=float, default=s["high_eur"],
                   help=f"Fixed-mode: EUR at/above this = HIGH (default {s['high_eur']:.0f}).")
    p.add_argument("--no-email", action="store_true",
                   help="Skip sending email (log only).")
    return p.parse_args(argv)


def build_search(origin: str, destination: str, airlines: tuple[str, ...],
                 max_stops: int | None, start: dt.date, end: dt.date, stay_days: int,
                 step_days: int, currencies: tuple[str, ...]) -> Search:
    """Validated Search; raises ValueError naming the bad field."""
    origin, destination = origin.strip().upper(), destination.strip().upper()
    for name, code in (("origin", origin), ("destination", destination)):
        if len(code) != 3 or not code.isalpha():
            raise ValueError(f"{name} must be a 3-letter IATA code, got {code!r}")
    if origin == destination:
        raise ValueError("origin and destination are the same airport")
    if start > end:
        raise ValueError(f"first outbound date {start} is after the last one {end}")
    if stay_days < 1 or step_days < 1:
        raise ValueError("stay and step must be at least 1 day")
    return Search(origin=origin, destination=destination, start=start, end=end,
                  stay_days=stay_days, airlines=airlines, max_stops=max_stops,
                  step_days=step_days, currencies=currencies, adults=ADULTS)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    """monitor.log, plus the console when there is one (pythonw has none)."""
    if logger.handlers:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.setLevel(logging.INFO)


def log(msg: str) -> None:
    logger.info(msg)


def load_dotenv() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def set_env_value(key: str, value: str) -> None:
    """Write KEY=value into .env (replacing an existing line) and the live environment."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    out, done = [], False
    for line in lines:
        name = line.partition("=")[0].strip()
        if name == key and not line.lstrip().startswith("#"):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value


# --------------------------------------------------------------------------- #
# Traffic light
# --------------------------------------------------------------------------- #
def past_run_low_eur() -> list[float]:
    if not RUNS_CSV.exists():
        return []
    out = []
    with RUNS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                out.append(float(row["low_eur"]))
            except (KeyError, ValueError):
                pass
    return out


def classify(current_eur: float, low_eur: float, high_eur: float):
    """Return (light, mode, baseline_median_or_None)."""
    history = past_run_low_eur()
    if len(history) < MIN_HISTORY_RUNS:
        if current_eur <= low_eur:
            light = "LOW"
        elif current_eur >= high_eur:
            light = "HIGH"
        else:
            light = "NORMAL"
        return light, "fixed", None
    med = statistics.median(history)
    if current_eur <= med * (1 - BAND):
        light = "LOW"
    elif current_eur >= med * (1 + BAND):
        light = "HIGH"
    else:
        light = "NORMAL"
    return light, "auto-median", med


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
HIST_FIELDS = ["checked_at", "currency", "depart", "return", "price", "out_dep_time"]
RUN_FIELDS = ["run_time", "light", "mode", "baseline_median",
              "low_eur", "low_eur_depart", "low_eur_return",
              "low_brl", "low_brl_depart", "low_brl_return"]


def offer_row(o: Offer, checked_at: str) -> dict:
    return {"checked_at": checked_at, "currency": o.currency, "depart": o.depart.isoformat(),
            "return": o.ret.isoformat(), "price": o.price, "out_dep_time": o.out_dep_time}


def append_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def update_best(rows: list[dict], save: bool = True) -> dict:
    best = {}
    if BEST_JSON.exists():
        best = json.loads(BEST_JSON.read_text(encoding="utf-8"))
    improved = {}
    for r in rows:
        cur = r["currency"]
        prev = best.get(cur, {}).get("price")
        if prev is None or r["price"] < prev:
            best[cur] = r
            improved[cur] = r
    if save:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BEST_JSON.write_text(json.dumps(best, indent=2), encoding="utf-8")
    return improved


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
LIGHT_EMOJI = {"LOW": "\U0001F7E2", "NORMAL": "\U0001F7E1", "HIGH": "\U0001F534"}
CURRENCY_SIGN = {"EUR": "€", "BRL": "R$ ", "USD": "$", "GBP": "£"}


def money(currency: str, amount: float) -> str:
    return f"{CURRENCY_SIGN.get(currency, currency + ' ')}{amount:.0f}"


def send_email(to_addr: str, subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pwd:
        log("Email skipped: GMAIL_USER / GMAIL_APP_PASSWORD not set in .env.")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(user, pwd.replace(" ", ""))
        s.sendmail(user, [to_addr], msg.as_string())
    log(f"Email sent to {to_addr}.")


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    search: Search
    engine: str = ""
    offers: list[Offer] = field(default_factory=list)
    errors: int = 0
    best: dict[str, Offer] = field(default_factory=dict)   # cheapest per currency
    light: str = ""              # LOW / NORMAL / HIGH; "" without an EUR price
    mode: str = ""
    median: float | None = None
    new_low: bool = False        # beat the all-time EUR low
    failure: str = ""            # why the sweep was cut short (EngineError)
    cancelled: bool = False


def engine_label(key: str) -> str:
    """Label of an engine key, or of "+"-joined keys ("auto" falling back per currency)."""
    return " + ".join(ENGINES[k].label if k in ENGINES else k for k in key.split("+"))


def format_report(r: RunResult, low_eur: float, high_eur: float) -> tuple[str, str]:
    """(subject, body) of the report email."""
    s = r.search
    head = r.best.get("EUR") or next(iter(r.best.values()))
    tag = r.light or "INFO"
    subject = (f"[{tag}] {s.origin}-{s.destination} {head.currency} {head.price:.0f} "
               f"(out {head.depart})")
    lines = [
        f"{s.label}, {s.origin} <-> {s.destination}, {s.stay_days}-night stay.",
        f"Outbound window scanned: {s.start} .. {s.end} (step {s.step_days}d).",
        f"Engine: {engine_label(r.engine)}.",
        "",
    ]
    if r.failure:
        lines += [f"WARNING: sweep cut short, results may be incomplete: {r.failure}", ""]
    if r.light:
        lines.append(f"TRAFFIC LIGHT: {LIGHT_EMOJI.get(r.light, '')} {r.light}   [{r.mode}]")
        lines.append(f"Baseline: rolling median of past runs = €{r.median:.0f} "
                     f"(±{int(BAND*100)}% band)" if r.mode == "auto-median"
                     else f"Baseline: fixed thresholds LOW≤€{low_eur:.0f} / "
                          f"HIGH≥€{high_eur:.0f}")
        lines.append("")
    for cur in s.currencies:
        o = r.best.get(cur)
        if not o:
            continue
        newlow = " *NEW ALL-TIME LOW*" if cur == "EUR" and r.new_low else ""
        stops = "" if o.stops is None else (", direct" if o.stops == 0
                                            else f", {o.stops} stop(s)")
        airline = f", {o.airline}" if o.airline else ""
        lines += [f"Cheapest {cur}: {money(cur, o.price)}{newlow}",
                  f"   outbound {o.depart} (dep {o.out_dep_time}{airline}{stops})  /  "
                  f"return {o.ret}", ""]
    lines += [f"Offers scanned this run: {len(r.offers)}  |  errors: {r.errors}",
              f"Data: {HISTORY_CSV}"]
    return subject, "\n".join(lines)


def run_monitor(search: Search, engine: str = AUTO, *, recipient: str = "",
                send: bool = True, record: bool = True,
                low_eur: float = DEFAULTS["low_eur"], high_eur: float = DEFAULTS["high_eur"],
                api_keys: dict[str, str] | None = None, stop: threading.Event | None = None,
                on_engine: Callable[[str], None] | None = None,
                on_offer: Callable[[Offer], None] | None = None) -> RunResult:
    """One monitor run: sweep, then record, classify and report. Shared by CLI and GUI."""
    r = RunResult(search, engine=engine)
    route = search.label
    log(f"Run start. {route}, {search.stay_days}n stay, outbound {search.start}..{search.end} "
        f"step {search.step_days}d, currencies {list(search.currencies)}, engine {engine}.")
    sw = sweep(search, engine, api_keys=api_keys, stop=stop,
               on_engine=on_engine, on_offer=on_offer)
    r.offers, r.errors, r.engine, r.failure = sw.offers, sw.errors, sw.engine, sw.failure
    if stop is not None and stop.is_set():
        r.cancelled = True
        log("Run cancelled; nothing recorded or sent.")
        return r

    for o in r.offers:
        if o.currency not in r.best or o.price < r.best[o.currency].price:
            r.best[o.currency] = o

    if not r.best:
        log(f"No {route} offers found this run. ({r.errors} errors)")
        # Still notify so silence never hides a scraper break.
        if send:
            send_email(recipient,
                       f"[WARN] {search.origin}-{search.destination} monitor: no offers found",
                       f"Run at {dt.datetime.now():%Y-%m-%d %H:%M} found no {route} offers "
                       f"via {engine_label(r.engine)} ({r.errors} errors). "
                       + (f"{r.failure} " if r.failure else "")
                       + "Possible scraper/consent issue — check monitor.log.")
        return r

    now = dt.datetime.now().isoformat(timespec="seconds")
    rows = [offer_row(o, now) for o in r.offers]
    r.new_low = "EUR" in update_best(rows, save=record)

    eur, brl = r.best.get("EUR"), r.best.get("BRL")
    if eur:
        r.light, r.mode, r.median = classify(eur.price, low_eur, high_eur)

    if record:
        append_rows(HISTORY_CSV, HIST_FIELDS, rows)
        # Record this run's summary (used for future median baseline).
        if eur:
            append_rows(RUNS_CSV, RUN_FIELDS, [{
                "run_time": now, "light": r.light, "mode": r.mode,
                "baseline_median": f"{r.median:.0f}" if r.median is not None else "",
                "low_eur": eur.price, "low_eur_depart": eur.depart.isoformat(),
                "low_eur_return": eur.ret.isoformat(),
                "low_brl": brl.price if brl else "",
                "low_brl_depart": brl.depart.isoformat() if brl else "",
                "low_brl_return": brl.ret.isoformat() if brl else "",
            }])

    summary = "  ".join(f"{o.currency} {o.price:.0f} ({o.depart})" for o in r.best.values())
    log(f"Cheapest: {summary}" + (f" light={r.light} [{r.mode}]" if r.light else ""))
    if send:
        subject, body = format_report(r, low_eur, high_eur)
        try:
            send_email(recipient, subject, body)
        except Exception as e:
            log(f"Email FAILED: {e}")

    log(f"Run done. {len(r.offers)} offers, {r.errors} errors.")
    return r


def list_engines() -> str:
    rows = [f"  {AUTO:<16} free engines in order ({', '.join(AUTO_ORDER)}), "
            "falling back per currency"]
    for key, e in ENGINES.items():
        need = f"  [needs {e.key_env}]" if e.key_env else ""
        rows.append(f"  {key:<16} {e.label} -- {e.cost}{need}")
    return "Price engines (free first):\n" + "\n".join(rows)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    load_dotenv()
    setup_logging()
    args = parse_args(argv)
    if args.list_engines:
        print(list_engines())
        return 0
    # Route the report to the real recipient without hardcoding it in the repo:
    # REPORT_EMAIL wins, else the Gmail account owner, else the placeholder.
    if args.email == "you@example.com":
        args.email = (os.environ.get("REPORT_EMAIL")
                      or os.environ.get("GMAIL_USER")
                      or args.email)
    try:
        search = build_search(args.origin, args.destination, args.airline, args.max_stops,
                              dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end),
                              args.stay_days, args.step_days, args.currencies)
    except ValueError as e:
        log(f"Invalid parameters: {e}")
        return 1
    r = run_monitor(search, args.engine, recipient=args.email, send=not args.no_email,
                    record=True, low_eur=args.low_eur, high_eur=args.high_eur)
    return 2 if r.failure else 0


if __name__ == "__main__":
    sys.exit(main())
