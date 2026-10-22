import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flight_monitor as fm  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point every data file (and .env) at a temp dir."""
    d = tmp_path / "data"
    monkeypatch.setattr(fm, "DATA_DIR", d)
    monkeypatch.setattr(fm, "HISTORY_CSV", d / "price_history.csv")
    monkeypatch.setattr(fm, "RUNS_CSV", d / "run_summary.csv")
    monkeypatch.setattr(fm, "BEST_JSON", d / "best_prices.json")
    monkeypatch.setattr(fm, "SETTINGS_JSON", d / "settings.json")
    monkeypatch.setattr(fm, "LOG_FILE", d / "monitor.log")
    monkeypatch.setattr(fm, "ENV_FILE", tmp_path / ".env")
    return d
