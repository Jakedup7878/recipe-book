"""The support@ forwarder: header rewriting, spam/virus filtering, and the handler's calls."""

import email
import email.policy
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import devtools  # noqa: E402

devtools.use_fake_aws("recipes-test")  # boto3 clients are created at import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mail_forwarder"))
import forwarder  # noqa: E402

SUPPORT = "support@example.com"
ORIGINAL = (
    b"Return-Path: <bounce@sender.example>\r\n"
    b"DKIM-Signature: v=1; a=rsa-sha256; d=sender.example; b=abc\r\n"
    b"From: =?utf-8?q?J=C3=B6rg_Cook?= <jorg@sender.example>\r\n"
    b"To: support@example.com\r\n"
    b"Subject: Please delete my account\r\n"
    b"Message-ID: <original@sender.example>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hi, please delete my account.\r\n"
)


def parse(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def test_rewrite_sends_from_support_and_replies_to_the_sender():
    message = parse(forwarder.rewrite(ORIGINAL, SUPPORT))
    assert message["From"].addresses[0].addr_spec == SUPPORT
    assert message["From"].addresses[0].display_name == "Jörg Cook via Family Foodie Book"
    assert message["Reply-To"].addresses[0].addr_spec == "jorg@sender.example"
    assert message["To"] == "support@example.com"
    assert message["Subject"] == "Please delete my account"
    for header in ("Return-Path", "DKIM-Signature", "Message-ID"):
        assert header not in message, header
    assert message.get_content() == "Hi, please delete my account.\n"


def test_rewrite_keeps_an_existing_reply_to():
    raw = ORIGINAL.replace(b"To: ", b"Reply-To: Desk <desk@sender.example>\r\nTo: ", 1)
    message = parse(forwarder.rewrite(raw, SUPPORT))
    assert message["Reply-To"].addresses[0].addr_spec == "desk@sender.example"
    assert len(message.get_all("Reply-To")) == 1


def test_rewrite_falls_back_to_the_address_without_a_name():
    raw = ORIGINAL.replace(b"From: =?utf-8?q?J=C3=B6rg_Cook?= <jorg@sender.example>", b"From: jorg@sender.example")
    message = parse(forwarder.rewrite(raw, SUPPORT))
    assert message["From"].addresses[0].display_name == "jorg@sender.example via Family Foodie Book"


@pytest.mark.parametrize("receipt, reason", [
    ({"spamVerdict": {"status": "PASS"}, "virusVerdict": {"status": "PASS"}}, None),
    ({"spamVerdict": {"status": "GRAY"}, "virusVerdict": {"status": "GRAY"}}, None),
    ({"spamVerdict": {"status": "FAIL"}, "virusVerdict": {"status": "PASS"}}, "spam"),
    ({"spamVerdict": {"status": "PASS"}, "virusVerdict": {"status": "FAIL"}}, "virus"),
    ({"dmarcVerdict": {"status": "FAIL"}, "dmarcPolicy": "reject"}, "dmarc-reject"),
    ({"dmarcVerdict": {"status": "FAIL"}, "dmarcPolicy": "none"}, None),
])
def test_blocked_reason(receipt, reason):
    assert forwarder.blocked_reason(receipt) == reason


class FakeS3:
    def __init__(self):
        self.keys = []

    def get_object(self, Bucket, Key):
        self.keys.append((Bucket, Key))
        return {"Body": io.BytesIO(ORIGINAL)}


class FakeSES:
    class exceptions:
        class MessageRejected(Exception):
            pass

    def __init__(self):
        self.sent = []

    def send_email(self, **kwargs):
        self.sent.append(kwargs)


class FakeSSM:
    def get_parameter(self, Name):
        assert Name == "/test/forward-to"
        return {"Parameter": {"Value": " owner@inbox.example \n"}}


@pytest.fixture
def fwd(monkeypatch):
    monkeypatch.setenv("FROM_ADDRESS", SUPPORT)
    monkeypatch.setenv("MAIL_BUCKET", "mail-bucket")
    monkeypatch.setenv("MAIL_PREFIX", "support/")
    monkeypatch.setenv("FORWARD_TO_PARAMETER", "/test/forward-to")
    monkeypatch.setattr(forwarder, "S3", FakeS3())
    monkeypatch.setattr(forwarder, "SES", FakeSES())
    monkeypatch.setattr(forwarder, "SSM", FakeSSM())
    return forwarder


def ses_event(spam="PASS"):
    return {"Records": [{"ses": {
        "mail": {"messageId": "abc123"},
        "receipt": {"spamVerdict": {"status": spam}, "virusVerdict": {"status": "PASS"}},
    }}]}


def test_handler_forwards_the_stored_message(fwd):
    fwd.handler(ses_event(), None)
    assert fwd.S3.keys == [("mail-bucket", "support/abc123")]
    [sent] = fwd.SES.sent
    assert sent["FromEmailAddress"] == SUPPORT
    assert sent["Destination"] == {"ToAddresses": ["owner@inbox.example"]}
    assert parse(sent["Content"]["Raw"]["Data"])["Reply-To"].addresses[0].addr_spec == "jorg@sender.example"


def test_handler_keeps_spam_in_the_bucket_without_forwarding(fwd):
    fwd.handler(ses_event(spam="FAIL"), None)
    assert fwd.S3.keys == []
    assert fwd.SES.sent == []
