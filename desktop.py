"""
Desktop integration for the GUI, as in port-bridge: app icon, system-tray icon,
single-instance guard and the GitHub release update check.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap, QPolygonF
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from flight_monitor import __version__

logger = logging.getLogger("flight_monitor")

RELEASES_API = "https://api.github.com/repos/gabrielfrasantos/flight-monitor/releases/latest"
INSTANCE_KEY = "flight-monitor-gui"


# --------------------------------------------------------------------------- #
# Icon
# --------------------------------------------------------------------------- #
def render_icon(size: int) -> QPixmap:
    """Blue rounded square with a white airplane (top view, flying up-right)."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = max(1, size // 16)
    radius = max(3, size // 5)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#0277BD"))
    p.drawRoundedRect(margin, margin, size - 2 * margin, size - 2 * margin, radius, radius)

    # Plane drawn on a 100x100 grid, nose up, then turned 45 degrees.
    p.translate(size / 2, size / 2)
    p.rotate(45)
    p.scale(size / 125, size / 125)
    p.translate(-50, -50)
    plane = QPainterPath()
    plane.addRoundedRect(44, 8, 12, 84, 6, 6)
    for points in (
        [(50, 36), (92, 58), (92, 66), (50, 54), (8, 66), (8, 58)],  # wings
        [(50, 76), (68, 86), (68, 92), (50, 88), (32, 92), (32, 86)],  # tail
    ):
        plane.addPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
    p.setBrush(QColor("white"))
    p.drawPath(plane.simplified())
    p.end()
    return px


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(render_icon(size))
    return icon


# --------------------------------------------------------------------------- #
# Single instance
# --------------------------------------------------------------------------- #
class SingleInstance(QObject):
    """One app per user: a second launch asks the first to show its window and exits.

    Two copies would both run the schedule and send every email twice. The same
    channel lets the installer ask the running copy to quit before replacing files.
    """

    activated = Signal()  # another launch asked us to show the window
    quit_requested = Signal()  # `--quit` (the installer) asked us to exit

    def __init__(self, key: str = INSTANCE_KEY, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._server: QLocalServer | None = None

    def send(self, message: bytes) -> bool:
        """Deliver message to the running instance; False if there is none."""
        socket = QLocalSocket()
        socket.connectToServer(self._key)
        if not socket.waitForConnected(500):
            return False
        socket.write(message)
        socket.flush()
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return True

    def already_running(self) -> bool:
        """True (after asking it to show itself) if another instance is up; else become it."""
        if self.send(b"show"):
            return True
        # A crashed instance can leave a stale socket file behind (Unix).
        QLocalServer.removeServer(self._key)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        if not self._server.listen(self._key):
            logger.info(f"Single-instance guard unavailable: {self._server.errorString()}")
        return False

    def _on_connection(self) -> None:
        assert self._server is not None
        while (conn := self._server.nextPendingConnection()) is not None:
            conn.readyRead.connect(lambda c=conn: self._on_message(c))
            conn.disconnected.connect(conn.deleteLater)
            if conn.bytesAvailable():
                self._on_message(conn)

    def _on_message(self, conn: QLocalSocket) -> None:
        message = bytes(conn.readAll().data()).strip()
        if message == b"quit":
            self.quit_requested.emit()
        elif message:
            self.activated.emit()


# --------------------------------------------------------------------------- #
# Tray
# --------------------------------------------------------------------------- #
class SystemTrayIcon(QSystemTrayIcon):
    """Keeps the app reachable (and its schedule running) from the notification area.

    Click toggles the window; the menu adds Run now, update check, data folder and Quit.
    """

    def __init__(
        self,
        window: QWidget,
        *,
        run_now: Callable[[], None],
        check_updates: Callable[[], None],
        open_data: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(app_icon(), parent)
        self._window = window
        menu = QMenu()
        self._toggle_action = menu.addAction("Hide window")
        self._toggle_action.triggered.connect(self.toggle_window)
        menu.addSeparator()
        self.run_now_action = menu.addAction("Run monitor now")
        self.run_now_action.triggered.connect(run_now)
        self.check_updates_action = menu.addAction("Check for updates")
        self.check_updates_action.triggered.connect(check_updates)
        menu.addAction("Open data folder").triggered.connect(open_data)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(QApplication.quit)
        self._menu = menu
        self.setContextMenu(menu)
        self.setToolTip("Flight Monitor")
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_window()

    def toggle_window(self) -> None:
        if self._window.isVisible():
            self._window.hide()
        else:
            self.show_window()

    def show_window(self) -> None:
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()
        self.sync_menu()

    def sync_menu(self) -> None:
        self._toggle_action.setText("Hide window" if self._window.isVisible() else "Show window")

    def notify(self, title: str, message: str, warning: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.showMessage(title, message, icon, 8000)


# --------------------------------------------------------------------------- #
# Updates
# --------------------------------------------------------------------------- #
def parse_version(v: str) -> tuple[int, ...]:
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); (0,) when unparseable."""
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


class UpdateChecker(QObject):
    """Checks GitHub Releases in a daemon thread; signals are delivered on Qt's thread.

    update_available(version, url) when a newer release exists; check_done(found)
    after every check, so callers can re-enable controls or report "up to date".
    """

    update_available = Signal(str, str)
    check_done = Signal(bool)

    def check_in_background(self) -> None:
        threading.Thread(target=self._check, daemon=True, name="update-check").start()

    def _check(self) -> None:
        found = False
        try:
            req = urllib.request.Request(
                RELEASES_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"flight-monitor/{__version__}",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            tag = str(data.get("tag_name", "")).lstrip("v")
            url = str(data.get("html_url", ""))
            if tag and url and parse_version(tag) > parse_version(__version__):
                logger.info(f"Update available: v{tag} (running v{__version__}).")
                found = True
                self.update_available.emit(tag, url)
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.debug("Update check failed: %s", e)
        finally:
            self.check_done.emit(found)


class UpdateDialog(QDialog):
    """Offers to open the release page of a newer version."""

    def __init__(self, version: str, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self.setWindowTitle("Update available")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Flight Monitor {version}</b> is available."))
        layout.addWidget(QLabel(f"You are running version {__version__}."))
        layout.addWidget(QLabel("Open the release page to download the installer?"))
        buttons = QDialogButtonBox()
        buttons.addButton(QPushButton("Open release page"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QPushButton("Later"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._open)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open(self) -> None:
        webbrowser.open(self._url)
        self.accept()
