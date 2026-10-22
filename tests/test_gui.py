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
    values = window._values()
    assert values == {k: fm.DEFAULTS[k] for k in values}

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


@pytest.fixture
def no_message_boxes(monkeypatch):
    shown = []
    for name in ("information", "warning"):
        monkeypatch.setattr(
            gui.QMessageBox, name, lambda *a, n=name: shown.append((n, a[-1])), raising=True
        )
    return shown


def test_email_dialog_saves_account_and_policy(window, no_message_boxes):
    dialog = gui.EmailDialog(window)
    dialog._user_edit.setText("me@gmail.com")
    dialog._password_edit.setText("abcd efgh ijkl mnop")
    dialog._to_edit.setText("you@example.com")
    dialog._when_combo.setCurrentIndex(dialog._when_combo.findData("low"))
    dialog.accept()
    cfg = fm.email_config()
    assert (cfg.user, cfg.recipient, cfg.port) == ("me@gmail.com", "you@example.com", 465)
    assert cfg.password == "abcd efgh ijkl mnop"
    assert "abcd" not in fm.ENV_FILE.read_text(encoding="utf-8")
    assert fm.load_settings()["email_when"] == "low"


def test_email_dialog_reports_a_missing_credential_store(window, monkeypatch, no_message_boxes):
    class NoStore:
        def set_password(self, *a):
            raise RuntimeError("No recommended backend was available")

    monkeypatch.setattr(fm, "_keyring", lambda: NoStore())
    dialog = gui.EmailDialog(window)
    dialog._user_edit.setText("me@gmail.com")
    dialog._password_edit.setText("pw")
    dialog.accept()
    assert no_message_boxes[-1][0] == "warning" and dialog.result() == 0
    assert not fm.ENV_FILE.exists()


def test_email_dialog_test_button(window, monkeypatch, no_message_boxes):
    sent = []
    monkeypatch.setattr(fm, "send_email", lambda to, subject, body, cfg: sent.append((to, cfg)))
    dialog = gui.EmailDialog(window)
    dialog._send_test()
    assert sent == [] and no_message_boxes[-1][0] == "warning"
    dialog._user_edit.setText("me@gmail.com")
    dialog._password_edit.setText("pw")
    dialog._send_test()
    assert sent[0][0] == "me@gmail.com" and no_message_boxes[-1][0] == "information"
    assert fm.email_config().user == ""


def test_schedule_controls_save_immediately(window, no_message_boxes):
    window._times_edit.setText("21:30,7:05")
    window._auto_check.setChecked(True)
    s = fm.load_settings()
    assert s["auto_run"] is True and s["run_times"] == "07:05,21:30"
    assert window._next_label.text().startswith("next:")
    window._times_edit.setText("soon")
    window._save_schedule()
    assert no_message_boxes[-1][0] == "warning"
    assert fm.load_settings()["run_times"] == "07:05,21:30"


@pytest.fixture
def started(window, monkeypatch):
    calls = []
    monkeypatch.setattr(
        window._controller, "start", lambda search, engine, **kw: calls.append((search, kw))
    )
    return calls


def test_due_slot_starts_a_recorded_emailed_run(window, monkeypatch, started):
    fm.save_settings({"auto_run": True, "email_when": "low", "end": "2099-01-31"})
    monkeypatch.setattr(fm, "due_run", lambda now, times, last: now)
    window._tick_schedule()
    ((search, kw),) = started
    assert (kw["record"], kw["send"], kw["email_when"]) == (True, True, "low")
    assert search.origin == "AMS"
    assert "last_auto_run" in fm.load_state()


def test_no_run_when_off_or_window_over(window, monkeypatch, started):
    monkeypatch.setattr(fm, "due_run", lambda now, times, last: now)
    window._tick_schedule()
    fm.save_settings({"auto_run": True, "start": "2020-01-01", "end": "2020-01-31"})
    window._tick_schedule()
    assert started == []


class FakeTray:
    def __init__(self):
        self.notes = []

    def notify(self, title, message, warning=False):
        self.notes.append(message)

    def sync_menu(self):
        pass

    def setToolTip(self, text):
        self.tooltip = text

    def show_window(self):
        pass


def test_close_hides_to_tray(window):
    tray = FakeTray()
    window.set_tray(tray)
    window.show()
    window.close()
    assert not window.isVisible() and "tray" in tray.notes[0]


def test_icon_and_version_parsing():
    import desktop

    assert not desktop.render_icon(32).isNull()
    assert desktop.parse_version("v0.10.1") > desktop.parse_version("0.9.9")
    assert desktop.parse_version("garbage") == (0,)


def _send_from_thread(instance, message):
    """Send like a second process would: the client blocks until the server acknowledges,
    so the server's event loop (this thread) must keep running meanwhile."""
    import threading

    from PySide6.QtCore import QCoreApplication

    result = []
    worker = threading.Thread(target=lambda: result.append(instance.send(message)))
    worker.start()
    while worker.is_alive():
        QCoreApplication.processEvents()
    worker.join()
    return result[0]


def test_single_instance_hands_over_to_the_running_copy():
    import desktop

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    key = f"flight-monitor-test-{os.getpid()}"
    first = desktop.SingleInstance(key)
    shown, quits = [], []
    first.activated.connect(lambda: shown.append(True))
    first.quit_requested.connect(lambda: quits.append(True))
    assert first.already_running() is False

    assert _send_from_thread(desktop.SingleInstance(key), b"show") is True
    assert shown == [True] and quits == []
    assert _send_from_thread(desktop.SingleInstance(key), b"quit") is True
    assert quits == [True] and shown == [True]
    assert desktop.SingleInstance(f"{key}-nobody").send(b"quit") is False
    del app


def test_smoke_test_passes(data_dir):
    pytest.importorskip("fli")
    assert gui.smoke_test() == 0
