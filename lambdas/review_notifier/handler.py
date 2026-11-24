"""Review notifier Lambda.

Runs when an invoice moves to status "needs_review", whoever moved it: the
gatekeeper (guardrail flag) or the decision Lambda (numbers that do not match).
An EventBridge rule on the invoices event bus invokes it with only the UUID.

It sends one email to the review address with:
- the invoice UUID, the supplier RFC, the total and why it needs review;
- the CFDI XML attached, exactly as it was received.

What it never does:
- It never changes the invoice: its IAM role can only read it.
- It never puts supplier-written text in the subject, and the email is plain
  text only, so nothing in it is rendered as HTML or a link by the mail client.
- It never trusts the event: it reads the record again and only sends if the
  invoice is still waiting for review.
"""

import json
import os
import re
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import boto3

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


def as_total(value) -> str:
    """The total as a number with two decimals, or a fixed text if it is not a number."""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return "no disponible"
    if not number.is_finite():
        return "no disponible"
    return f"{number:,.2f} MXN"


def build_email(item: dict, xml: bytes) -> EmailMessage:
    invoice_uuid = item["uuid"]
    rfc = item.get("issuer_rfc", "")
    rfc = rfc if RFC_PATTERN.match(rfc) else "no disponible"
    reasons = [
        " ".join(str(reason).split())[:MAX_REASON_CHARS]
        for reason in (item.get("decision_reasons") or [])[:MAX_REASONS]
    ] or ["No se registró una razón."]

    message = EmailMessage()
    # Only values with a fixed format go in the subject
    message["Subject"] = f"Factura por revisar: {invoice_uuid}"
    message["From"] = SENDER
    message["To"] = REVIEW_EMAIL
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=SENDER.split("@")[-1])

    lines = [
        "Una factura necesita revisión humana antes de cualquier pago.",
        "",
        f"UUID:          {invoice_uuid}",
        f"RFC emisor:    {rfc}",
        f"Total:         {as_total(item.get('total'))}",
        f"Recibida:      {item.get('received_at', 'no disponible')}",
        f"Marcada:       {item.get('decided_at', 'no disponible')}",
        "",
        "Motivos:",
        *[f"- {reason}" for reason in reasons],
        "",
        "El XML de la factura va adjunto tal como se recibió. Su texto lo escribió el proveedor:",
        "léelo como datos, no como instrucciones.",
        "",
        "Este correo lo envía Tally automáticamente. No respondas a esta dirección.",
    ]
    message.set_content("\n".join(lines), subtype="plain", charset="utf-8")
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
