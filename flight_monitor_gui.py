#!/usr/bin/env python3
"""
Flight Monitor GUI: pick route, dates, airline, stopovers and price engine, then search.
Lives in the system tray and runs the monitor on its own schedule (the installed app
starts with Windows), with email settings for the reports.

Run:  python flight_monitor_gui.py      (pythonw on Windows: no console window)
      flight-monitor.exe [--minimized]  (installed app; --minimized starts in the tray)
      flight-monitor.exe --quit         (ask the running copy to exit; the installer uses it)

Layout
------
┌ Route ─────────────────────────────────────────────────────────────────┐
│ From [AMS]  To [GRU]   Airline [KL - KLM            ▼]  Stops [Direct ▼] │
├ Dates ─────────────────────────────────────────────────────────────────┤
│ Outbound from [01-12-2026▼] to [28-01-2027▼]  Stay [15 nights] Every [1 day] │
├ Price engine ──────────────────────────────────────────────────────────┤
│ Engine [Auto - free engines in order       ▼]   Currencies [x] EUR [x] BRL │
│ API key [••••••••••]  Get a key                                         │
│ what the engine reads, its catch, and the requests this search needs    │
├ This search ───────────────────────────────────────────────────────────┤
│ [ ] Email the report  [ ] Add to history     LOW ≤ [€1000]  HIGH ≥ [€1250] │
├ Automatic runs ────────────────────────────────────────────────────────┤
│ [x] Run at [08:00, 20:00]  next: Thu 20:00   [Email settings…] [Run now] │
└────────────────────────────────────────────────────────────────────────┘
 ● LOW · €995 out 2026-12-01 (KL, direct, 10:55)  [Save as default] [ Search ]
 ┌ Outbound │ Return │ Currency │ Price │ Airline │ Stops │ Departs ───────┐
 └────────────────────────────────────────────────────────────────────────┘
 ┌ log output ────────────────────────────────────────────────────────────┐
 └────────────────────────────────────────────────────────────────────────┘

Thread model (as in port-bridge): Qt owns the main thread; each run calls
flight_monitor.run_monitor() in a daemon thread, which posts log records,
offers and the result to a queue.SimpleQueue that a 100 ms QTimer drains.
Automatic runs use the saved defaults (data/settings.json), not unsaved edits.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import queue
import signal
import sys
import threading

try:
    from PySide6.QtCore import QDate, QObject, Qt, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QFont
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDateEdit,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QStatusBar,
        QSystemTrayIcon,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    print(
        "PySide6 is required for the GUI. Install it with:\n"
        "    python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)

import flight_monitor as fm
from desktop import (
    SingleInstance,
    SystemTrayIcon,
    UpdateChecker,
    UpdateDialog,
    app_icon,
    render_icon,
)
from engines import AUTO, AUTO_ORDER, ENGINES, Offer, Search

# (code, name); the combo also takes typed codes, e.g. "KL,AF".
AIRLINES = [
    ("", "Any airline"),
    ("KL", "KLM"),
    ("AF", "Air France"),
    ("LA", "LATAM"),
    ("TP", "TAP Air Portugal"),
    ("IB", "Iberia"),
    ("UX", "Air Europa"),
    ("LH", "Lufthansa"),
    ("LX", "SWISS"),
    ("BA", "British Airways"),
    ("AZ", "ITA Airways"),
    ("TK", "Turkish Airlines"),
    ("EK", "Emirates"),
    ("AD", "Azul"),
    ("G3", "GOL"),
]
STOPS = [("Direct only", "0"), ("Up to 1 stop", "1"), ("Up to 2 stops", "2"), ("Any", "any")]
CURRENCIES = ["EUR", "BRL"]
COLUMNS = ["Outbound", "Return", "Currency", "Price", "Airline", "Stops", "Departs"]
EMAIL_WHEN = [
    ("Every run", "always"),
    ("Only LOW prices, new lows and problems", "low"),
    ("Never", "never"),
]

LIGHT_COLORS = {"LOW": "#44dd44", "NORMAL": "#f0c060", "HIGH": "#ff4444"}
IDLE, RUNNING, FAILED = "#444444", "#5aa0ff", "#ff4444"
SCHEDULE_TICK_MS = 30_000


def _status_dot(color: str) -> QLabel:
    label = QLabel("●")
    label.setStyleSheet(f"color: {color}; font-size: 18px;")
    return label


def _airline_text(code: str) -> str:
    return f"{code} - {dict(AIRLINES)[code]}" if code else "Any airline"


def _qdate(d: dt.date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _pydate(q: QDate) -> dt.date:
    return dt.date(q.year(), q.month(), q.day())


def _summary(r: fm.RunResult) -> str:
    """One line for the status row and tray notifications."""
    if r.cancelled:
        return "Stopped."
    if not r.best:
        return r.failure or f"No offers found ({r.errors} errors). See the log."
    head = r.best.get("EUR") or next(iter(r.best.values()))
    stops = (
        None if head.stops is None else ("direct" if head.stops == 0 else f"{head.stops} stop(s)")
    )
    dep = None if head.out_dep_time == "--:--" else head.out_dep_time
    details = ", ".join(x for x in (head.airline, stops, dep) if x)
    others = "  ".join(fm.money(o.currency, o.price) for o in r.best.values() if o is not head)
    text = (
        f"{fm.money(head.currency, head.price)} out {head.depart}, back {head.ret}"
        + (f" ({details})" if details else "")
        + (f"   {others}" if others else "")
    )
    if r.light:
        text = f"{r.light} · {text}"
    if r.new_low:
        text += "   NEW ALL-TIME LOW"
    if r.failure:
        text += f"   (incomplete: {r.failure})"
    return text


# --------------------------------------------------------------------------- #
# Worker thread <-> Qt
# --------------------------------------------------------------------------- #
class _QueueHandler(logging.Handler):
    def __init__(self, events: queue.SimpleQueue) -> None:
        super().__init__()
        self._events = events

    def emit(self, record: logging.LogRecord) -> None:
        self._events.put_nowait(("log", record))


class SearchController(QObject):
    """Runs one monitor run at a time in a daemon thread; re-emits its events on Qt's thread."""

    log_record = Signal(object)  # logging.LogRecord
    engine_started = Signal(str)  # engine key ("auto" may try several)
    offer_found = Signal(object)  # engines.Offer
    finished = Signal(object)  # flight_monitor.RunResult

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._log_handler = _QueueHandler(self._events)
        fm.logger.addHandler(self._log_handler)

        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(100)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, search: Search, engine: str, **options) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(search, engine, options),
            daemon=True,
            name="flight-monitor-search",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self, timeout: float = 10.0) -> None:
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout)
        fm.logger.removeHandler(self._log_handler)

    def _run(self, search: Search, engine: str, options: dict) -> None:
        put = self._events.put_nowait
        try:
            result = fm.run_monitor(
                search,
                engine,
                stop=self._stop,
                on_engine=lambda key: put(("engine", key)),
                on_offer=lambda offer: put(("offer", offer)),
                **options,
            )
        except Exception as e:  # a bug, not a price problem: report it, don't die
            fm.log(f"Run crashed: {e!r}")
            result = fm.RunResult(search, engine=engine, failure=f"Run crashed: {e}")
        put(("done", result))

    def _drain(self) -> None:
        signals = {
            "log": self.log_record,
            "engine": self.engine_started,
            "offer": self.offer_found,
            "done": self.finished,
        }
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                return
            signals[kind].emit(payload)


# --------------------------------------------------------------------------- #
# Email settings
# --------------------------------------------------------------------------- #
class EmailDialog(QDialog):
    """Report recipient, sending account and when to email.

    The account goes to .env (SMTP_*), the policy to data/settings.json.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Email settings")
        self.setMinimumWidth(460)
        cfg = fm.email_config()

        self._to_edit = QLineEdit(cfg.recipient)
        self._to_edit.setPlaceholderText("where reports go (default: the sending account)")
        self._user_edit = QLineEdit(cfg.user)
        self._user_edit.setPlaceholderText("you@gmail.com")
        self._password_edit = QLineEdit(cfg.password)
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText("Gmail: a 16-letter App Password")
        help_link = QLabel(
            '<a href="https://myaccount.google.com/apppasswords">Create a Gmail App Password</a>'
            " (needs 2-Step Verification)"
        )
        help_link.setOpenExternalLinks(True)
        self._host_edit = QLineEdit(cfg.host)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(cfg.port)
        self._port_spin.setToolTip("465 = SSL; 587 (or any other) = STARTTLS")
        self._when_combo = QComboBox()
        for label, value in EMAIL_WHEN:
            self._when_combo.addItem(label, value)
        when = fm.load_settings()["email_when"]
        self._when_combo.setCurrentIndex(max(0, self._when_combo.findData(when)))

        form = QFormLayout()
        form.addRow("Send reports to:", self._to_edit)
        form.addRow("From account:", self._user_edit)
        form.addRow("Password:", self._password_edit)
        form.addRow("", help_link)
        server = QHBoxLayout()
        server.addWidget(self._host_edit, stretch=1)
        server.addWidget(QLabel("Port:"))
        server.addWidget(self._port_spin)
        form.addRow("SMTP server:", server)
        form.addRow("When to email:", self._when_combo)
        stored = QLabel(f"Stored on this PC in {fm.ENV_FILE}")
        stored.setStyleSheet("color: #888888; font-size: 10px;")
        stored.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._test_btn = buttons.addButton(
            "Send test email", QDialogButtonBox.ButtonRole.ActionRole
        )
        self._test_btn.clicked.connect(self._send_test)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(stored)
        layout.addWidget(buttons)

    def config(self) -> fm.EmailConfig:
        user = self._user_edit.text().strip()
        return fm.EmailConfig(
            user=user,
            password=self._password_edit.text().strip(),
            recipient=self._to_edit.text().strip() or user,
            host=self._host_edit.text().strip() or fm.SMTP_HOST,
            port=self._port_spin.value(),
        )

    def email_when(self) -> str:
        return self._when_combo.currentData()

    @Slot()
    def _send_test(self) -> None:
        cfg = self.config()
        if not cfg.complete:
            QMessageBox.warning(self, "Email settings", "Enter the sending account and password.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            fm.send_email(
                cfg.recipient,
                "Flight Monitor test email",
                f"Email settings work. Reports will come from {cfg.user} via {cfg.host}.",
                cfg,
            )
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Test email failed", f"Could not send:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Test email", f"Sent to {cfg.recipient}.")

    def accept(self) -> None:
        cfg = self.config()
        if cfg.recipient and "@" not in cfg.recipient:
            QMessageBox.warning(self, "Email settings", f"{cfg.recipient!r} is not an address.")
            return
        fm.save_email_config(cfg)
        fm.save_settings({"email_when": self.email_when()})
        fm.log(f"Email settings saved (to {cfg.recipient or '-'}, email {self.email_when()}).")
        super().accept()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Flight Monitor {fm.__version__}")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(820)

        self._tray: SystemTrayIcon | None = None
        self._checker: UpdateChecker | None = None
        self._told_about_tray = False
        self._saved_run = False  # the running search is an automatic / "Run now" one
        self._manual_update_check = False

        self._controller = SearchController(self)
        self._controller.log_record.connect(self._on_log_record)
        self._controller.engine_started.connect(self._on_engine_started)
        self._controller.offer_found.connect(self._add_offer)
        self._controller.finished.connect(self._on_finished)

        self._dot = _status_dot(IDLE)
        self._status = QLabel("Ready.")

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)
        layout.addWidget(self._build_route_group())
        layout.addWidget(self._build_dates_group())
        layout.addWidget(self._build_engine_group())
        layout.addWidget(self._build_report_group())
        layout.addWidget(self._build_schedule_group())
        layout.addWidget(self._build_status_row())
        layout.addWidget(self._build_results_table(), stretch=3)
        layout.addWidget(self._build_log_panel(), stretch=1)
        self._setup_status_bar()

        settings = fm.load_settings()
        try:
            self._load(settings)
        except (ValueError, TypeError) as e:
            fm.log(f"Bad value in {fm.SETTINGS_JSON.name} ({e}); showing built-in defaults.")
            self._load(fm.DEFAULTS)
        self._load_schedule(settings)
        self._on_engine_changed()

        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(SCHEDULE_TICK_MS)
        self._schedule_timer.timeout.connect(self._tick_schedule)
        self._schedule_timer.start()
        QTimer.singleShot(5000, self._tick_schedule)  # catch up soon after login / wake

    def set_tray(self, tray: SystemTrayIcon) -> None:
        self._tray = tray
        self._refresh_schedule_info()

    def set_checker(self, checker: UpdateChecker) -> None:
        self._checker = checker
        checker.update_available.connect(self._on_update_available)
        checker.check_done.connect(self._on_update_check_done)

    def shutdown(self) -> None:
        self._schedule_timer.stop()
        self._controller.shutdown()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._tray is not None:
            # Closing hides to the tray: the schedule keeps running. Quit is in its menu.
            event.ignore()
            self.hide()
            self._tray.sync_menu()
            if not self._told_about_tray:
                self._told_about_tray = True
                self._tray.notify(
                    "Flight Monitor",
                    "Still running in the tray, automatic runs continue. "
                    "Right-click the icon to quit.",
                )
            return
        self.shutdown()
        event.accept()

    def bring_to_front(self) -> None:
        if self._tray is not None:
            self._tray.show_window()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    # ------------------------------------------------------------------
    # Builder helpers
    # ------------------------------------------------------------------

    def _build_route_group(self) -> QGroupBox:
        box = QGroupBox("Route")
        row = QHBoxLayout(box)

        self._origin_edit = self._airport_edit()
        self._dest_edit = self._airport_edit()
        self._airline_combo = QComboBox()
        self._airline_combo.setEditable(True)
        self._airline_combo.setMinimumWidth(190)
        self._airline_combo.addItems([_airline_text(code) for code, _ in AIRLINES])
        self._airline_combo.setToolTip("Pick one, or type carrier codes: KL or KL,AF")
        self._stops_combo = QComboBox()
        for label, value in STOPS:
            self._stops_combo.addItem(label, value)

        row.addWidget(QLabel("From:"))
        row.addWidget(self._origin_edit)
        row.addWidget(QLabel("To:"))
        row.addWidget(self._dest_edit)
        row.addSpacing(12)
        row.addWidget(QLabel("Airline:"))
        row.addWidget(self._airline_combo, stretch=1)
        row.addSpacing(12)
        row.addWidget(QLabel("Stopovers:"))
        row.addWidget(self._stops_combo)
        return box

    def _build_dates_group(self) -> QGroupBox:
        box = QGroupBox("Dates")
        row = QHBoxLayout(box)

        self._start_edit = self._date_edit()
        self._end_edit = self._date_edit()
        self._stay_spin = self._spin(1, 90, " nights")
        self._step_spin = self._spin(1, 30, " day(s)")
        self._step_spin.setToolTip("Probe every Nth outbound date (fewer requests).")
        for w in (self._start_edit, self._end_edit, self._stay_spin, self._step_spin):
            signal = w.dateChanged if isinstance(w, QDateEdit) else w.valueChanged
            signal.connect(self._update_engine_info)

        row.addWidget(QLabel("Outbound from:"))
        row.addWidget(self._start_edit)
        row.addWidget(QLabel("to:"))
        row.addWidget(self._end_edit)
        row.addSpacing(12)
        row.addWidget(QLabel("Stay:"))
        row.addWidget(self._stay_spin)
        row.addWidget(QLabel("Every:"))
        row.addWidget(self._step_spin)
        row.addStretch()
        return box

    def _build_engine_group(self) -> QGroupBox:
        box = QGroupBox("Price engine")
        grid = QGridLayout(box)

        self._engine_combo = QComboBox()
        self._engine_combo.addItem("Auto - free engines in order, with fallback", AUTO)
        for key, engine in ENGINES.items():
            self._engine_combo.addItem(f"{engine.label}   ({engine.cost})", key)
        self._engine_combo.currentIndexChanged.connect(self._on_engine_changed)

        self._currency_checks = {c: QCheckBox(c) for c in CURRENCIES}
        currencies = QHBoxLayout()
        currencies.addWidget(QLabel("Currencies:"))
        for check in self._currency_checks.values():
            check.toggled.connect(self._update_engine_info)
            currencies.addWidget(check)

        self._key_label = QLabel("API key:")
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._signup_link = QLabel()
        self._signup_link.setOpenExternalLinks(True)
        self._engine_about = QLabel()
        self._engine_about.setWordWrap(True)
        self._engine_about.setStyleSheet("color: #888888;")

        grid.addWidget(QLabel("Engine:"), 0, 0)
        grid.addWidget(self._engine_combo, 0, 1)
        grid.addLayout(currencies, 0, 2)
        grid.addWidget(self._key_label, 1, 0)
        grid.addWidget(self._key_edit, 1, 1)
        grid.addWidget(self._signup_link, 1, 2)
        grid.addWidget(self._engine_about, 2, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        return box

    def _build_report_group(self) -> QGroupBox:
        box = QGroupBox("This search")
        row = QHBoxLayout(box)

        self._email_check = QCheckBox("Email the report")
        self._email_check.setToolTip("Send this search's report to the address in Email settings.")
        self._history_check = QCheckBox("Add to history")
        self._history_check.setToolTip(
            "Append this run to price_history.csv and run_summary.csv, which set the\n"
            "traffic-light baseline. Automatic runs always do; leave off for one-off\n"
            "searches on other routes."
        )
        self._low_spin = self._spin(0, 50000, "", prefix="€ ")
        self._high_spin = self._spin(0, 50000, "", prefix="€ ")

        row.addWidget(self._email_check)
        row.addSpacing(12)
        row.addWidget(self._history_check)
        row.addStretch()
        row.addWidget(QLabel("LOW ≤"))
        row.addWidget(self._low_spin)
        row.addWidget(QLabel("HIGH ≥"))
        row.addWidget(self._high_spin)
        return box

    def _build_schedule_group(self) -> QGroupBox:
        box = QGroupBox("Automatic runs")
        row = QHBoxLayout(box)

        self._auto_check = QCheckBox("Run the monitor at")
        self._auto_check.setToolTip(
            "Runs the saved defaults at these times while the app is open (it keeps\n"
            "running in the tray), records history and emails per Email settings.\n"
            "A time missed while the PC slept runs at wake-up, up to 6 hours late."
        )
        self._auto_check.toggled.connect(self._save_schedule)
        self._times_edit = QLineEdit()
        self._times_edit.setPlaceholderText("08:00, 20:00")
        self._times_edit.setMaximumWidth(160)
        self._times_edit.editingFinished.connect(self._save_schedule)
        self._next_label = QLabel()
        self._next_label.setStyleSheet("color: #888888;")
        email_btn = QPushButton("Email settings…")
        email_btn.clicked.connect(self._edit_email)
        self._run_now_btn = QPushButton("Run now")
        self._run_now_btn.setToolTip("Run the saved defaults now, like an automatic run.")
        self._run_now_btn.clicked.connect(self.run_saved_now)

        row.addWidget(self._auto_check)
        row.addWidget(self._times_edit)
        row.addWidget(self._next_label, stretch=1)
        row.addWidget(email_btn)
        row.addWidget(self._run_now_btn)
        return box

    def _build_status_row(self) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(self._dot)
        row.addWidget(self._status, stretch=1)

        self._save_btn = QPushButton("Save as default")
        self._save_btn.setToolTip(
            "Write these settings to settings.json: automatic runs, the\n"
            "command line and the next start use them."
        )
        self._save_btn.clicked.connect(self._save_defaults)
        row.addWidget(self._save_btn)

        self._search_btn = QPushButton("Search")
        self._search_btn.setMinimumWidth(100)
        self._search_btn.setDefault(True)
        self._search_btn.clicked.connect(self._toggle_search)
        row.addWidget(self._search_btn)
        return widget

    def _build_results_table(self) -> QTableWidget:
        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSortIndicator(COLUMNS.index("Price"), Qt.SortOrder.AscendingOrder)
        self._table.setSortingEnabled(True)
        self._table.setMinimumHeight(180)
        return self._table

    def _build_log_panel(self) -> QPlainTextEdit:
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setStyleSheet(
            "QPlainTextEdit {"
            " background: #1e1e1e; color: #dddddd;"
            " font-family: monospace; font-size: 11px;"
            " }"
        )
        return self._log_view

    def _setup_status_bar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        path = QLabel(f"Data: {fm.DATA_DIR}")
        path.setStyleSheet("color: #888888; font-size: 10px;")
        bar.addWidget(path, 1)
        open_btn = self._link_button("Open data folder", self.open_data_dir)
        self._check_updates_btn = self._link_button("Check for updates", self.check_for_updates)
        bar.addPermanentWidget(open_btn)
        bar.addPermanentWidget(self._check_updates_btn)

    @staticmethod
    def _link_button(text: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setFlat(True)
        btn.setStyleSheet("color: #aaaaff; font-size: 10px;")
        btn.clicked.connect(slot)
        return btn

    @staticmethod
    def _airport_edit() -> QLineEdit:
        edit = QLineEdit()
        edit.setMaxLength(3)
        edit.setFixedWidth(56)
        edit.setPlaceholderText("IATA")
        edit.textEdited.connect(lambda text, e=edit: e.setText(text.upper()))
        return edit

    @staticmethod
    def _date_edit() -> QDateEdit:
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("dd-MM-yyyy")
        return edit

    @staticmethod
    def _spin(low: int, high: int, suffix: str, prefix: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSuffix(suffix)
        spin.setPrefix(prefix)
        return spin

    # ------------------------------------------------------------------
    # Settings <-> widgets
    # ------------------------------------------------------------------

    def _load(self, s: dict) -> None:
        self._origin_edit.setText(str(s["origin"]).upper())
        self._dest_edit.setText(str(s["destination"]).upper())
        codes = fm.parse_airlines(s["airline"])
        names = dict(AIRLINES)
        if not codes:
            self._airline_combo.setCurrentIndex(0)
        elif len(codes) == 1 and codes[0] in names:
            self._airline_combo.setCurrentText(_airline_text(codes[0]))
        else:
            self._airline_combo.setCurrentText(",".join(codes))
        stops = fm.parse_stops(s["max_stops"])
        value = "any" if stops is None else str(min(stops, 2))
        self._stops_combo.setCurrentIndex(max(0, self._stops_combo.findData(value)))
        self._start_edit.setDate(_qdate(dt.date.fromisoformat(s["start"])))
        self._end_edit.setDate(_qdate(dt.date.fromisoformat(s["end"])))
        self._stay_spin.setValue(int(s["stay_days"]))
        self._step_spin.setValue(int(s["step_days"]))
        self._engine_combo.setCurrentIndex(max(0, self._engine_combo.findData(s["engine"])))
        chosen = fm.parse_currencies(s["currencies"])
        for cur, check in self._currency_checks.items():
            check.setChecked(cur in chosen)
        self._low_spin.setValue(int(s["low_eur"]))
        self._high_spin.setValue(int(s["high_eur"]))

    def _load_schedule(self, s: dict) -> None:
        self._auto_check.blockSignals(True)
        self._auto_check.setChecked(bool(s["auto_run"]))
        self._auto_check.blockSignals(False)
        self._times_edit.setText(", ".join(t.strftime("%H:%M") for t in self._times(s)))
        self._refresh_schedule_info()

    @staticmethod
    def _times(s: dict) -> list[dt.time]:
        return fm.parse_run_times(s["run_times"])

    def _airlines(self) -> tuple[str, ...]:
        text = self._airline_combo.currentText().strip()
        if text in ("", "Any airline"):
            return ()
        return fm.parse_airlines(text.split(" - ")[0])

    def _values(self) -> dict:
        """The route fields (what "Save as default" stores)."""
        airlines = self._airlines()
        return {
            "origin": self._origin_edit.text().strip().upper(),
            "destination": self._dest_edit.text().strip().upper(),
            "airline": ",".join(airlines) or "ANY",
            "max_stops": self._stops_combo.currentData(),
            "start": _pydate(self._start_edit.date()).isoformat(),
            "end": _pydate(self._end_edit.date()).isoformat(),
            "stay_days": self._stay_spin.value(),
            "step_days": self._step_spin.value(),
            "currencies": ",".join(
                c for c, chk in self._currency_checks.items() if chk.isChecked()
            ),
            "engine": self._engine_combo.currentData(),
            "low_eur": float(self._low_spin.value()),
            "high_eur": float(self._high_spin.value()),
        }

    def _build_search(self) -> Search:
        """Search from the widgets; raises ValueError naming the bad field."""
        v = self._values()
        if not v["currencies"]:
            raise ValueError("pick at least one currency")
        return fm.search_from_settings(v)

    def _engine_key(self) -> str:
        return self._engine_combo.currentData()

    # ------------------------------------------------------------------
    # Automatic runs
    # ------------------------------------------------------------------

    @Slot()
    def _save_schedule(self) -> None:
        text = self._times_edit.text()
        try:
            times = fm.parse_run_times(text)
        except ValueError:
            QMessageBox.warning(
                self, "Automatic runs", f"{text!r}: use times like 08:00, 20:00 (24-hour)."
            )
            self._load_schedule(fm.load_settings())
            return
        run_times = ",".join(t.strftime("%H:%M") for t in times)
        fm.save_settings({"auto_run": self._auto_check.isChecked(), "run_times": run_times})
        self._times_edit.setText(run_times.replace(",", ", "))
        self._refresh_schedule_info()

    def _refresh_schedule_info(self) -> None:
        s = fm.load_settings()
        if not s["auto_run"]:
            text = "off"
        else:
            nxt = fm.next_run(dt.datetime.now(), self._times(s))
            text = f"next: {nxt:%a %d-%m %H:%M}"
        self._next_label.setText(text)
        if self._tray is not None:
            self._tray.setToolTip(f"Flight Monitor {fm.__version__}\nAutomatic runs: {text}")

    @Slot()
    def _tick_schedule(self) -> None:
        self._refresh_schedule_info()
        s = fm.load_settings()
        if not s["auto_run"] or self._controller.is_running:
            return  # a search in progress: the slot stays due until it finishes
        last = fm.load_state().get("last_auto_run")
        try:
            last_run = dt.datetime.fromisoformat(last) if last else None
        except ValueError:
            last_run = None
        slot = fm.due_run(dt.datetime.now(), self._times(s), last_run)
        if slot is None:
            return
        fm.save_state(last_auto_run=dt.datetime.now().isoformat(timespec="seconds"))
        fm.log(f"Automatic run ({slot:%H:%M}).")
        self._start_saved_run(s)

    @Slot()
    def run_saved_now(self) -> None:
        if self._controller.is_running:
            QMessageBox.information(self, "Run now", "A search is already running.")
            return
        self._start_saved_run(fm.load_settings())

    def _start_saved_run(self, s: dict) -> None:
        """Run the saved defaults the way the scheduled monitor does: record + email."""
        try:
            search = fm.search_from_settings(s)
        except (ValueError, TypeError, KeyError) as e:
            self._notify(f"Saved settings are invalid ({e}). Fix them and Save as default.", True)
            return
        if not any(d > dt.date.today() for d in search.departures()):
            self._notify(
                f"The saved outbound window ({search.start} .. {search.end}) is over. "
                "Pick new dates and Save as default.",
                True,
            )
            return
        self._saved_run = True
        self._clear_results()
        self._set_editing_enabled(False)
        self._search_btn.setText("Stop")
        self._set_status(RUNNING, f"Monitor run: {search.label}…")
        self._controller.start(
            search,
            s["engine"],
            send=True,
            email_when=s["email_when"],
            record=True,
            low_eur=float(s["low_eur"]),
            high_eur=float(s["high_eur"]),
        )

    def _notify(self, message: str, warning: bool = False) -> None:
        fm.log(message)
        if self._tray is not None:
            self._tray.notify("Flight Monitor", message, warning)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot()
    def _on_engine_changed(self) -> None:
        engine = ENGINES.get(self._engine_key())
        needs_key = bool(engine and engine.key_env)
        self._key_label.setEnabled(needs_key)
        self._key_edit.setEnabled(needs_key)
        self._key_edit.setText(os.environ.get(engine.key_env, "") if needs_key else "")
        self._key_edit.setPlaceholderText(
            f"{engine.key_env}: saved to .env by Save as default"
            if needs_key
            else "not needed for this engine"
        )
        self._signup_link.setText(
            f'<a href="{engine.signup_url}">Get a key</a>' if needs_key else ""
        )
        self._update_engine_info()

    @Slot()
    def _update_engine_info(self) -> None:
        engine = ENGINES.get(self._engine_key())
        dates = currencies = 0
        try:
            search = self._build_search()
            dates, currencies = len(search.departures()), len(search.currencies)
        except ValueError:
            search = None
        if engine is None:
            about = (
                "Runs "
                + ", then ".join(ENGINES[k].label for k in AUTO_ORDER)
                + " (only with a token set), each one only for the currencies the "
                "previous found nothing for. Never touches a paid quota."
            )
            cost = ENGINES[AUTO_ORDER[0]].requests_for(search) if search else 0
        else:
            about = engine.about
            cost = engine.requests_for(search) if search else 0
        if search:
            about += (
                f"\nThis search: {dates} outbound date(s) × {currencies} currency(ies)"
                f" ≈ {cost} request(s)."
            )
        self._engine_about.setText(about)

    @Slot()
    def _toggle_search(self) -> None:
        if self._controller.is_running:
            self._controller.stop()
            self._search_btn.setEnabled(False)
            self._set_status(RUNNING, "Stopping…")
            return
        try:
            search = self._build_search()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid settings", str(e))
            return
        engine = self._engine_key()
        api_keys = {}
        if engine in ENGINES and ENGINES[engine].key_env:
            if not self._key_edit.text().strip():
                QMessageBox.warning(
                    self,
                    "API key needed",
                    f"{ENGINES[engine].label} needs an API key ({ENGINES[engine].key_env}).",
                )
                return
            api_keys[engine] = self._key_edit.text().strip()
        if self._email_check.isChecked() and not fm.email_config().complete:
            QMessageBox.warning(self, "Email", "Set up the email account first (Email settings).")
            return

        self._saved_run = False
        self._clear_results()
        self._set_editing_enabled(False)
        self._search_btn.setText("Stop")
        self._set_status(RUNNING, f"Searching {search.label}…")
        self._controller.start(
            search,
            engine,
            send=self._email_check.isChecked(),
            record=self._history_check.isChecked(),
            low_eur=float(self._low_spin.value()),
            high_eur=float(self._high_spin.value()),
            api_keys=api_keys,
        )

    @Slot()
    def _save_defaults(self) -> None:
        try:
            self._build_search()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid settings", str(e))
            return
        fm.save_settings(self._values())
        engine = ENGINES.get(self._engine_key())
        key = self._key_edit.text().strip()
        if engine and engine.key_env and key and key != os.environ.get(engine.key_env, ""):
            fm.set_env_value(engine.key_env, key)
        fm.log(f"Saved defaults to {fm.SETTINGS_JSON}.")
        self.statusBar().showMessage("Saved: automatic runs and the next start use these.", 5000)

    @Slot()
    def _edit_email(self) -> None:
        EmailDialog(self).exec()

    @Slot(str)
    def _on_engine_started(self, key: str) -> None:
        self._set_status(RUNNING, f"Searching with {fm.engine_label(key)}…")

    @Slot(object)
    def _add_offer(self, offer: Offer) -> None:
        stops = "" if offer.stops is None else ("direct" if offer.stops == 0 else str(offer.stops))
        cells = [
            offer.depart.isoformat(),
            offer.ret.isoformat(),
            offer.currency,
            round(offer.price),
            offer.airline,
            stops,
            offer.out_dep_time,
        ]
        self._table.setSortingEnabled(False)
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, value in enumerate(cells):
            item = QTableWidgetItem()
            item.setData(Qt.ItemDataRole.DisplayRole, value)  # ints sort numerically
            item.setData(Qt.ItemDataRole.UserRole, offer)
            self._table.setItem(row, col, item)
        self._table.setSortingEnabled(True)

    @Slot(object)
    def _on_finished(self, r: fm.RunResult) -> None:
        saved_run, self._saved_run = self._saved_run, False
        self._set_editing_enabled(True)
        self._search_btn.setText("Search")
        self._search_btn.setEnabled(True)
        text = _summary(r)
        if r.cancelled:
            self._set_status(IDLE, text)
            return
        if r.best:
            # Rows streamed in as they were found; show the run's final set, cheapest marked.
            self._clear_results()
            for offer in r.offers:
                self._add_offer(offer)
            self._mark_cheapest(r)
            color = LIGHT_COLORS.get(r.light, RUNNING)
            self._set_status(color, f"{text}   via {fm.engine_label(r.engine)}")
        else:
            self._set_status(FAILED, text)
        if saved_run and self._tray is not None:
            self._tray.notify("Flight Monitor", text, warning=not r.best or bool(r.failure))
        self._refresh_schedule_info()

    @Slot(object)
    def _on_log_record(self, record: logging.LogRecord) -> None:
        ts = dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        self._log_view.appendPlainText(f"[{ts}] {record.getMessage()}")

    @Slot()
    def open_data_dir(self) -> None:
        fm.DATA_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(fm.DATA_DIR)))

    @Slot()
    def check_for_updates(self) -> None:
        if self._checker is None:
            return
        self._manual_update_check = True
        self._check_updates_btn.setEnabled(False)
        self._check_updates_btn.setText("Checking…")
        self._checker.check_in_background()

    @Slot(str, str)
    def _on_update_available(self, version: str, url: str) -> None:
        UpdateDialog(version, url, self).exec()

    @Slot(bool)
    def _on_update_check_done(self, found: bool) -> None:
        self._check_updates_btn.setEnabled(True)
        self._check_updates_btn.setText("Check for updates")
        if not found and self._manual_update_check:
            self.statusBar().showMessage(f"Flight Monitor {fm.__version__} is up to date.", 5000)
        self._manual_update_check = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, color: str, text: str) -> None:
        self._dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self._status.setText(text)

    def _set_editing_enabled(self, enabled: bool) -> None:
        for box in self.centralWidget().findChildren(QGroupBox):
            box.setEnabled(enabled)
        self._save_btn.setEnabled(enabled)

    def _clear_results(self) -> None:
        self._table.setRowCount(0)

    def _mark_cheapest(self, r: fm.RunResult) -> None:
        best = {id(o) for o in r.best.values()}
        bold = QFont()
        bold.setBold(True)
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is None or id(item.data(Qt.ItemDataRole.UserRole)) not in best:
                continue
            for col in range(self._table.columnCount()):
                cell = self._table.item(row, col)
                cell.setFont(bold)
                cell.setBackground(QColor(68, 221, 68, 60))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _log_uncaught_exceptions() -> None:
    """The installed app has no console: send crashes to monitor.log."""
    original = sys.excepthook

    def hook(exc_type, exc, tb) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            fm.logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        original(exc_type, exc, tb)

    sys.excepthook = hook


def _quit_on_signals(app: QApplication) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    # Python signal handlers only run between bytecodes; a no-op timer gives them a turn.
    wake = QTimer(app)
    wake.timeout.connect(lambda: None)
    wake.start(250)


def smoke_test() -> int:
    """Build check for the packaged app: engine libraries load, the window builds."""
    try:
        import fast_flights.parser  # noqa: F401
        import primp  # noqa: F401
        from babel.numbers import format_currency
        from fli.search import SearchDates  # noqa: F401
        from fli.search.client import get_client

        get_client()
        format_currency(1, "EUR", locale="en_US")
        if render_icon(64).isNull():
            raise RuntimeError("icon did not render")
        window = MainWindow()
        window.shutdown()
    except Exception:
        fm.logger.exception("Smoke test failed")
        return 1
    fm.log(f"Smoke test passed (Flight Monitor {fm.__version__}, data in {fm.HOME}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flight Monitor GUI.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {fm.__version__}")
    parser.add_argument("--minimized", action="store_true", help="Start in the system tray.")
    parser.add_argument(
        "--quit", action="store_true", help="Ask the running copy to quit (used by the installer)."
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    fm.load_dotenv()
    fm.setup_logging()
    _log_uncaught_exceptions()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Flight Monitor")
    app.setWindowIcon(app_icon())
    if args.smoke_test:
        return smoke_test()

    instance = SingleInstance(parent=app)
    if args.quit:
        instance.send(b"quit")
        return 0
    if instance.already_running():
        return 0  # the running copy shows its window

    window = MainWindow()
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = SystemTrayIcon(
            window,
            run_now=window.run_saved_now,
            check_updates=window.check_for_updates,
            open_data=window.open_data_dir,
            parent=app,
        )
        tray.show()
        window.set_tray(tray)
        app.setQuitOnLastWindowClosed(False)
    instance.activated.connect(window.bring_to_front)
    instance.quit_requested.connect(app.quit)

    checker = UpdateChecker(app)
    window.set_checker(checker)
    if fm.FROZEN:  # a checkout updates with git pull
        QTimer.singleShot(3000, checker.check_in_background)

    app.aboutToQuit.connect(window.shutdown)
    _quit_on_signals(app)
    if not (args.minimized and QSystemTrayIcon.isSystemTrayAvailable()):
        window.show()
    if window._tray is not None:
        window._tray.sync_menu()
    fm.log(f"Flight Monitor {fm.__version__} started (data in {fm.HOME}).")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
