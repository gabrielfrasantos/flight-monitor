# Flight Monitor — AMS ⇄ GRU, KLM direct

[![CI](https://github.com/gabrielfrasantos/flight-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/gabrielfrasantos/flight-monitor/actions/workflows/ci.yml)
[![CodeQL](https://github.com/gabrielfrasantos/flight-monitor/actions/workflows/codeql.yml/badge.svg)](https://github.com/gabrielfrasantos/flight-monitor/actions/workflows/codeql.yml)

Sweeps every outbound date **01-12-2026 → 28-01-2027** (step 1 day), each with a
**15-night** return, **direct KLM only**, prices in **EUR and BRL**. Picks the
lowest, classifies it with a **traffic light**, and **emails** the report.
Runs **twice a day** via Windows Task Scheduler.

Origin, destination, airline(s), stopovers, dates and the **price engine** are
all **configurable**, from the command line ([Parameters](#parameters)) or a
window ([GUI](#gui)). AMS⇄GRU/KLM/direct is just the scheduled default.

Data: Google Flights by default (free, no API key); other engines below.

## Price engines

Pick with `--engine` (or in the GUI). Free first:

| Engine | Source | Cost | Requests per default sweep |
|---|---|---|---|
| `auto` *(default)* | the free engines below, in order; each one covers the currencies the previous found nothing for | free | ~4 |
| `google-calendar` | Google Flights price calendar ([fli](https://github.com/punitarani/fli)) | free, no key | ~4 (whole window per call) |
| `google-scrape` | Google Flights result page (fast-flights) — the original engine | free, no key | ~118, 1–3 s apart |
| `travelpayouts` | Aviasales price cache: fares users found in the last ~48 h, **not live**; sparse on quiet dates | free token (`TRAVELPAYOUTS_TOKEN`) | ~118 |
| `ignav` | Ignav live fare API | 1,000 free requests once, then $2 / 1,000 (`IGNAV_API_KEY`) | ~118 |
| `serpapi` | Google Flights via SerpApi | 250 free searches/month, 50/hour (`SERPAPI_KEY`) | ~118 |

- `auto` never uses a paid engine; `travelpayouts` joins it once its token is set.
- `google-calendar` reads the whole window in one call per currency, then does
  one detailed lookup on the cheapest date (airline, stops, departure time).
  The other rows show the price only.
- For the keyed engines, put the key in `.env` (see `.env.example`) or paste it
  in the GUI. To stay inside a free tier, raise `--step-days` and/or use one
  currency: e.g. `--step-days 3 --currencies EUR` is ~20 requests.
- Not offered: **Amadeus** (self-service API shut down July 2026),
  **Kiwi Tequila** (invite only), **Skyscanner** (partners only).
- `python flight_monitor.py --list-engines` prints the list.

## GUI

```powershell
pythonw flight_monitor_gui.py      # or: python flight_monitor_gui.py (with console)
```
Set route, airline(s), stopovers, outbound window, stay, engine and currencies,
then **Search**. Results stream into the table (cheapest per currency
highlighted), with the traffic light and a live log. **Stop** cancels.

- **Airline:** pick from the list or type codes, e.g. `KL,AF`. "Any airline" = no filter.
- **Stopovers:** direct only, up to 1, up to 2, or any.
- **Email report** / **Add to history:** off by default, so one-off searches
  don't email you or skew the traffic-light baseline. The scheduled run always
  does both.
- **Save as default** writes the settings to `data\settings.json` (and the
  engine's API key to `.env`). The CLI and the scheduled task start from those
  defaults; see [Scheduler](#scheduler-already-registered).

## Traffic light

Classifies each run's lowest EUR price:
- 🟢 **LOW** · 🟡 **NORMAL** · 🔴 **HIGH**
- First 6 runs: fixed thresholds (`--low-eur` default 1000, `--high-eur` 1250).
- After 6 runs of history: auto — rolling **median** of past runs' lows, ±10% band.
- The light appears in the email **subject** (`[LOW]/[NORMAL]/[HIGH]`) so you can
  filter, and in the body with the color emoji.

## One-time setup

### 1. Dependencies (Python 3.10+, globally or in a venv)
```powershell
cd path\to\flight-monitor
python -m pip install -r requirements.txt
```
Re-run this after updating: the default engine and the GUI need the new
`flights` and `PySide6` packages.

### 2. Gmail App Password (for the email report)
1. The Google account needs **2-Step Verification ON**:
   https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords
   → app "Mail", device "Windows" → Google shows a **16-char** code.
3. Put it in `.env` (already stubbed with your address):
   ```
   GMAIL_USER=you@example.com
   GMAIL_APP_PASSWORD=the16charcode
   ```
   Spaces in the code are fine (stripped automatically).

Test email works:
```powershell
python flight_monitor.py --start 2026-12-01 --end 2026-12-02
```
You should get one email. (`--no-email` runs without sending.)

## Scheduler (already registered)

Task **`FlightMonitor_AMS_GRU`** runs daily at **08:00** and **20:00** as your
Windows user, hidden (via `pythonw.exe`, no console window). It pins
`--origin AMS --destination GRU --airline KL --max-stops 0`; everything else
(engine, dates, stay, currencies, thresholds) comes from the GUI's saved
defaults.

To let the GUI's **Save as default** control the route too, drop the pinned
arguments from the task once:
```powershell
$a = (Get-ScheduledTask FlightMonitor_AMS_GRU).Actions[0]
$a.Arguments = ($a.Arguments -replace '\s+--.*$', '')
Set-ScheduledTask -TaskName FlightMonitor_AMS_GRU -Action $a
```

**Auto-cleanup:** the triggers stop after the last outbound date (end boundary
2027-01-29) and Windows deletes the task ~1 day later
(`DeleteExpiredTaskAfter = P1D`) — no manual removal needed.

Manage it:
```powershell
Get-ScheduledTask FlightMonitor_AMS_GRU            # status
Start-ScheduledTask FlightMonitor_AMS_GRU          # run now
Get-ScheduledTaskInfo FlightMonitor_AMS_GRU        # last/next run, result
Unregister-ScheduledTask FlightMonitor_AMS_GRU -Confirm:$false   # remove early
```

## Parameters

```
python flight_monitor.py [options]
  --origin IATA          origin airport                       (default AMS)
  --destination IATA     destination airport                  (default GRU)
  --airline CODES        carrier code(s), e.g. KL or KL,AF; ANY=all (default KL)
  --max-stops N|any      0 = direct only, 1, 2, or any        (default 0)
  --start YYYY-MM-DD     first outbound date                  (default 2026-12-01)
  --end   YYYY-MM-DD     last outbound date                   (default 2027-01-28)
  --stay-days N          nights at destination                (default 15)
  --step-days N          gap between dates                    (default 1)
  --currencies LIST      e.g. EUR,BRL                         (default EUR,BRL)
  --engine NAME          price engine, see Price engines      (default auto)
  --list-engines         show the engines and exit
  --email ADDR           report recipient       (default from REPORT_EMAIL env, else GMAIL_USER)
  --low-eur N            fixed-mode LOW cutoff                (default 1000)
  --high-eur N           fixed-mode HIGH cutoff               (default 1250)
  --no-email             log only, don't send
```
Defaults shown are the built-in ones; `data\settings.json` (GUI **Save as
default**) overrides them, and command-line options override both.

Examples:
```powershell
# Any airline, up to 1 stop, London <-> New York, 7 nights
python flight_monitor.py --origin LHR --destination JFK --airline ANY --max-stops 1 --stay-days 7

# KLM or Air France, the original page-scrape engine
python flight_monitor.py --airline KL,AF --engine google-scrape
```
Exit code: 0 ok, 1 invalid parameters, 2 the engine was cut short (bad key,
quota, consent wall). A run with no offers still emails a `[WARN]` report.

## Output (in `data\`)

- `price_history.csv` — every offer, every run.
- `run_summary.csv` — one row per run (feeds the median baseline).
- `best_prices.json` — all-time low per currency.
- `settings.json` — defaults saved by the GUI.
- `monitor.log` — run log.

## Development

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest            # tests: no network, no API keys
ruff check . ; ruff format . ; mypy
```
CI (GitHub Actions) runs lint, format and type checks, then the tests on
Windows and Linux for Python 3.10–3.13; CodeQL scans weekly and on PRs;
Dependabot keeps dependencies and actions current. The keyed engines
(`travelpayouts`, `ignav`, `serpapi`) are tested against their documented
response shapes, not live.

Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `docs:`, `ci:`, …). On `main`, `release-please` opens a
release PR that bumps the version (`pyproject.toml`, `flight_monitor.py`) and
`CHANGELOG.md`; merging it tags the version and publishes a GitHub release.
`python flight_monitor.py --version` shows the running version.

Agent and contributor rules: [AGENTS.md](AGENTS.md).

## Caveats

- **The Google engines are scrapers, not an official API.** If Google changes
  something: `pip install -U flights fast-flights`. `auto` falls back to the
  next free engine for any currency the first one fails on.
- **Consent wall** (`google-scrape` only): bypassed with a `SOCS` cookie
  (`SOCS_COOKIE` in `engines.py`). If a run stops at the "consent wall", accept
  cookies on google.com in a browser and copy the new `SOCS` value there.
- **Soft-blocking / HTTP 429:** Google rate-limits busy IPs. `google-calendar`
  needs ~4 requests a run; `google-scrape` ~118 (raise `--step-days`, or the
  `pause` in `engines.py`, if it returns nothing).
- Prices are the **round-trip total** in each currency (real fares, not
  FX-converted). `ignav` prices by point of sale: EUR as the Netherlands, BRL as
  Brazil.
