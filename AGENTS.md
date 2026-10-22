# flight-monitor — Agent Rules (canonical)

Single source of truth for **Claude, Copilot, and sub-agents**. `CLAUDE.md` and `.github/copilot-instructions.md` point here.

Round-trip flight price monitor: sweeps a window of outbound dates through a selectable price engine, keeps the cheapest offer per currency, classifies it with a traffic light, records history and emails a report. Runs twice a day from Windows Task Scheduler (`pythonw flight_monitor.py`), on demand from the CLI, or from a PySide6 GUI. Plain scripts run from a checkout — not an installable package.

## Architecture

```
flight_monitor.py      — CLI entry point + run logic: settings/args → Search, run_monitor()
                         (sweep → history CSVs → traffic light → email), format_report()
engines.py             — Search / Offer, Engine base, the engines, ENGINES registry, sweep()
flight_monitor_gui.py  — GUI entry point: MainWindow + SearchController (daemon thread)
run_monitor.ps1        — Task Scheduler wrapper (venv python if present)
tests/                 — pytest; no network, no API keys
data/                  — runtime only, gitignored: price_history.csv, run_summary.csv,
                         best_prices.json, settings.json, monitor.log
```

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
- Defaults: `DEFAULTS` < `data/settings.json` (GUI "Save as default") < CLI args. The scheduled task depends on this.
- Exit codes: 0 ok, 1 invalid parameters, 2 engine cut short. A run with no offers still emails `[WARN]`.

## GUI design

- Mirrors port-bridge: Qt owns the main thread; each search runs `run_monitor()` in a `threading.Thread` (daemon). Worker → Qt via a `queue.SimpleQueue` (log records, offers, result) drained by a 100 ms `QTimer`. No `qasync`, no widget access from the worker.
- `aboutToQuit` / `closeEvent` call `shutdown()` so a running search stops first.
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

## Release

- Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `ci:`, …).
- `release-please` bumps `pyproject.toml`, `__version__` in `flight_monitor.py` and `.release-please-manifest.json`, writes `CHANGELOG.md`, and publishes a GitHub release. Nothing is published to PyPI.

## Assistant behavior

- Minimal prose. Report file paths + pass/fail.
- Don't add dependencies the scheduled run doesn't need to its hot path; keep engine libraries lazy.
- Never commit `.env` or anything under `data/`.
