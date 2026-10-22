# GitHub Copilot Instructions — flight-monitor

Canonical rules: **[AGENTS.md](../AGENTS.md)** (shared with Claude).

Essentials (full detail in AGENTS.md):
- **Engines** — `engines.py`, free first; `auto` = free engines only, per-currency fallback. `EngineError` = engine unusable; `sweep()` never raises it.
- **Run** — `flight_monitor.run_monitor()` is shared by CLI and GUI. Defaults: `DEFAULTS` < `data/settings.json` < CLI args.
- **Data** — CSV/JSON history formats are user data; don't change them without a migration. Never commit `.env` or `data/`.
- **GUI** — PySide6 on the main thread, run in a daemon thread, events via `queue.SimpleQueue` + `QTimer`. No `qasync`. Tray app, single instance, automatic runs from saved settings.
- **Files** — `app_home()`: checkout dir, or `%APPDATA%\flight-monitor` when frozen; email password in the OS credential store (`keyring`), other secrets in `.env`.
- **Packaging** — PyInstaller one-folder + Inno Setup (per-user, start with Windows); `--smoke-test` must pass.
- **Windows first** — `pythonw` (no stdout), Task Scheduler, UTF-8 files, `pathlib`.
- **Style** — PEP 8, 100-char lines, `ruff` lint + format authoritative, `mypy`. No code comments (tool directives only); docstrings instead.
- **Tests** — `python -m pytest`; no network, provider payloads mocked; GUI offscreen.
- **Release** — Conventional Commits + `release-please` (GitHub release + Windows installer).
- **Be terse** — minimal prose; report file paths + pass/fail.
