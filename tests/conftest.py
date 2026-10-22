import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flight_monitor as fm  # noqa: E402


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, password):
        self.store[(service, user)] = password

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point every data file (and .env) at a temp dir."""
    d = tmp_path / "data"
    monkeypatch.setattr(fm, "DATA_DIR", d)
    monkeypatch.setattr(fm, "HISTORY_CSV", d / "price_history.csv")
    monkeypatch.setattr(fm, "RUNS_CSV", d / "run_summary.csv")
    monkeypatch.setattr(fm, "BEST_JSON", d / "best_prices.json")
    monkeypatch.setattr(fm, "SETTINGS_JSON", d / "settings.json")
    monkeypatch.setattr(fm, "STATE_JSON", d / "state.json")
    monkeypatch.setattr(fm, "HOME", tmp_path)
    monkeypatch.setattr(fm, "LOG_FILE", d / "monitor.log")
    monkeypatch.setattr(fm, "ENV_FILE", tmp_path / ".env")
    fake_keyring = FakeKeyring()
    monkeypatch.setattr(fm, "_keyring", lambda: fake_keyring)
    for key in (
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_HOST",
        "SMTP_PORT",
        "REPORT_EMAIL",
        "GMAIL_USER",
        "GMAIL_APP_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    return d
