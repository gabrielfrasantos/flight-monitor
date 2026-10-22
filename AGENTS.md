# flight-monitor — Agent Rules (canonical)

Single source of truth for **Claude, Copilot, and sub-agents**. `CLAUDE.md` and `.github/copilot-instructions.md` point here.

Round-trip flight price monitor: sweeps a window of outbound dates through a selectable price engine, keeps the cheapest offer per currency, classifies it with a traffic light, records history and emails a report. Ships as a Windows app (PyInstaller + Inno Setup) that lives in the tray, starts with Windows and runs the monitor on its own schedule; the same scripts also run from a checkout (CLI, GUI, or Windows Task Scheduler calling `pythonw flight_monitor.py`). Not a PyPI package.

## Architecture

```
flight_monitor.py      — CLI entry point + run logic: settings/args → Search, run_monitor()
                         (sweep → history CSVs → traffic light → email), format_report()
engines.py             — Search / Offer, Engine base, the engines, ENGINES registry, sweep()
                         email account/policy, schedule rules (due_run), app_home()
flight_monitor_gui.py  — GUI entry point: MainWindow, SearchController (daemon thread),
                         EmailDialog, automatic runs, --minimized, --smoke-test
desktop.py             — icon, tray icon, single-instance guard, GitHub update check
flight-monitor.spec    — PyInstaller (one folder: dist/flight-monitor/)
installer/windows/     — Inno Setup script (per-user install, "start with Windows")
scripts/generate_icon.py — assets/icon.ico + icon.png from desktop.render_icon()
run_monitor.ps1        — Task Scheduler wrapper for a checkout (venv python if present)
tests/                 — pytest; no network, no API keys
data/                  — runtime only, gitignored: price_history.csv, run_summary.csv,
                         best_prices.json, settings.json, state.json, monitor.log
```

## Where files live

- `app_home()`: next to the scripts in a checkout; `%APPDATA%\flight-monitor` for the frozen app (its install folder is read-only); `FLIGHT_MONITOR_HOME` overrides. `.env` and `data/` live there. Never write next to `sys.executable`.
- Secrets (email password, API keys) go to `.env` only; settings, schedule and email policy to `data/settings.json`; app bookkeeping to `data/state.json`.

## Price engines

- Each engine answers: cheapest round trip per (outbound date, currency) honoring `Search` filters (airlines, max stops). Return `Offer`s; prices are round-trip totals.
- `ENGINES` is ordered **free first**. `auto` runs `AUTO_ORDER` (free only) and falls back **per currency**; it must never spend a paid quota.
- Per-date engines implement `fetch()`; the base `offers()` loop handles pacing (`pause`), stop requests and per-date error counting. Whole-window engines override `offers()`.
- `EngineError` = the whole engine is unusable (bad key, quota, consent wall, missing library). Any other exception = one bad date, counted and logged. `sweep()` never raises `EngineError`: partial offers are kept, the reason goes to `SweepResult.failure`.
- Keep `Search.accepts()` as a client-side check on top of each engine's own filters.
- API keys come from `.env` / environment (`key_env`); never hardcode or log them.
- Third-party imports (`fli`, `fast_flights`, `primp`) stay lazy inside engines so one missing library disables one engine, not the app.

## Data and compatibility

- The CSV headers (`HIST_FIELDS`, `RUN_FIELDS`) and `best_prices.json` shape are append-only history on the user's machine — don't change them without a migration.
- Defaults: `DEFAULTS` < `data/settings.json` (GUI) < CLI args. Automatic runs and the Task Scheduler setup depend on this. `save_settings()` updates only the keys it is given.
- Email: `SMTP_*` / `REPORT_EMAIL` in `.env` (`GMAIL_*` still read). `email_when` = always / low / never; a run with no offers or a cut-short engine counts as a problem and still emails unless `never`. SMTP failures are logged, never fatal to a run.
- Exit codes: 0 ok, 1 invalid parameters, 2 engine cut short. A run with no offers still emails `[WARN]`.

## GUI design

- Mirrors port-bridge: Qt owns the main thread; each search runs `run_monitor()` in a `threading.Thread` (daemon). Worker → Qt via a `queue.SimpleQueue` (log records, offers, result) drained by a 100 ms `QTimer`. No `qasync`, no widget access from the worker.
- `aboutToQuit` calls `shutdown()` so a running search stops first. With a tray, closing the window only hides it; Quit is in the tray menu.
- One instance per user (`desktop.SingleInstance`, QLocalServer): two copies would double every scheduled run and email.
- Automatic runs: a 30 s timer applies `fm.due_run()` to the saved `run_times` and `state.json`'s `last_auto_run`; they use the **saved** settings, record history and email per `email_when`. Missed slots run at wake-up within `RUN_GRACE`.
- GUI searches don't email or record history unless the user ticks the boxes.

## Cross-platform

- Primary target is Windows (Task Scheduler, `pythonw` with no console: `sys.stdout` may be `None`). Also runs on Linux/macOS.
- `pathlib` everywhere; files read/written as UTF-8. Log messages ASCII-safe (Windows consoles).

## Style

- PEP 8, line length 100, `ruff` (lint + format) authoritative; `mypy` on `engines.py` and `flight_monitor.py`.
- `from __future__ import annotations` at the top of every module; type annotations on public functions.
- Comments only for non-obvious *why*. No `TODO`/`FIXME`.
- Log through `log()` / the `flight_monitor` logger, not `print` (except CLI output like `--list-engines`).

## Testing

- `pytest`; `tests/conftest.py`'s `data_dir` fixture redirects every data file and `.env` to a temp dir.
- No network in tests: monkeypatch `_get_json` / `_post_json` / `sweep` with payloads shaped like the provider docs.
- GUI tests run with `QT_QPA_PLATFORM=offscreen`.
- Run: `python -m pytest`.

## Packaging

- PyInstaller one-folder build (`flight-monitor.spec`), windowed (`console=False`), no UPX. Engine libraries are imported lazily, so list them in `hiddenimports`.
- `flight-monitor.exe --smoke-test` must exit 0: CI runs it on the built folder and on a silent install of the installer.
- Inno Setup: per-user (`PrivilegesRequired=lowest`), startup shortcut with `--minimized` (task on by default), `CloseApplications=yes`, `[InstallDelete]` of `_internal` on upgrade. Keep `AppId` stable.

## Release

- Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `ci:`, …).
- `release-please` bumps `pyproject.toml`, `__version__` in `flight_monitor.py` and `.release-please-manifest.json`, writes `CHANGELOG.md`, and publishes a GitHub release; `build-installers.yml` then attaches the Windows installer. Nothing is published to PyPI.

## Assistant behavior

- Minimal prose. Report file paths + pass/fail.
- Don't add dependencies the scheduled run doesn't need to its hot path; keep engine libraries lazy.
- Never commit `.env` or anything under `data/`.
