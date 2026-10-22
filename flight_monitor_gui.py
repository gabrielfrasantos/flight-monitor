#!/usr/bin/env python3
"""
Flight Monitor GUI: pick route, dates, airline, stopovers and price engine, then search.

Run:  python flight_monitor_gui.py      (pythonw on Windows: no console window)

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
├ Report ────────────────────────────────────────────────────────────────┤
│ [ ] Email report to [________]  [ ] Add to history  LOW ≤ [€1000] HIGH ≥ [€1250] │
└────────────────────────────────────────────────────────────────────────┘
 ● LOW · €995 out 2026-12-01 (KL, direct, 10:55)  [Save as default] [ Search ]
 ┌ Outbound │ Return │ Currency │ Price │ Airline │ Stops │ Departs ───────┐
 └────────────────────────────────────────────────────────────────────────┘
 ┌ log output ────────────────────────────────────────────────────────────┐
 └────────────────────────────────────────────────────────────────────────┘

Thread model (as in port-bridge): Qt owns the main thread; each search runs
flight_monitor.run_monitor() in a daemon thread, which posts log records,
offers and the result to a queue.SimpleQueue that a 100 ms QTimer drains.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import queue
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

LIGHT_COLORS = {"LOW": "#44dd44", "NORMAL": "#f0c060", "HIGH": "#ff4444"}
IDLE, RUNNING, FAILED = "#444444", "#5aa0ff", "#ff4444"


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
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Flight Monitor {fm.__version__}")
        self.setMinimumWidth(820)

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
        layout.addWidget(self._build_status_row())
        layout.addWidget(self._build_results_table(), stretch=3)
        layout.addWidget(self._build_log_panel(), stretch=1)
        self._setup_status_bar()

        try:
            self._load(fm.load_settings())
        except (ValueError, TypeError) as e:
            fm.log(f"Bad value in {fm.SETTINGS_JSON.name} ({e}); showing built-in defaults.")
            self._load(fm.DEFAULTS)
        self._on_engine_changed()

    def shutdown(self) -> None:
        self._controller.shutdown()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        event.accept()

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
        box = QGroupBox("Report")
        row = QHBoxLayout(box)

        self._email_check = QCheckBox("Email report to")
        self._email_edit = QLineEdit(
            os.environ.get("REPORT_EMAIL") or os.environ.get("GMAIL_USER", "")
        )
        self._history_check = QCheckBox("Add to history")
        self._history_check.setToolTip(
            "Append this run to data\\price_history.csv and run_summary.csv, which set the\n"
            "traffic-light baseline. The scheduled run always does; leave off for one-off\n"
            "searches on other routes."
        )
        self._low_spin = self._spin(0, 50000, "", prefix="€ ")
        self._high_spin = self._spin(0, 50000, "", prefix="€ ")

        row.addWidget(self._email_check)
        row.addWidget(self._email_edit, stretch=1)
        row.addSpacing(12)
        row.addWidget(self._history_check)
        row.addSpacing(12)
        row.addWidget(QLabel("LOW ≤"))
        row.addWidget(self._low_spin)
        row.addWidget(QLabel("HIGH ≥"))
        row.addWidget(self._high_spin)
        return box

    def _build_status_row(self) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(self._dot)
        row.addWidget(self._status, stretch=1)

        self._save_btn = QPushButton("Save as default")
        self._save_btn.setToolTip(
            "Write these settings to data\\settings.json: the scheduled\n"
            "run and the next GUI start use them."
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
        open_btn = QPushButton("Open data folder")
        open_btn.setFlat(True)
        open_btn.setStyleSheet("color: #aaaaff; font-size: 10px;")
        open_btn.clicked.connect(self._open_data_dir)
        bar.addPermanentWidget(open_btn)

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

    def _airlines(self) -> tuple[str, ...]:
        text = self._airline_combo.currentText().strip()
        if text in ("", "Any airline"):
            return ()
        return fm.parse_airlines(text.split(" - ")[0])

    def _values(self) -> dict:
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
        return fm.build_search(
            v["origin"],
            v["destination"],
            fm.parse_airlines(v["airline"]),
            fm.parse_stops(v["max_stops"]),
            dt.date.fromisoformat(v["start"]),
            dt.date.fromisoformat(v["end"]),
            v["stay_days"],
            v["step_days"],
            fm.parse_currencies(v["currencies"]),
        )

    def _engine_key(self) -> str:
        return self._engine_combo.currentData()

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
        recipient = self._email_edit.text().strip()
        if self._email_check.isChecked() and not recipient:
            QMessageBox.warning(self, "Invalid settings", "Enter the report email address.")
            return

        self._clear_results()
        self._set_editing_enabled(False)
        self._search_btn.setText("Stop")
        self._set_status(RUNNING, f"Searching {search.label}…")
        self._controller.start(
            search,
            engine,
            recipient=recipient,
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
        self.statusBar().showMessage("Saved: the scheduled run and the next start use these.", 5000)

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
        self._set_editing_enabled(True)
        self._search_btn.setText("Search")
        self._search_btn.setEnabled(True)
        if r.cancelled:
            self._set_status(IDLE, "Stopped.")
            return
        if not r.best:
            why = r.failure or f"No offers found ({r.errors} errors). See the log below."
            self._set_status(FAILED, why)
            return
        # Rows streamed in as they were found; show the run's final set, cheapest marked.
        self._clear_results()
        for offer in r.offers:
            self._add_offer(offer)
        self._mark_cheapest(r)
        head = r.best.get("EUR") or next(iter(r.best.values()))
        details = ", ".join(
            x
            for x in (
                head.airline,
                None
                if head.stops is None
                else ("direct" if head.stops == 0 else f"{head.stops} stop(s)"),
                None if head.out_dep_time == "--:--" else head.out_dep_time,
            )
            if x
        )
        others = "  ".join(fm.money(o.currency, o.price) for o in r.best.values() if o is not head)
        text = (
            f"{fm.money(head.currency, head.price)} out {head.depart}, back {head.ret}"
            + (f" ({details})" if details else "")
            + (f"   {others}" if others else "")
            + f"   via {fm.engine_label(r.engine)}"
        )
        if r.light:
            text = f"{r.light} · {text}"
        if r.new_low:
            text += "   NEW ALL-TIME LOW"
        if r.failure:
            text += f"   (incomplete: {r.failure})"
        self._set_status(LIGHT_COLORS.get(r.light, RUNNING), text)

    @Slot(object)
    def _on_log_record(self, record: logging.LogRecord) -> None:
        ts = dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        self._log_view.appendPlainText(f"[{ts}] {record.getMessage()}")

    @Slot()
    def _open_data_dir(self) -> None:
        fm.DATA_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(fm.DATA_DIR)))

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


def main() -> int:
    fm.load_dotenv()
    fm.setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("Flight Monitor")
    window = MainWindow()
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
