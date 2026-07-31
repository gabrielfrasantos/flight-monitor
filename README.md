# Flight Monitor — AMS ⇄ GRU, KLM direct

Sweeps every outbound date **01-12-2026 → 28-01-2027** (step 1 day), each with a
**15-night** return, **direct KLM only**, prices in **EUR and BRL**. Picks the
lowest, classifies it with a **traffic light**, and **emails** the report.
Runs **twice a day** via Windows Task Scheduler.

Origin, destination, airline, and stopovers are all **configurable** (see
[Parameters](#parameters)) — AMS⇄GRU/KLM/direct is just the scheduled default.

Data: Google Flights via `fast-flights` (no API key, free).

## Traffic light

Classifies each run's lowest EUR price:
- 🟢 **LOW** · 🟡 **NORMAL** · 🔴 **HIGH**
- First 6 runs: fixed thresholds (`--low-eur` default 1000, `--high-eur` 1250).
- After 6 runs of history: auto — rolling **median** of past runs' lows, ±10% band.
- The light appears in the email **subject** (`[LOW]/[NORMAL]/[HIGH]`) so you can
  filter, and in the body with the color emoji.

## One-time setup

### 1. Dependencies (already installed globally, or use a venv)
```powershell
cd path\to\flight-monitor
python -m pip install -r requirements.txt
```

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
`--origin AMS --destination GRU --airline KL --max-stops 0`.

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
  --origin IATA          origin airport         (default AMS)
  --destination IATA     destination airport    (default GRU)
  --airline CODE         carrier code, ANY=all  (default KL)
  --max-stops N          0 = direct only         (default 0)
  --start YYYY-MM-DD     first outbound date    (default 2026-12-01)
  --end   YYYY-MM-DD     last outbound date     (default 2027-01-28)
  --stay-days N          nights at destination  (default 15)
  --step-days N          gap between dates       (default 1)
  --email ADDR           report recipient       (default from REPORT_EMAIL env, else placeholder)
  --low-eur N            fixed-mode LOW cutoff   (default 1000)
  --high-eur N           fixed-mode HIGH cutoff  (default 1250)
  --no-email             log only, don't send
```
Examples:
```powershell
# Any airline, up to 1 stop, London <-> New York, 7 nights
python flight_monitor.py --origin LHR --destination JFK --airline ANY --max-stops 1 --stay-days 7
```
The scheduled task pins `--origin AMS --destination GRU --airline KL --max-stops 0`
in its `-Argument`; edit the task to change what runs automatically.

## Output (in `data\`)

- `price_history.csv` — every offer, every run.
- `run_summary.csv` — one row per run (feeds the median baseline).
- `best_prices.json` — all-time low per currency.
- `monitor.log` — run log.

## Caveats

- **Scraper, not an official API.** If Google changes its page: `pip install -U fast-flights`.
- **Consent wall:** bypassed with a `SOCS` cookie (`SOCS_COOKIE` in the script).
  If a run aborts "consent cookie invalid", accept cookies on google.com in a
  browser and copy the new `SOCS` value into the script.
- **Soft-blocking:** ~118 requests/run. If Google returns 0 offers everywhere,
  raise `--step-days` and the `SLEEP_*` values.
- Prices are the **round-trip total** in each currency (real fares, not FX-converted).
