import datetime as dt
import sys

import pytest

import flight_monitor as fm
from engines import Offer, SweepResult

T = dt.time
D = dt.datetime


def test_parse_run_times():
    assert fm.parse_run_times("20:00, 8:00;08:00") == [T(8, 0), T(20, 0)]
    for bad in ("", "25:00", "8h"):
        with pytest.raises(ValueError):
            fm.parse_run_times(bad)


TIMES = [T(8, 0), T(20, 0)]


@pytest.mark.parametrize(
    "now, last, due",
    [
        (D(2026, 12, 1, 8, 0, 30), None, D(2026, 12, 1, 8, 0)),
        (D(2026, 12, 1, 8, 5), D(2026, 12, 1, 8, 0, 30), None),
        (D(2026, 12, 1, 10, 0), D(2026, 11, 30, 20, 0), D(2026, 12, 1, 8, 0)),
        (D(2026, 12, 1, 15, 0), D(2026, 11, 30, 20, 0), None),
        (D(2026, 12, 1, 1, 0), D(2026, 11, 30, 8, 0), D(2026, 11, 30, 20, 0)),
        (D(2026, 12, 1, 7, 59), None, None),
    ],
)
def test_due_run(now, last, due):
    assert fm.due_run(now, TIMES, last) == due


def test_next_run():
    assert fm.next_run(D(2026, 12, 1, 9, 0), TIMES) == D(2026, 12, 1, 20, 0)
    assert fm.next_run(D(2026, 12, 1, 21, 0), TIMES) == D(2026, 12, 2, 8, 0)


def test_save_settings_updates_only_given_keys(data_dir):
    fm.save_settings({"destination": "GIG"})
    fm.save_settings({"auto_run": True, "run_times": "07:30"})
    s = fm.load_settings()
    assert (s["destination"], s["auto_run"], s["run_times"]) == ("GIG", True, "07:30")


def test_load_settings_repairs_bad_schedule_values(data_dir):
    fm.save_settings({"email_when": "sometimes", "run_times": "noon"})
    s = fm.load_settings()
    assert s["email_when"] == "always" and s["run_times"] == fm.DEFAULTS["run_times"]


def test_state_round_trip(data_dir):
    fm.save_state(last_auto_run="2026-12-01T08:00:01")
    assert fm.load_state() == {"last_auto_run": "2026-12-01T08:00:01"}


def test_app_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIGHT_MONITOR_HOME", str(tmp_path))
    assert fm.app_home() == tmp_path
    monkeypatch.delenv("FLIGHT_MONITOR_HOME")
    assert fm.app_home() == fm.Path(fm.__file__).resolve().parent
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert fm.app_home() == tmp_path / "Roaming" / "flight-monitor"


def test_email_config_from_env_with_old_gmail_names(data_dir, monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "old@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh")
    monkeypatch.setenv("SMTP_PORT", "not-a-port")
    cfg = fm.email_config()
    assert (cfg.user, cfg.password, cfg.recipient) == (
        "old@gmail.com",
        "abcd efgh",
        "old@gmail.com",
    )
    assert (cfg.host, cfg.port, cfg.complete) == ("smtp.gmail.com", 465, True)
    monkeypatch.setenv("SMTP_USER", "new@example.com")
    monkeypatch.setenv("REPORT_EMAIL", "me@example.com")
    assert (fm.email_config().user, fm.email_config().recipient) == (
        "new@example.com",
        "me@example.com",
    )


def test_save_email_config_keeps_the_password_out_of_files(data_dir):
    fm.ENV_FILE.write_text("GMAIL_APP_PASSWORD=old\nSMTP_PASSWORD=older\n", encoding="utf-8")
    fm.load_dotenv()
    fm.save_email_config(fm.EmailConfig("a@x.y", "s3cret", "b@x.y", "smtp.x.y", 587))
    text = fm.ENV_FILE.read_text(encoding="utf-8")
    assert "SMTP_USER=a@x.y" in text and "SMTP_PORT=587" in text
    assert "s3cret" not in text and "PASSWORD" not in text
    assert fm._keyring().get_password(fm.KEYRING_SERVICE, "a@x.y") == "s3cret"
    assert fm.email_config() == fm.EmailConfig("a@x.y", "s3cret", "b@x.y", "smtp.x.y", 587)


def test_save_email_config_without_credential_store_changes_nothing(data_dir, monkeypatch):
    class NoStore:
        def set_password(self, *a):
            raise RuntimeError("No recommended backend was available")

    monkeypatch.setattr(fm, "_keyring", lambda: NoStore())
    fm.ENV_FILE.write_text("SMTP_USER=old@x.y\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="backend"):
        fm.save_email_config(fm.EmailConfig("a@x.y", "pw"))
    assert fm.ENV_FILE.read_text(encoding="utf-8") == "SMTP_USER=old@x.y\n"


def test_password_from_env_when_the_store_has_none(data_dir, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "a@x.y")
    monkeypatch.setenv("SMTP_PASSWORD", "from-env")
    assert fm.email_config().password == "from-env"
    fm._keyring().set_password(fm.KEYRING_SERVICE, "a@x.y", "from-store")
    assert fm.email_config().password == "from-store"


class FakeSMTP:
    calls: list = []

    def __init__(self, host, port, **kw):
        self.calls.append(("connect", type(self).__name__, host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, sender, to, msg):
        self.calls.append(("sendmail", sender, tuple(to)))


class FakeSMTP_SSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.calls = []
    monkeypatch.setattr(fm.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(fm.smtplib, "SMTP_SSL", FakeSMTP_SSL)
    return FakeSMTP.calls


def test_send_email_ssl_on_465(data_dir, smtp):
    fm.send_email("", "s", "b", fm.EmailConfig("me@gmail.com", "abcd efgh ijkl mnop"))
    assert smtp == [
        ("connect", "FakeSMTP_SSL", "smtp.gmail.com", 465),
        ("login", "me@gmail.com", "abcdefghijklmnop"),
        ("sendmail", "me@gmail.com", ("me@gmail.com",)),
    ]


def test_send_email_starttls_on_other_ports(data_dir, smtp):
    fm.send_email("to@x.y", "s", "b", fm.EmailConfig("me@x.y", "pw", host="mail.x.y", port=587))
    assert smtp[0] == ("connect", "FakeSMTP", "mail.x.y", 587) and smtp[1] == ("starttls",)


def test_send_email_skips_without_account(data_dir, smtp):
    fm.send_email("to@x.y", "s", "b")
    assert smtp == []


def result(light="", new_low=False, best=True, failure=""):
    search = fm.build_search(
        "AMS", "GRU", ("KL",), 0, dt.date(2026, 12, 1), dt.date(2026, 12, 2), 15, 1, ("EUR",)
    )
    r = fm.RunResult(search, light=light, new_low=new_low, failure=failure)
    if best:
        r.best["EUR"] = Offer("EUR", dt.date(2026, 12, 1), dt.date(2026, 12, 16), 995.0)
    return r


@pytest.mark.parametrize(
    "r, always, low, never",
    [
        (result("LOW"), True, True, False),
        (result("NORMAL"), True, False, False),
        (result("HIGH", new_low=True), True, True, False),
        (result(best=False), True, True, False),
        (result("NORMAL", failure="quota"), True, True, False),
    ],
)
def test_wants_email(r, always, low, never):
    assert (fm.wants_email(r, "always"), fm.wants_email(r, "low"), fm.wants_email(r, "never")) == (
        always,
        low,
        never,
    )


@pytest.fixture
def sent(monkeypatch):
    mails = []
    monkeypatch.setattr(fm, "send_email", lambda to, subject, body: mails.append(subject))
    return mails


def run_with(monkeypatch, offers, **kw):
    monkeypatch.setattr(
        fm, "sweep", lambda *a, **k: SweepResult(offers=offers, engine="google-calendar")
    )
    search = fm.build_search(
        "AMS", "GRU", ("KL",), 0, dt.date(2026, 12, 1), dt.date(2026, 12, 2), 15, 1, ("EUR",)
    )
    return fm.run_monitor(search, send=True, record=False, **kw)


def eur(price):
    return Offer("EUR", dt.date(2026, 12, 1), dt.date(2026, 12, 16), float(price))


def test_email_only_on_low(data_dir, monkeypatch, sent):
    run_with(monkeypatch, [eur(1100)], email_when="low", low_eur=1000, high_eur=1250)
    assert sent[0].startswith("[NORMAL]")
    sent.clear()
    fm.update_best([fm.offer_row(eur(900), "t")])
    run_with(monkeypatch, [eur(1100)], email_when="low", low_eur=1000, high_eur=1250)
    assert sent == []
    run_with(monkeypatch, [eur(950)], email_when="low", low_eur=1000, high_eur=1250)
    assert len(sent) == 1 and sent[0].startswith("[LOW]")
    run_with(monkeypatch, [], email_when="low")
    assert sent[-1].startswith("[WARN]")


def test_email_never(data_dir, monkeypatch, sent):
    run_with(monkeypatch, [eur(900)], email_when="never")
    run_with(monkeypatch, [], email_when="never")
    assert sent == []


def test_smtp_failure_does_not_break_the_run(data_dir, monkeypatch):
    def boom(*a, **k):
        raise OSError("smtp down")

    monkeypatch.setattr(fm, "send_email", boom)
    assert run_with(monkeypatch, []).best == {}
    assert run_with(monkeypatch, [eur(900)]).best["EUR"].price == 900
