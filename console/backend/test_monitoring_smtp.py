"""SMTP 阶段与投递确认回归；所有会话均为假对象，不连接邮件服务。"""
import smtplib

import pytest

import monitoring_alert as ma


@pytest.fixture()
def smtp_config(monkeypatch):
    monkeypatch.setattr(ma, "smtp_config", lambda: {
        "host": "smtp.invalid", "port": 465, "user": "private-user",
        "password": "private-password", "sender": "private-sender",
        "recipients": ["private-recipient"],
    })


class Session:
    def __init__(self, *, login_error=None, submit_error=None, quit_error=None,
                 quit_code=221, refused=None):
        self.login_error = login_error
        self.submit_error = submit_error
        self.quit_error = quit_error
        self.quit_code = quit_code
        self.refused = refused or {}
        self.submits = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        # 与真实smtplib一致：QUIT直接断连已被忽略，其他退出错误仍会上抛。
        try:
            code, message = self.quit()
            if code != 221:
                raise smtplib.SMTPResponseException(code, message)
        except smtplib.SMTPServerDisconnected:
            pass
        finally:
            self.close()

    def login(self, *args):
        if self.login_error:
            raise self.login_error
        return 235, b"ok"

    def sendmail(self, *args):
        self.submits += 1
        if self.submit_error:
            raise self.submit_error
        return self.refused

    def quit(self):
        if self.quit_error:
            raise self.quit_error
        return self.quit_code, b"bye"

    def close(self):
        self.closed = True


def sessions(monkeypatch, *items):
    pending = list(items)
    calls = []

    def connect(*args, **kwargs):
        calls.append(True)
        item = pending.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ma.smtplib, "SMTP_SSL", connect)
    return calls


@pytest.mark.parametrize("quit_error,category", [
    (smtplib.SMTPServerDisconnected("private-password"), "disconnected"),
    (TimeoutError("private-password"), "timeout"),
    (smtplib.SMTPResponseException(451, b"private-password"), "smtp_error"),
])
def test_accepted_message_survives_cleanup_error(smtp_config, monkeypatch, quit_error, category):
    session = Session(quit_error=quit_error)
    calls = sessions(monkeypatch, session)
    result = ma.smtp_send("subject", "body")
    assert result["ok"] is True
    assert result["stage"] == "accepted"
    assert result["cleanup_warning"] == category
    assert session.submits == 1 and len(calls) == 1 and session.closed
    assert "private" not in str(result)


def test_accepted_message_survives_non_221_quit_response(smtp_config, monkeypatch):
    session = Session(quit_code=451)
    calls = sessions(monkeypatch, session)
    result = ma.smtp_send("subject", "body")
    assert result["ok"] is True and result["stage"] == "accepted"
    assert result["cleanup_warning"] == "unexpected_quit_response"
    assert session.submits == 1 and len(calls) == 1 and session.closed


def test_connect_failure_retries_before_submitting(smtp_config, monkeypatch):
    session = Session()
    calls = sessions(monkeypatch, TimeoutError("private-password"), session)
    result = ma.smtp_send("subject", "body")
    assert result["ok"] is True and result["attempts"] == 2
    assert len(calls) == 2 and session.submits == 1
    assert result["pre_submit_failures"] == [{
        "stage": "connect", "category": "timeout", "smtp_code": None,
    }]


def test_authentication_rejection_is_not_retried(smtp_config, monkeypatch):
    session = Session(login_error=smtplib.SMTPAuthenticationError(535, b"private-password"))
    calls = sessions(monkeypatch, session)
    result = ma.smtp_send("subject", "body")
    assert result["ok"] is False and result["stage"] == "authenticate"
    assert result["category"] == "authentication" and result["smtp_code"] == 535
    assert len(calls) == 1 and session.submits == 0 and session.closed
    assert "private" not in str(result)


def test_auth_disconnect_can_retry_without_duplicate_submission(smtp_config, monkeypatch):
    first = Session(login_error=smtplib.SMTPServerDisconnected("private-password"))
    second = Session()
    calls = sessions(monkeypatch, first, second)
    result = ma.smtp_send("subject", "body")
    assert result["ok"] is True and result["attempts"] == 2
    assert len(calls) == 2 and first.closed and second.closed
    assert first.submits == 0 and second.submits == 1


def test_submit_disconnect_is_ambiguous_and_never_retried(smtp_config, monkeypatch):
    session = Session(submit_error=smtplib.SMTPServerDisconnected("private-recipient"))
    calls = sessions(monkeypatch, session)
    result = ma.smtp_send("subject", "body")
    assert result["ok"] is False and result["stage"] == "submit"
    assert result["category"] == "disconnected" and result["delivery_unknown"] is True
    assert len(calls) == 1 and session.submits == 1 and session.closed
    assert "private" not in str(result)


def test_partial_recipient_refusal_is_not_success_or_retried(smtp_config, monkeypatch):
    session = Session(refused={"private-recipient": (550, b"private-password")})
    calls = sessions(monkeypatch, session)
    result = ma.smtp_send("subject", "body")
    assert result["ok"] is False and result["category"] == "recipients_refused"
    assert result["accepted_some"] is True and result["smtp_code"] == 550
    assert len(calls) == 1 and session.submits == 1
    assert "private" not in str(result)


def test_connect_retry_is_bounded(smtp_config, monkeypatch):
    calls = sessions(monkeypatch, TimeoutError("private"), TimeoutError("private"))
    result = ma.smtp_send("subject", "body")
    assert result["ok"] is False and result["attempts"] == 2
    assert result["stage"] == "connect" and len(calls) == 2


def test_delivery_summary_excludes_message_and_credentials():
    rows = [{"machine": "nas", "rule": "Status", "action": "opened", "delivery": {
        "ok": False, "stage": "authenticate", "category": "authentication",
        "smtp_code": 535, "attempts": 1, "error": "private-password",
        "recipients": ["private-recipient"], "body": "private-body",
    }}]
    result = ma.delivery_summary(rows)
    assert result == [{
        "machine": "nas", "rule": "Status", "action": "opened", "ok": False,
        "stage": "authenticate", "category": "authentication", "smtp_code": 535,
        "attempts": 1,
    }]
    assert "private" not in str(result)
