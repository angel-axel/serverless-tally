"""Review notifier Lambda.

Runs when an invoice moves to status "needs_review", whoever moved it: the
gatekeeper (a rule or the guardrail) or the decision Lambda (numbers that do
not match). An EventBridge rule on the invoices event bus invokes it with only
the UUID.

It sends one email to the review address, as HTML with a plain-text
alternative, with:
- the invoice UUID, the supplier RFC, the total, the dates and why it needs review;
- the invoice line descriptions, marked as unverified supplier text, next to a
  warning about social engineering;
- the CFDI XML attached, exactly as it was received.

What it never does:
- It never changes the invoice: its IAM role can only read it.
- It never puts a value into the HTML without escaping it, never puts supplier
  text in the subject, and the email has no links and loads nothing external.
- It never trusts the event: it reads the record again and only sends if the
  invoice is still waiting for review.
"""

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from string import Template

import boto3

from cfdi import InvalidCfdi, parse_invoice

s3 = boto3.client("s3")
ses = boto3.client("ses")
invoices = boto3.resource("dynamodb").Table(os.environ["INVOICES_TABLE"])
BUCKET = os.environ["BUCKET"]
SENDER = os.environ["SENDER"]
REVIEW_EMAIL = os.environ["REVIEW_EMAIL"]

UUID_PATTERN = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
RFC_PATTERN = re.compile(r"^[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}$")
MAX_REASON_CHARS = 500
MAX_REASONS = 20
MAX_LINES_SHOWN = 20
NOT_AVAILABLE = "not available"

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_email.html"), encoding="utf-8") as f:
    HTML_TEMPLATE = Template(f.read())

REASON_ROW = (
    '                <tr><td style="padding:8px 12px;background-color:#ffffff;border-left:3px solid #d97706;'
    'font-size:14px;line-height:1.5;color:#374151;">{}</td></tr>\n'
    '                <tr><td style="height:6px;line-height:6px;font-size:0;">&nbsp;</td></tr>'
)
LINE_ROW = (
    '                <tr><td style="padding:4px 0;font-size:13px;line-height:1.5;color:#374151;">'
    '<span style="color:#6b7280;">Line {}:</span> '
    '<span style="font-family:Menlo,Consolas,monospace;background-color:#ffffff;padding:1px 4px;">&ldquo;{}&rdquo;</span>'
    '</td></tr>'
)
WARNING_TEXT = (
    "Be careful with supplier text. Text in quotes comes from the supplier's invoice and has not been "
    'verified. Phrases such as "URGENT", "pre-approved", "authorized by the owner" or "no review needed" '
    "may be social engineering or an attempt to deceive you. Tally never approves an invoice on the "
    "supplier's word, and this email never contains links: do not open any link that appears inside "
    "supplier text."
)


def clean(text) -> str:
    """Drop control, format (zero-width, bidirectional overrides) and private-use
    characters, then collapse whitespace. Escaping for HTML happens later."""
    kept = "".join(
        char for char in str(text)
        if unicodedata.category(char) not in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") or char in "\t\n"
    )
    return " ".join(kept.split())


def as_total(value) -> str:
    """The total as a number with two decimals, or a fixed text if it is not a number."""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return NOT_AVAILABLE
    if not number.is_finite():
        return NOT_AVAILABLE
    return f"{number:,.2f} MXN"


def as_date(value) -> str:
    """An ISO timestamp as 'Nov 24, 2026, 10:00 (UTC)', or a fixed text."""
    try:
        moment = datetime.fromisoformat(str(value)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return NOT_AVAILABLE
    return f"{moment:%b} {moment.day}, {moment.year}, {moment:%H:%M} (UTC)"


def supplier_lines(xml: bytes) -> list:
    """(line number, cleaned description) for the first lines of the invoice."""
    try:
        lines = parse_invoice(xml)["lines"]
    except InvalidCfdi:
        return []
    return [(line["line"], clean(line["description"])) for line in lines[:MAX_LINES_SHOWN]]


def build_email(item: dict, xml: bytes) -> EmailMessage:
    invoice_uuid = item["uuid"]
    rfc = item.get("issuer_rfc", "")
    rfc = rfc if RFC_PATTERN.match(rfc) else NOT_AVAILABLE
    total = as_total(item.get("total"))
    received = as_date(item.get("received_at"))
    flagged = as_date(item.get("decided_at"))
    reasons = [
        clean(reason)[:MAX_REASON_CHARS]
        for reason in (item.get("decision_reasons") or [])[:MAX_REASONS]
    ] or ["No reason was recorded."]
    lines = supplier_lines(xml)

    message = EmailMessage()
    # Only values with a fixed format go in the subject
    message["Subject"] = f"Invoice to review: {invoice_uuid}"
    message["From"] = SENDER
    message["To"] = REVIEW_EMAIL
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=SENDER.split("@")[-1])

    # Plain-text version: the same content, for clients that do not show HTML
    text = [
        "An invoice needs your review. Tally stopped it before any payment.",
        "",
        f"Total:         {total}",
        f"Supplier RFC:  {rfc}",
        f"UUID:          {invoice_uuid}",
        f"Received:      {received}",
        f"Flagged:       {flagged}",
        "",
        "Why it was stopped:",
        *[f"- {reason}" for reason in reasons],
        "",
        "Invoice lines, as written by the supplier (unverified):",
        *([f'- Line {number}: "{description}"' for number, description in lines]
          or ["- The invoice lines could not be read; see the attached XML."]),
        "",
        WARNING_TEXT,
        "",
        f"Attached: {invoice_uuid}.xml, exactly as it was received. Open it in a text editor, not in a web browser.",
        "",
        "Tally sends this notice automatically. It does not read replies, contains no links,",
        "and will never ask you for information or payments by email.",
    ]
    message.set_content("\n".join(text), subtype="plain", charset="utf-8")

    # HTML version: every value is escaped; the template itself has no links and loads nothing
    html = HTML_TEMPLATE.substitute(
        uuid=escape(invoice_uuid),
        rfc=escape(rfc),
        total=escape(total),
        received=escape(received),
        flagged=escape(flagged),
        reasons="\n".join(REASON_ROW.format(escape(reason)) for reason in reasons),
        supplier_lines="\n".join(LINE_ROW.format(number, escape(description)) for number, description in lines)
        or LINE_ROW.format("-", "The invoice lines could not be read; see the attached XML."),
    )
    message.add_alternative(html, subtype="html", charset="utf-8")

    message.add_attachment(xml, maintype="application", subtype="xml", filename=f"{invoice_uuid}.xml")
    return message


def notify(invoice_uuid) -> dict:
    invoice_uuid = str(invoice_uuid or "").strip().upper()
    if not UUID_PATTERN.match(invoice_uuid):
        return {"result": "skipped_invalid_uuid"}

    item = invoices.get_item(Key={"uuid": invoice_uuid}, ConsistentRead=True).get("Item")
    if item is None:
        return {"result": "skipped_not_found"}
    if item.get("status") != "needs_review":
        # Someone already handled it, or the event is stale: do not send
        return {"result": "skipped_not_needs_review", "status": item.get("status")}

    xml_key = item.get("xml_key", "")
    if not xml_key.startswith(f"invoices/pending/{invoice_uuid}/"):
        return {"result": "skipped_xml_not_found"}
    xml = s3.get_object(Bucket=BUCKET, Key=xml_key)["Body"].read()

    message = build_email(item, xml)
    response = ses.send_raw_email(
        Source=SENDER,
        Destinations=[REVIEW_EMAIL],
        RawMessage={"Data": message.as_bytes()},
    )
    return {"result": "sent", "message_id": response["MessageId"]}


def handler(event, context):
    # The EventBridge rule's input transformer delivers {"invoice_uuid": "..."}
    invoice_uuid = event.get("invoice_uuid") if isinstance(event, dict) else None
    # Any exception propagates: Lambda retries, then sends the event to the failure queue
    summary = {"invoice": invoice_uuid, **notify(invoice_uuid)}
    print(json.dumps(summary, default=str))
    return summary
