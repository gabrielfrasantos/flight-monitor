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

Defaults: built in (DEFAULTS below) < data/settings.json (written by the GUI)
< command-line arguments.

Files live in app_home(): next to this script in a checkout, or the per-user
app-data folder for the installed app (%APPDATA%\flight-monitor on Windows).

Email: SMTP account in .env (or the GUI's Email settings). Gmail by default:
  SMTP_USER=you@gmail.com
  SMTP_PASSWORD=xxxxxxxxxxxxxxxx           # 16-char Google App Password
  REPORT_EMAIL=you@example.com             # recipient; defaults to SMTP_USER
  SMTP_HOST=smtp.gmail.com  SMTP_PORT=465  # 465 = SSL, other ports = STARTTLS
GMAIL_USER / GMAIL_APP_PASSWORD (older names) still work.

Run:  python flight_monitor.py            (uses defaults)
      python flight_monitor.py --help     (all overridable parameters)
      python flight_monitor_gui.py        (the same, from a window)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import smtplib
import ssl
import statistics
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from engines import AUTO, AUTO_ORDER, ENGINES, Offer, Search, sweep

__version__ = "0.1.0"  # x-release-please-version

FROZEN = bool(getattr(sys, "frozen", False))  # running from the installed (PyInstaller) app

# --------------------------------------------------------------------------- #
# Defaults -- overridden by data/settings.json, then by CLI args.
# --------------------------------------------------------------------------- #
DEFAULTS: dict[str, Any] = {
    "origin": "AMS",
    "destination": "GRU",
    "airline": "KL",  # carrier code(s), comma-separated; "ANY" = all
    "max_stops": "0",  # 0 = direct only; 1 = up to one stopover; "any"
    "start": "2026-12-01",
    "end": "2027-01-28",
    "stay_days": 15,
    "step_days": 1,
    "currencies": "EUR,BRL",
    "engine": AUTO,
    "low_eur": 1000.0,
    "high_eur": 1250.0,
    "email_when": "always",  # "always", "low" (LOW, new lows, problems) or "never"
    "auto_run": FROZEN,  # the installed app runs the monitor itself
    "run_times": "08:00,20:00",
}
EMAIL_WHEN = ("always", "low", "never")

# --------------------------------------------------------------------------- #
# Fixed (non-parameter) config
# --------------------------------------------------------------------------- #
ADULTS = 1

MIN_HISTORY_RUNS = 6  # runs of fixed thresholds before auto median
BAND = 0.10  # +/-10% band around the median for LOW/HIGH

RUN_GRACE = dt.timedelta(hours=6)  # a run missed while the PC slept still happens this late
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def app_home() -> Path:
    """Where .env and data/ live.

    A checkout keeps them next to the scripts (the Task Scheduler setup relies on
    that). The installed app can't write to its install folder, so it uses the
    per-user app-data folder. FLIGHT_MONITOR_HOME overrides both, e.g. to point
    the installed app at a checkout's existing history.
    """
    if os.environ.get("FLIGHT_MONITOR_HOME"):
        return Path(os.environ["FLIGHT_MONITOR_HOME"]).expanduser()
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "flight-monitor"


HOME = app_home()
ENV_FILE = HOME / ".env"
DATA_DIR = HOME / "data"
HISTORY_CSV = DATA_DIR / "price_history.csv"  # every offer, every run
RUNS_CSV = DATA_DIR / "run_summary.csv"  # one row per run (for median)
BEST_JSON = DATA_DIR / "best_prices.json"  # all-time low per currency
SETTINGS_JSON = DATA_DIR / "settings.json"  # saved defaults (GUI)
STATE_JSON = DATA_DIR / "state.json"  # app bookkeeping (last automatic run)
LOG_FILE = DATA_DIR / "monitor.log"

logger = logging.getLogger("flight_monitor")


# --------------------------------------------------------------------------- #
# Parameters (CLI, with defaults)
# --------------------------------------------------------------------------- #
def parse_airlines(text: str) -> tuple[str, ...]:
    codes = tuple(c.strip().upper() for c in str(text).replace("+", ",").split(",") if c.strip())
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


def parse_run_times(text: str) -> list[dt.time]:
    """ "08:00, 20:00" -> sorted times of day."""
    times = set()
    for part in str(text).replace(";", ",").split(","):
        if part.strip():
            times.add(dt.datetime.strptime(part.strip(), "%H:%M").time())
    if not times:
        raise ValueError("at least one run time is needed, e.g. 08:00,20:00")
    return sorted(times)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log(f"Ignoring {path.name}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def load_settings() -> dict:
    """DEFAULTS overlaid with data/settings.json, if present and readable."""
    settings = dict(DEFAULTS)
    settings.update({k: v for k, v in _read_json(SETTINGS_JSON).items() if k in DEFAULTS})
    if settings["engine"] not in (AUTO, *ENGINES):
        log(f"Unknown engine {settings['engine']!r} in {SETTINGS_JSON.name}; using {AUTO}.")
        settings["engine"] = AUTO
    if settings["email_when"] not in EMAIL_WHEN:
        settings["email_when"] = DEFAULTS["email_when"]
    try:
        parse_run_times(settings["run_times"])
    except ValueError:
        settings["run_times"] = DEFAULTS["run_times"]
    settings["auto_run"] = bool(settings["auto_run"])
    return settings


def save_settings(values: dict) -> None:
    """Update the given keys in data/settings.json, keeping the others."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = _read_json(SETTINGS_JSON)
    current.update({k: values[k] for k in DEFAULTS if k in values})
    SETTINGS_JSON.write_text(json.dumps(current, indent=2), encoding="utf-8")


def load_state() -> dict:
    return _read_json(STATE_JSON)


def save_state(**values) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    state.update(values)
    STATE_JSON.write_text(json.dumps(state, indent=2), encoding="utf-8")


def due_run(
    now: dt.datetime, times: list[dt.time], last_run: dt.datetime | None
) -> dt.datetime | None:
    """The scheduled slot to run now, if any: the latest one at or before now that
    hasn't run yet and is less than RUN_GRACE old (so a PC that slept through
    08:00 still runs at wake-up, once)."""
    slots = [
        dt.datetime.combine(now.date() + dt.timedelta(days=d), t) for d in (-1, 0) for t in times
    ]
    past = [s for s in slots if s <= now and now - s < RUN_GRACE]
    if not past:
        return None
    slot = max(past)
    return None if last_run is not None and last_run >= slot else slot


def next_run(now: dt.datetime, times: list[dt.time]) -> dt.datetime:
    return min(
        dt.datetime.combine(now.date() + dt.timedelta(days=d), t)
        for d in (0, 1)
        for t in times
        if dt.datetime.combine(now.date() + dt.timedelta(days=d), t) > now
    )


def parse_args(argv=None, settings: dict | None = None) -> argparse.Namespace:
    s = settings if settings is not None else load_settings()
    p = argparse.ArgumentParser(description="Round-trip flight price monitor.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--origin", default=s["origin"], help=f"Origin airport IATA code (default {s['origin']})."
    )
    p.add_argument(
        "--destination",
        default=s["destination"],
        help=f"Destination airport IATA code (default {s['destination']}).",
    )
    p.add_argument(
        "--airline",
        type=parse_airlines,
        default=s["airline"],
        help=f"Airline code(s), comma-separated, or ANY for all (default {s['airline']}).",
    )
    p.add_argument(
        "--max-stops",
        type=parse_stops,
        default=s["max_stops"],
        help=f"Max stopovers per leg: 0 = direct, 1, 2, or 'any' (default {s['max_stops']}).",
    )
    p.add_argument(
        "--start",
        default=s["start"],
        help=f"First outbound date, YYYY-MM-DD (default {s['start']}).",
    )
    p.add_argument(
        "--end", default=s["end"], help=f"Last outbound date, YYYY-MM-DD (default {s['end']})."
    )
    p.add_argument(
        "--stay-days",
        type=int,
        default=s["stay_days"],
        help=f"Nights at destination / return offset (default {s['stay_days']}).",
    )
    p.add_argument(
        "--step-days",
        type=int,
        default=s["step_days"],
        help=f"Days between probed outbound dates (default {s['step_days']}).",
    )
    p.add_argument(
        "--currencies",
        type=parse_currencies,
        default=s["currencies"],
        help=f"Comma-separated currencies (default {s['currencies']}).",
    )
    p.add_argument(
        "--engine",
        choices=[AUTO, *ENGINES],
        default=s["engine"],
        help=f"Price source (default {s['engine']}); see --list-engines.",
    )
    p.add_argument(
        "--list-engines", action="store_true", help="Show the price engines, free first, and exit."
    )
    p.add_argument(
        "--email",
        default="",
        help="Report recipient (default REPORT_EMAIL, else the sending account).",
    )
    p.add_argument(
        "--email-when",
        choices=EMAIL_WHEN,
        default=s["email_when"],
        help="Email every run, only LOW prices / new lows / problems, or never "
        f"(default {s['email_when']}).",
    )
    p.add_argument(
        "--low-eur",
        type=float,
        default=s["low_eur"],
        help=f"Fixed-mode: EUR at/below this = LOW (default {s['low_eur']:.0f}).",
    )
    p.add_argument(
        "--high-eur",
        type=float,
        default=s["high_eur"],
        help=f"Fixed-mode: EUR at/above this = HIGH (default {s['high_eur']:.0f}).",
    )
    p.add_argument("--no-email", action="store_true", help="Skip sending email (log only).")
    return p.parse_args(argv)


def build_search(
    origin: str,
    destination: str,
    airlines: tuple[str, ...],
    max_stops: int | None,
    start: dt.date,
    end: dt.date,
    stay_days: int,
    step_days: int,
    currencies: tuple[str, ...],
) -> Search:
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
    return Search(
        origin=origin,
        destination=destination,
        start=start,
        end=end,
        stay_days=stay_days,
        airlines=airlines,
        max_stops=max_stops,
        step_days=step_days,
        currencies=currencies,
        adults=ADULTS,
    )


def search_from_settings(s: dict) -> Search:
    """Validated Search from settings values (load_settings() or the GUI's fields)."""
    return build_search(
        s["origin"],
        s["destination"],
        parse_airlines(s["airline"]),
        parse_stops(s["max_stops"]),
        dt.date.fromisoformat(s["start"]),
        dt.date.fromisoformat(s["end"]),
        int(s["stay_days"]),
        int(s["step_days"]),
        parse_currencies(s["currencies"]),
    )


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
RUN_FIELDS = [
    "run_time",
    "light",
    "mode",
    "baseline_median",
    "low_eur",
    "low_eur_depart",
    "low_eur_return",
    "low_brl",
    "low_brl_depart",
    "low_brl_return",
]


def offer_row(o: Offer, checked_at: str) -> dict:
    return {
        "checked_at": checked_at,
        "currency": o.currency,
        "depart": o.depart.isoformat(),
        "return": o.ret.isoformat(),
        "price": o.price,
        "out_dep_time": o.out_dep_time,
    }


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
LIGHT_EMOJI = {"LOW": "\U0001f7e2", "NORMAL": "\U0001f7e1", "HIGH": "\U0001f534"}
CURRENCY_SIGN = {"EUR": "€", "BRL": "R$ ", "USD": "$", "GBP": "£"}


def money(currency: str, amount: float) -> str:
    return f"{CURRENCY_SIGN.get(currency, currency + ' ')}{amount:.0f}"


@dataclass
class EmailConfig:
    user: str = ""  # sending account
    password: str = ""
    recipient: str = ""
    host: str = SMTP_HOST
    port: int = SMTP_PORT

    @property
    def complete(self) -> bool:
        return bool(self.user and self.password)


def email_config() -> EmailConfig:
    """The email account from the environment / .env (GMAIL_* are the older names)."""
    user = os.environ.get("SMTP_USER") or os.environ.get("GMAIL_USER", "")
    try:
        port = int(os.environ.get("SMTP_PORT") or SMTP_PORT)
    except ValueError:
        port = SMTP_PORT
    return EmailConfig(
        user=user,
        password=os.environ.get("SMTP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD", ""),
        recipient=os.environ.get("REPORT_EMAIL") or user,
        host=os.environ.get("SMTP_HOST") or SMTP_HOST,
        port=port,
    )


def save_email_config(cfg: EmailConfig) -> None:
    for key, value in (
        ("SMTP_USER", cfg.user),
        ("SMTP_PASSWORD", cfg.password),
        ("REPORT_EMAIL", cfg.recipient),
        ("SMTP_HOST", cfg.host),
        ("SMTP_PORT", str(cfg.port)),
    ):
        set_env_value(key, value)


def send_email(to_addr: str, subject: str, body: str, cfg: EmailConfig | None = None) -> None:
    """Send via SMTP: SSL on port 465, STARTTLS otherwise. Raises on SMTP errors."""
    cfg = cfg or email_config()
    if not cfg.complete:
        log(
            "Email skipped: no email account set (GUI Email settings, or SMTP_USER / "
            "SMTP_PASSWORD in .env)."
        )
        return
    to_addr = to_addr or cfg.recipient or cfg.user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg.user
    msg["To"] = to_addr
    ctx = ssl.create_default_context()
    password = cfg.password.replace(" ", "")  # Google shows App Passwords in groups of 4
    if cfg.port == 465:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=30) as s:
            s.login(cfg.user, password)
            s.sendmail(cfg.user, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(cfg.user, password)
            s.sendmail(cfg.user, [to_addr], msg.as_string())
    log(f"Email sent to {to_addr}.")


def wants_email(r: RunResult, when: str) -> bool:
    """Report policy: every run, only LOW / new all-time lows / problems, or never."""
    if when == "never":
        return False
    if when == "low":
        return r.light == "LOW" or r.new_low or not r.best or bool(r.failure)
    return True


def _send_quietly(to_addr: str, subject: str, body: str) -> None:
    try:
        send_email(to_addr, subject, body)
    except Exception as e:
        log(f"Email FAILED: {e}")


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    search: Search
    engine: str = ""
    offers: list[Offer] = field(default_factory=list)
    errors: int = 0
    best: dict[str, Offer] = field(default_factory=dict)  # cheapest per currency
    light: str = ""  # LOW / NORMAL / HIGH; "" without an EUR price
    mode: str = ""
    median: float | None = None
    new_low: bool = False  # beat the all-time EUR low
    failure: str = ""  # why the sweep was cut short (EngineError)
    cancelled: bool = False


def engine_label(key: str) -> str:
    """Label of an engine key, or of "+"-joined keys ("auto" falling back per currency)."""
    return " + ".join(ENGINES[k].label if k in ENGINES else k for k in key.split("+"))


def format_report(r: RunResult, low_eur: float, high_eur: float) -> tuple[str, str]:
    """(subject, body) of the report email."""
    s = r.search
    head = r.best.get("EUR") or next(iter(r.best.values()))
    tag = r.light or "INFO"
    subject = (
        f"[{tag}] {s.origin}-{s.destination} {head.currency} {head.price:.0f} (out {head.depart})"
    )
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
        lines.append(
            f"Baseline: rolling median of past runs = €{r.median:.0f} (±{int(BAND * 100)}% band)"
            if r.mode == "auto-median"
            else f"Baseline: fixed thresholds LOW≤€{low_eur:.0f} / HIGH≥€{high_eur:.0f}"
        )
        lines.append("")
    for cur in s.currencies:
        o = r.best.get(cur)
        if not o:
            continue
        newlow = " *NEW ALL-TIME LOW*" if cur == "EUR" and r.new_low else ""
        stops = "" if o.stops is None else (", direct" if o.stops == 0 else f", {o.stops} stop(s)")
        airline = f", {o.airline}" if o.airline else ""
        lines += [
            f"Cheapest {cur}: {money(cur, o.price)}{newlow}",
            f"   outbound {o.depart} (dep {o.out_dep_time}{airline}{stops})  /  return {o.ret}",
            "",
        ]
    lines += [
        f"Offers scanned this run: {len(r.offers)}  |  errors: {r.errors}",
        f"Data: {HISTORY_CSV}",
    ]
    return subject, "\n".join(lines)


def run_monitor(
    search: Search,
    engine: str = AUTO,
    *,
    recipient: str = "",
    send: bool = True,
    email_when: str = "always",
    record: bool = True,
    low_eur: float = DEFAULTS["low_eur"],
    high_eur: float = DEFAULTS["high_eur"],
    api_keys: dict[str, str] | None = None,
    stop: threading.Event | None = None,
    on_engine: Callable[[str], None] | None = None,
    on_offer: Callable[[Offer], None] | None = None,
) -> RunResult:
    """One monitor run: sweep, then record, classify and report. Shared by CLI and GUI."""
    r = RunResult(search, engine=engine)
    route = search.label
    log(
        f"Run start. {route}, {search.stay_days}n stay, outbound {search.start}..{search.end} "
        f"step {search.step_days}d, currencies {list(search.currencies)}, engine {engine}."
    )
    sw = sweep(search, engine, api_keys=api_keys, stop=stop, on_engine=on_engine, on_offer=on_offer)
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
        if send and wants_email(r, email_when):
            _send_quietly(
                recipient,
                f"[WARN] {search.origin}-{search.destination} monitor: no offers found",
                f"Run at {dt.datetime.now():%Y-%m-%d %H:%M} found no {route} offers "
                f"via {engine_label(r.engine)} ({r.errors} errors). "
                + (f"{r.failure} " if r.failure else "")
                + "Possible scraper/consent issue — check monitor.log.",
            )
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
            append_rows(
                RUNS_CSV,
                RUN_FIELDS,
                [
                    {
                        "run_time": now,
                        "light": r.light,
                        "mode": r.mode,
                        "baseline_median": f"{r.median:.0f}" if r.median is not None else "",
                        "low_eur": eur.price,
                        "low_eur_depart": eur.depart.isoformat(),
                        "low_eur_return": eur.ret.isoformat(),
                        "low_brl": brl.price if brl else "",
                        "low_brl_depart": brl.depart.isoformat() if brl else "",
                        "low_brl_return": brl.ret.isoformat() if brl else "",
                    }
                ],
            )

    summary = "  ".join(f"{o.currency} {o.price:.0f} ({o.depart})" for o in r.best.values())
    log(f"Cheapest: {summary}" + (f" light={r.light} [{r.mode}]" if r.light else ""))
    if send and wants_email(r, email_when):
        _send_quietly(recipient, *format_report(r, low_eur, high_eur))
    elif send:
        log(f"No email: {r.light or 'no light'} and email_when={email_when}.")

    log(f"Run done. {len(r.offers)} offers, {r.errors} errors.")
    return r


def list_engines() -> str:
    rows = [
        f"  {AUTO:<16} free engines in order ({', '.join(AUTO_ORDER)}), falling back per currency"
    ]
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
    try:
        search = build_search(
            args.origin,
            args.destination,
            args.airline,
            args.max_stops,
            dt.date.fromisoformat(args.start),
            dt.date.fromisoformat(args.end),
            args.stay_days,
            args.step_days,
            args.currencies,
        )
    except ValueError as e:
        log(f"Invalid parameters: {e}")
        return 1
    r = run_monitor(
        search,
        args.engine,
        recipient=args.email,
        send=not args.no_email,
        email_when=args.email_when,
        record=True,
        low_eur=args.low_eur,
        high_eur=args.high_eur,
    )
    return 2 if r.failure else 0


if __name__ == "__main__":
    sys.exit(main())
