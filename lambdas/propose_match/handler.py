"""propose_match tool (AgentCore Gateway Lambda target).

The only tool that writes. It records the agent's proposal of which invoice
line corresponds to which purchase order line, and moves the invoice from
"received" to "match_proposed". The decision Lambda takes it from there.

The agent's proposal is checked before anything is written:
- The invoice exists and is still waiting for a proposal.
- Every invoice line appears exactly once (matched or null).
- The order belongs to the invoice's supplier (the key is built from the
  invoice's RFC, never from the agent) and is open.
- Every order line exists and is used at most once.
- With no order, every line must be unmatched.

If a check fails, nothing is written and the agent gets the reason, so it can
correct the proposal in the same run. The write itself is conditional on the
invoice still being "received", so a retry never overwrites a proposal.
"""

import json
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from cfdi import InvalidCfdi, parse_invoice

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
invoices = dynamodb.Table(os.environ["INVOICES_TABLE"])
purchase_orders = dynamodb.Table(os.environ["PURCHASE_ORDERS_TABLE"])
BUCKET = os.environ["BUCKET"]

UUID_PATTERN = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_NOTES = 500


class ProposalError(Exception):
    """The proposal is not acceptable; the message is returned to the agent."""


def as_line_number(value, field):
    # bool is a subclass of int in Python: reject it explicitly
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProposalError(f"{field} must be a positive integer or null.")
    return value


def validate(arguments: dict) -> dict:
    invoice_uuid = str(arguments.get("invoice_uuid") or "").strip().upper()
    if not UUID_PATTERN.match(invoice_uuid):
        raise ProposalError("invoice_uuid must be a CFDI UUID.")

    order_id = arguments.get("order_id")
    if order_id is not None:
        order_id = str(order_id).strip()
        if not ORDER_ID_PATTERN.match(order_id):
            raise ProposalError("order_id must be an order ID returned by find_open_orders, or null.")

    lines = arguments.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ProposalError("lines must be a non-empty list of {invoice_line, order_line}.")

    notes = " ".join(str(arguments.get("notes") or "").split())[:MAX_NOTES]

    # --- The invoice: it must still be waiting for a proposal ---------------
    invoice = invoices.get_item(Key={"uuid": invoice_uuid}, ConsistentRead=True).get("Item")
    if invoice is None:
        raise ProposalError(f"Invoice {invoice_uuid} does not exist.")
    if invoice.get("status") != "received":
        raise ProposalError(f"Invoice {invoice_uuid} is not waiting for a match proposal.")

    xml_key = invoice.get("xml_key", "")
    if not xml_key.startswith(f"invoices/pending/{invoice_uuid}/"):
        raise ProposalError("The stored invoice file could not be found.")
    try:
        cfdi = parse_invoice(s3.get_object(Bucket=BUCKET, Key=xml_key)["Body"].read())
    except InvalidCfdi as exc:
        raise ProposalError("The stored invoice file could not be read.") from exc
    invoice_lines = {line["line"] for line in cfdi["lines"]}

    # --- Every invoice line exactly once -------------------------------------
    pairs, seen_invoice_lines, used_order_lines = [], set(), set()
    for entry in lines:
        if not isinstance(entry, dict):
            raise ProposalError("Each item in lines must be {invoice_line, order_line}.")
        invoice_line = as_line_number(entry.get("invoice_line"), "invoice_line")
        order_line = entry.get("order_line")
        if order_line is not None:
            order_line = as_line_number(order_line, "order_line")

        if invoice_line not in invoice_lines:
            raise ProposalError(f"Invoice line {invoice_line} does not exist in invoice {invoice_uuid}.")
        if invoice_line in seen_invoice_lines:
            raise ProposalError(f"Invoice line {invoice_line} appears more than once.")
        seen_invoice_lines.add(invoice_line)

        if order_line is not None:
            if order_line in used_order_lines:
                raise ProposalError(f"Order line {order_line} is matched to more than one invoice line.")
            used_order_lines.add(order_line)
        pairs.append({"invoice_line": invoice_line, "order_line": order_line})

    missing = sorted(invoice_lines - seen_invoice_lines)
    if missing:
        raise ProposalError(f"Every invoice line must be listed (use null when unmatched). Missing: {missing}.")

    # --- The order: same supplier, open, lines exist -------------------------
    if order_id is None:
        if used_order_lines:
            raise ProposalError("Without an order_id, every order_line must be null.")
    else:
        # The partition key comes from the invoice, never from the agent
        order = purchase_orders.get_item(
            Key={"supplier_rfc": invoice["issuer_rfc"], "order_id": order_id},
            ConsistentRead=True,
        ).get("Item")
        if order is None:
            raise ProposalError(f"Order {order_id} is not an order of this invoice's supplier.")
        if order.get("status") != "open":
            raise ProposalError(f"Order {order_id} is not open.")
        order_lines = {int(line["line"]) for line in order.get("lines", [])}
        unknown = sorted(used_order_lines - order_lines)
        if unknown:
            raise ProposalError(f"Order {order_id} has no line(s) {unknown}.")

    pairs.sort(key=lambda pair: pair["invoice_line"])
    return {
        "invoice_uuid": invoice_uuid,
        "proposal": {"order_id": order_id, "lines": pairs, "notes": notes},
    }


def record(invoice_uuid: str, proposal: dict) -> bool:
    """Store the proposal. Returns False if the invoice was no longer 'received'."""
    try:
        invoices.update_item(
            Key={"uuid": invoice_uuid},
            UpdateExpression="SET #status = :proposed, match_proposal = :proposal, proposed_at = :now",
            ConditionExpression="attribute_exists(#uuid) AND #status = :received",
            ExpressionAttributeNames={"#uuid": "uuid", "#status": "status"},
            ExpressionAttributeValues={
                ":proposed": "match_proposed",
                ":received": "received",
                ":proposal": proposal,
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


def propose_match(arguments: dict) -> dict:
    try:
        checked = validate(arguments)
    except ProposalError as exc:
        return {"error": str(exc)}

    if not record(checked["invoice_uuid"], checked["proposal"]):
        return {"error": f"Invoice {checked['invoice_uuid']} already has a proposal."}

    matched = sum(1 for pair in checked["proposal"]["lines"] if pair["order_line"] is not None)
    return {
        "result": "proposal_recorded",
        "invoice_uuid": checked["invoice_uuid"],
        "order_id": checked["proposal"]["order_id"],
        "matched_lines": matched,
        "total_lines": len(checked["proposal"]["lines"]),
        "note": "The proposal will be checked by a separate system. Nothing has been approved.",
    }


def handler(event, context):
    arguments = event if isinstance(event, dict) else {}
    result = propose_match(arguments)
    print(json.dumps({
        "tool": "propose_match",
        "invoice_uuid": arguments.get("invoice_uuid"),
        "order_id": arguments.get("order_id"),
        "result": result.get("result", "error"),
        "error": result.get("error"),
    }, default=str))
    return result
