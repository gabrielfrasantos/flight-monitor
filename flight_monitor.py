#!/usr/bin/env python3
"""
Flight price monitor: AMS <-> GRU, round trip, direct KLM only.

Each run sweeps every outbound date in the window (default 01-12-2026 ..
28-01-2027), each with a +STAY nights return, in EUR and BRL, keeps the
cheapest KLM-direct offer, classifies it with a traffic light
(LOW / NORMAL / HIGH), and emails the result.

Traffic light ("both" mode):
  - First MIN_HISTORY_RUNS runs: fixed EUR thresholds (--low-eur / --high-eur).
  - After that: rolling median of past runs' lowest EUR price.
      LOW  = current <= median * (1 - BAND)
      HIGH = current >= median * (1 + BAND)
      else NORMAL

Data source: Google Flights via `fast-flights` (no API key). EU consent wall
bypassed with a SOCS cookie.

Email: Gmail SMTP + App Password. Put creds in a .env file next to this script:
  GMAIL_USER=you@example.com
  GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx      # 16-char Google App Password

Set the report recipient with --email or the REPORT_EMAIL environment variable.

Run:  python flight_monitor.py            (uses defaults below)
      python flight_monitor.py --help     (all overridable parameters)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import smtplib
import ssl
import statistics
import sys
import time
import datetime as dt
from email.mime.text import MIMEText
from pathlib import Path

from primp import Client
from fast_flights import FlightQuery, create_query, Passengers
from fast_flights.fetcher import URL
from fast_flights.parser import parse

# --------------------------------------------------------------------------- #
# Fixed (non-parameter) config
# --------------------------------------------------------------------------- #
ORIGIN = "AMS"
DESTINATION = "GRU"
AIRLINE = "KL"                 # KLM carrier code
CURRENCIES = ["EUR", "BRL"]
ADULTS = 1
SEAT = "economy"

SLEEP_MIN = 1.2               # politeness between requests (seconds)
SLEEP_MAX = 2.6

MIN_HISTORY_RUNS = 6          # runs of fixed thresholds before auto median
BAND = 0.10                   # +/-10% band around the median for LOW/HIGH

# Google consent-accept cookie (bypasses consent.google.com EU wall).
# Refresh if runs abort "consent cookie invalid": accept cookies on google.com
# in a browser, copy the SOCS cookie value here.
SOCS_COOKIE = "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
HISTORY_CSV = DATA_DIR / "price_history.csv"      # every offer, every run
RUNS_CSV = DATA_DIR / "run_summary.csv"           # one row per run (for median)
BEST_JSON = DATA_DIR / "best_prices.json"         # all-time low per currency
LOG_FILE = DATA_DIR / "monitor.log"


# --------------------------------------------------------------------------- #
# Parameters (CLI, with defaults)
# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KLM-direct AMS<->GRU price monitor.")
    p.add_argument("--start", default="2026-12-01",
                   help="First outbound date, YYYY-MM-DD (default 2026-12-01).")
    p.add_argument("--end", default="2027-01-28",
                   help="Last outbound date, YYYY-MM-DD (default 2027-01-28).")
    p.add_argument("--stay-days", type=int, default=15,
                   help="Nights in Brazil / return offset (default 15).")
    p.add_argument("--step-days", type=int, default=1,
                   help="Days between probed outbound dates (default 1).")
    p.add_argument("--email", default=os.environ.get("REPORT_EMAIL", "you@example.com"),
                   help="Recipient for the report (or set REPORT_EMAIL env var).")
    p.add_argument("--low-eur", type=float, default=1000.0,
                   help="Fixed-mode: EUR at/below this = LOW (default 1000).")
    p.add_argument("--high-eur", type=float, default=1250.0,
                   help="Fixed-mode: EUR at/above this = HIGH (default 1250).")
    p.add_argument("--no-email", action="store_true",
                   help="Skip sending email (log only).")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_dotenv() -> None:
    envf = HERE / ".env"
    if not envf.exists():
        return
    for line in envf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def make_client() -> Client:
    c = Client(impersonate="chrome_145", impersonate_os="macos",
               referer=True, cookie_store=True)
    c.set_cookies("https://www.google.com", {"SOCS": SOCS_COOKIE})
    return c


def build_query(depart: dt.date, ret: dt.date, currency: str):
    return create_query(
        flights=[
            FlightQuery(date=depart.isoformat(), from_airport=ORIGIN,
                        to_airport=DESTINATION, max_stops=0, airlines=[AIRLINE]),
            FlightQuery(date=ret.isoformat(), from_airport=DESTINATION,
                        to_airport=ORIGIN, max_stops=0, airlines=[AIRLINE]),
        ],
        trip="round-trip", seat=SEAT, passengers=Passengers(adults=ADULTS),
        currency=currency, max_stops=0,
    )


def is_direct_klm(f) -> bool:
    """Single-leg KLM flight AMS->GRU (belt & suspenders on top of API filter)."""
    if len(f.flights) != 1 or f.type != AIRLINE:
        return False
    leg = f.flights[0]
    return leg.from_airport.code == ORIGIN and leg.to_airport.code == DESTINATION


def cheapest_offer(client: Client, depart: dt.date, ret: dt.date, currency: str):
    q = build_query(depart, ret, currency)
    html = client.get(URL, params=q.params()).text
    if "consent.google.com" in html[:400]:
        raise RuntimeError("Hit Google consent wall (refresh SOCS_COOKIE).")
    best = None
    for f in parse(html):
        if not is_direct_klm(f):
            continue
        price = float(f.price)
        if best is None or price < best["price"]:
            leg = f.flights[0]
            best = {
                "price": price, "currency": currency,
                "out_dep_time": f"{leg.departure.time[0]:02d}:{leg.departure.time[1]:02d}",
            }
    return best


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


def append_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def update_best(rows: list[dict]) -> dict:
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
    BEST_JSON.write_text(json.dumps(best, indent=2), encoding="utf-8")
    return improved


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
LIGHT_EMOJI = {"LOW": "\U0001F7E2", "NORMAL": "\U0001F7E1", "HIGH": "\U0001F534"}


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
# Main
# --------------------------------------------------------------------------- #
def daterange(start: dt.date, end: dt.date, step: int):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=step)


def main(argv=None) -> int:
    args = parse_args(argv)
    load_dotenv()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    stay = args.stay_days

    client = make_client()
    log(f"Run start. {ORIGIN}<->{DESTINATION} KLM direct, {stay}n stay, "
        f"outbound {start}..{end} step {args.step_days}d, currencies {CURRENCIES}.")

    all_rows: list[dict] = []
    run_best: dict = {}
    errors = 0

    for depart in daterange(start, end, args.step_days):
        ret = depart + dt.timedelta(days=stay)
        for currency in CURRENCIES:
            try:
                offer = cheapest_offer(client, depart, ret, currency)
            except Exception as e:
                errors += 1
                log(f"  {depart} {currency}: error {e}")
                if "consent wall" in str(e):
                    log("Aborting: consent cookie invalid.")
                    return 2
                time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
                continue
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
            if not offer:
                continue
            row = {
                "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
                "currency": currency, "depart": depart.isoformat(),
                "return": ret.isoformat(), "price": offer["price"],
                "out_dep_time": offer["out_dep_time"],
            }
            all_rows.append(row)
            if currency not in run_best or row["price"] < run_best[currency]["price"]:
                run_best[currency] = row

    if not all_rows or "EUR" not in run_best:
        log(f"No KLM direct offers found this run. ({errors} errors)")
        # Still notify so silence never hides a scraper break.
        if not args.no_email:
            send_email(args.email, "[WARN] KLM AMS-GRU monitor: no offers found",
                       f"Run at {dt.datetime.now():%Y-%m-%d %H:%M} found no KLM "
                       f"direct offers ({errors} errors). Possible scraper/consent "
                       f"issue — check monitor.log.")
        return 0

    append_rows(HISTORY_CSV, HIST_FIELDS, all_rows)
    improved = update_best(all_rows)

    eur = run_best["EUR"]
    brl = run_best.get("BRL")
    light, mode, med = classify(eur["price"], args.low_eur, args.high_eur)

    # Record this run's summary (used for future median baseline).
    run_row = {
        "run_time": dt.datetime.now().isoformat(timespec="seconds"),
        "light": light, "mode": mode,
        "baseline_median": f"{med:.0f}" if med is not None else "",
        "low_eur": eur["price"], "low_eur_depart": eur["depart"],
        "low_eur_return": eur["return"],
        "low_brl": brl["price"] if brl else "", "low_brl_depart": brl["depart"] if brl else "",
        "low_brl_return": brl["return"] if brl else "",
    }
    append_rows(RUNS_CSV, RUN_FIELDS, [run_row])

    # ---- Report ----
    emoji = LIGHT_EMOJI.get(light, "")
    subject = (f"[{light}] KLM AMS-GRU EUR {eur['price']:.0f} "
               f"(out {eur['depart']})")

    baseline_line = (f"Baseline: rolling median of past runs = €{med:.0f} "
                     f"(±{int(BAND*100)}% band)"
                     if mode == "auto-median"
                     else f"Baseline: fixed thresholds LOW≤€{args.low_eur:.0f} / "
                          f"HIGH≥€{args.high_eur:.0f}")
    newlow = " *NEW ALL-TIME LOW*" if "EUR" in improved else ""

    body = "\n".join([
        f"KLM direct, Amsterdam (AMS) <-> Sao Paulo (GRU), {stay}-night stay.",
        f"Outbound window scanned: {start} .. {end} (step {args.step_days}d).",
        "",
        f"TRAFFIC LIGHT: {emoji} {light}   [{mode}]",
        baseline_line,
        "",
        f"Cheapest EUR: €{eur['price']:.0f}{newlow}",
        f"   outbound {eur['depart']} (dep {eur['out_dep_time']})  /  "
        f"return {eur['return']}",
    ])
    if brl:
        body += "\n".join([
            "",
            f"Cheapest BRL: R$ {brl['price']:.0f}",
            f"   outbound {brl['depart']} (dep {brl['out_dep_time']})  /  "
            f"return {brl['return']}",
        ])
    body += (f"\n\nOffers scanned this run: {len(all_rows)}  |  errors: {errors}"
             f"\nData: {HISTORY_CSV}")

    log(f"Cheapest: EUR {eur['price']:.0f} ({eur['depart']}) light={light} [{mode}]"
        + (f" BRL {brl['price']:.0f}" if brl else ""))
    if not args.no_email:
        try:
            send_email(args.email, subject, body)
        except Exception as e:
            log(f"Email FAILED: {e}")

    log(f"Run done. {len(all_rows)} offers, {errors} errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
