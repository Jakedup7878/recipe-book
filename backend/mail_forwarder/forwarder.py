"""Forwards mail that SES receives for support@ to the owner's inbox.

SES stores each message in the mail bucket first, then calls this function with the
receipt (no message body). SES can only send from our verified domain, so the copy comes
from support@ with the original sender as Reply-To: replying in the inbox goes straight
back to them. Mail SES flags as a virus or spam, or that fails a sender's DMARC `reject`
policy, stays in the bucket and isn't forwarded.

The destination address is read from Parameter Store for each message, so it's in neither
the repository nor the CloudFormation template, and a change there applies to the next one.
"""

import email
import email.header
import email.policy
import email.utils
import json
import os
import re

import boto3

S3 = boto3.client("s3")
SES = boto3.client("sesv2")
SSM = boto3.client("ssm")

# Belong to the original sender's server or signature: SES would reject the copy, or the
# old signature would fail checks once we resend it. From and Reply-To are set again below.
DROP_HEADERS = ("From", "Reply-To", "Sender", "Return-Path", "Message-ID", "DKIM-Signature")

def forward_to():
    return SSM.get_parameter(Name=os.environ["FORWARD_TO_PARAMETER"])["Parameter"]["Value"].strip()


def blocked_reason(receipt):
    """Why a message shouldn't be forwarded, or None to forward it."""
    if receipt.get("virusVerdict", {}).get("status") == "FAIL":
        return "virus"
    if receipt.get("spamVerdict", {}).get("status") == "FAIL":
        return "spam"
    if (receipt.get("dmarcVerdict", {}).get("status") == "FAIL"
            and receipt.get("dmarcPolicy", "").lower() == "reject"):
        return "dmarc-reject"
    return None


def _unfold(value):
    return re.sub(r"\r?\n[ \t]+", " ", value or "").strip()


def _display_name(address_header):
    """The sender's name as plain text (decoding =?utf-8?...?= words), else their address."""
    name, address = email.utils.parseaddr(address_header)
    if name:
        name = str(email.header.make_header(email.header.decode_header(name)))
    return name or address or "Unknown sender"


def rewrite(raw, from_address):
    """The original message with From/Reply-To changed so SES can send it from our domain."""
    # compat32 is the email package's round-trip mode: the body and the headers we don't
    # touch are written back as they arrived.
    message = email.message_from_bytes(raw, policy=email.policy.compat32)
    original_from = _unfold(message.get("From"))
    reply_to = _unfold(message.get("Reply-To")) or original_from
    for header in DROP_HEADERS:
        del message[header]
    name = f"{_display_name(original_from)} via Family Foodie Book"
    message["From"] = email.utils.formataddr((name, from_address), charset="utf-8")
    if reply_to:
        message["Reply-To"] = reply_to
    return message.as_bytes()


def handler(event, context):
    from_address = os.environ["FROM_ADDRESS"]
    for record in event["Records"]:
        notice = record["ses"]
        message_id = notice["mail"]["messageId"]
        # Log the message ID and the outcome, never the sender or the content.
        reason = blocked_reason(notice["receipt"])
        if reason:
            print(json.dumps({"messageId": message_id, "forwarded": False, "reason": reason}))
            continue
        key = os.environ["MAIL_PREFIX"] + message_id
        raw = S3.get_object(Bucket=os.environ["MAIL_BUCKET"], Key=key)["Body"].read()
        try:
            SES.send_email(
                FromEmailAddress=from_address,
                Destination={"ToAddresses": [forward_to()]},
                Content={"Raw": {"Data": rewrite(raw, from_address)}},
            )
        except SES.exceptions.MessageRejected as error:
            # Permanent (too large, malformed): retrying won't help. The original stays in S3.
            print(json.dumps({"messageId": message_id, "forwarded": False,
                              "reason": "rejected", "error": str(error)}))
            continue
        print(json.dumps({"messageId": message_id, "forwarded": True}))
