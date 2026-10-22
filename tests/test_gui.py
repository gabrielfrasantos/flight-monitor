import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

import flight_monitor as fm  # noqa: E402
import flight_monitor_gui as gui  # noqa: E402


@pytest.fixture
def window(data_dir):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = gui.MainWindow()
    yield w
    w.shutdown()
    w.deleteLater()
    app.processEvents()


def test_window_shows_saved_settings_and_saves_back(data_dir, window):
    assert window._values() == {**fm.DEFAULTS, "airline": "KL"}

    window._airline_combo.setCurrentText("KL,af")
    window._stops_combo.setCurrentIndex(window._stops_combo.findData("any"))
    window._engine_combo.setCurrentIndex(window._engine_combo.findData("google-scrape"))
    window._currency_checks["BRL"].setChecked(False)
    window._save_defaults()

    saved = fm.load_settings()
    assert (saved["airline"], saved["max_stops"], saved["engine"], saved["currencies"]) == (
        "KL,AF",
        "any",
        "google-scrape",
        "EUR",
    )
    search = window._build_search()
    assert search.airlines == ("KL", "AF") and search.max_stops is None


def test_key_field_follows_engine(window, monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "secret")
    assert not window._key_edit.isEnabled()
    window._engine_combo.setCurrentIndex(window._engine_combo.findData("serpapi"))
    assert window._key_edit.isEnabled() and window._key_edit.text() == "secret"
    assert "118 request" in window._engine_about.text()


def test_typed_key_survives_date_changes(window):
    window._engine_combo.setCurrentIndex(window._engine_combo.findData("ignav"))
    window._key_edit.setText("typed")
    window._stay_spin.setValue(10)
    assert window._key_edit.text() == "typed"


def test_bad_saved_dates_fall_back_to_defaults(data_dir):
    fm.save_settings({**fm.DEFAULTS, "start": "01-12-2026"})
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = gui.MainWindow()
    try:
        assert w._values()["start"] == fm.DEFAULTS["start"]
    finally:
        w.shutdown()
        w.deleteLater()
        app.processEvents()
