"""Decision Lambda.

Triggered by the EventBridge rule "match_proposed" when an invoice moves to
"match_proposed". The rule delivers the stream record as {"Records": [record]}.
It checks the agent's proposal with plain arithmetic and moves the invoice to
"matched" or "needs_review". There is no model in this function.

Everything is read again from the source (the stored XML and the purchase
order), never from what the agent saw or said. The agent's proposal only says
which line goes with which; this function decides whether the numbers agree.

When an invoice matches, the invoiced quantities are added to the purchase
order in the same transaction as the invoice update, with conditions that the
order was not changed in between and that no line is invoiced beyond what was
ordered. The same order cannot be paid twice through two invoices.
"""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeSerializer

from cfdi import InvalidCfdi, parse_invoice

s3 = boto3.client("s3")
dynamodb_client = boto3.client("dynamodb")
dynamodb = boto3.resource("dynamodb")
INVOICES_TABLE = os.environ["INVOICES_TABLE"]
PURCHASE_ORDERS_TABLE = os.environ["PURCHASE_ORDERS_TABLE"]
invoices = dynamodb.Table(INVOICES_TABLE)
purchase_orders = dynamodb.Table(PURCHASE_ORDERS_TABLE)
BUCKET = os.environ["BUCKET"]
PRICE_TOLERANCE = Decimal(os.environ.get("PRICE_TOLERANCE", "0.01"))
# Invoices above this total always go to a person, even if everything matches
MAX_AUTO_MATCH_TOTAL = Decimal(os.environ.get("MAX_AUTO_MATCH_TOTAL", "10000"))

# CFDI amounts are rounded to cents; allow one cent per comparison
ROUNDING = Decimal("0.01")

serializer = TypeSerializer()


def av(value):
    """Python value -> DynamoDB AttributeValue (for the low-level transaction API)."""
    return serializer.serialize(value)


def check(invoice: dict, cfdi: dict, order: dict | None) -> list[str]:
    """Return every reason the invoice does not match. Empty list = matches."""
    reasons = []
    proposal = invoice.get("match_proposal") or {}
    pairs = proposal.get("lines") or []

    if cfdi["uuid"] != invoice["uuid"] or cfdi["issuer_rfc"] != invoice.get("issuer_rfc"):
        return ["The stored XML does not belong to this invoice record."]

    if cfdi["type"] != "I":
        reasons.append(f"Invoice type is {cfdi['type']!r}; only income invoices (I) are matched automatically.")

    # The gatekeeper applies the same limit to the declared total; this check
    # uses the total of the XML after every amount in it has been verified
    if cfdi["total"] >= MAX_AUTO_MATCH_TOTAL:
        reasons.append(
            f"Total {cfdi['total']} is at or above the automatic matching limit {MAX_AUTO_MATCH_TOTAL}; "
            "a person must review it.")

    if order is None:
        reasons.append("No open purchase order was matched to this invoice.")
        return reasons
    if order.get("status") != "open":
        reasons.append(f"Order {order['order_id']} is no longer open.")
    if cfdi["currency"] != order.get("currency", "MXN"):
        reasons.append(f"Currency {cfdi['currency']!r} differs from the order currency {order.get('currency', 'MXN')!r}.")

    # --- Invoice arithmetic (independent of the order) ------------------------
    line_sum = sum((line["amount"] for line in cfdi["lines"]), Decimal("0"))
    if abs(line_sum - cfdi["subtotal"]) > ROUNDING:
        reasons.append(f"SubTotal {cfdi['subtotal']} differs from the sum of the lines {line_sum}.")
    expected_total = cfdi["subtotal"] - cfdi["discount"] + cfdi["transferred_taxes"] - cfdi["withheld_taxes"]
    if abs(expected_total - cfdi["total"]) > ROUNDING:
        reasons.append(
            f"Total {cfdi['total']} differs from SubTotal - Descuento + taxes = {expected_total}.")

    # --- Line by line ----------------------------------------------------------
    invoice_lines = {line["line"]: line for line in cfdi["lines"]}
    order_lines = {int(line["line"]): line for line in order.get("lines", [])}
    invoiced = order.get("invoiced_quantities") or {}

    if {pair["invoice_line"] for pair in pairs} != set(invoice_lines):
        reasons.append("The proposal does not cover every invoice line exactly once.")

    for pair in pairs:
        number = int(pair["invoice_line"])
        line = invoice_lines.get(number)
        if line is None:
            continue
        if pair.get("order_line") is None:
            reasons.append(f"Invoice line {number} ({line['description'][:60]}) has no matching order line.")
            continue
        order_line = order_lines.get(int(pair["order_line"]))
        if order_line is None:
            reasons.append(f"Invoice line {number} points to order line {pair['order_line']}, which does not exist.")
            continue

        label = f"Invoice line {number} / order line {int(order_line['line'])}"
        expected_amount = (line["quantity"] * line["unit_price"]).quantize(ROUNDING)
        if abs(expected_amount - line["amount"]) > ROUNDING:
            reasons.append(f"{label}: Importe {line['amount']} differs from Cantidad x ValorUnitario = {expected_amount}.")

        pending = Decimal(order_line["quantity"]) - Decimal(invoiced.get(str(int(order_line["line"])), 0))
        if line["quantity"] > pending:
            reasons.append(f"{label}: invoiced quantity {line['quantity']} exceeds the pending quantity {pending}.")

        agreed = Decimal(order_line["unit_price"])
        if abs(line["unit_price"] - agreed) > PRICE_TOLERANCE:
            reasons.append(f"{label}: unit price {line['unit_price']} differs from the agreed price {agreed}.")

    return reasons


def record_needs_review(invoice_uuid: str, reasons: list[str], now: str) -> bool:
    try:
        invoices.update_item(
            Key={"uuid": invoice_uuid},
            UpdateExpression="SET #status = :review, decision_reasons = :reasons, decided_at = :now",
            ConditionExpression="#status = :proposed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":review": "needs_review", ":proposed": "match_proposed",
                ":reasons": reasons, ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False  # already decided (a retry)
        raise
    return True


def record_matched(invoice: dict, cfdi: dict, order: dict, now: str) -> None:
    """Mark the invoice matched and add the invoiced quantities to the order, atomically."""
    invoice_lines = {line["line"]: line for line in cfdi["lines"]}
    current = order.get("invoiced_quantities")
    new_totals = {str(int(line["line"])): Decimal((current or {}).get(str(int(line["line"])), 0))
                  for line in order["lines"]}
    for pair in invoice["match_proposal"]["lines"]:
        key = str(int(pair["order_line"]))
        new_totals[key] += invoice_lines[int(pair["invoice_line"])]["quantity"]

    fully_invoiced = all(new_totals[str(int(line["line"]))] >= Decimal(line["quantity"])
                         for line in order["lines"])

    # Optimistic lock: every line's invoiced quantity must still be what we
    # read. Each comparison is on a single number, so it behaves the same in
    # every DynamoDB implementation.
    names = {"#status": "status"}
    values = {":open": av("open"), ":order_status": av("closed" if fully_invoiced else "open")}
    if current is None:
        updates = ["invoiced_quantities = :totals"]
        conditions = ["attribute_not_exists(invoiced_quantities)"]
        values[":totals"] = av(new_totals)
    else:
        updates, conditions = [], []
        for index, (line_key, total) in enumerate(sorted(new_totals.items())):
            names[f"#l{index}"] = line_key
            values[f":n{index}"] = av(total)
            updates.append(f"invoiced_quantities.#l{index} = :n{index}")
            if line_key in current:
                values[f":c{index}"] = av(Decimal(current[line_key]))
                conditions.append(f"invoiced_quantities.#l{index} = :c{index}")
            else:
                conditions.append(f"attribute_not_exists(invoiced_quantities.#l{index})")
    order_update = "SET " + ", ".join(updates + ["#status = :order_status"])
    order_condition = " AND ".join(conditions + ["#status = :open"])

    dynamodb_client.transact_write_items(TransactItems=[
        {"Update": {
            "TableName": INVOICES_TABLE,
            "Key": {"uuid": av(invoice["uuid"])},
            "UpdateExpression": "SET #status = :matched, decision_reasons = :reasons, decided_at = :now",
            "ConditionExpression": "#status = :proposed",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":matched": av("matched"), ":proposed": av("match_proposed"),
                ":reasons": av([]), ":now": av(now),
            },
        }},
        {"Update": {
            "TableName": PURCHASE_ORDERS_TABLE,
            "Key": {"supplier_rfc": av(order["supplier_rfc"]), "order_id": av(order["order_id"])},
            "UpdateExpression": order_update,
            "ConditionExpression": order_condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }},
    ])


def decide(invoice_uuid: str) -> dict:
    invoice = invoices.get_item(Key={"uuid": invoice_uuid}, ConsistentRead=True).get("Item")
    if invoice is None or invoice.get("status") != "match_proposed":
        return {"result": "skipped_not_pending"}

    now = datetime.now(timezone.utc).isoformat()
    try:
        cfdi = parse_invoice(s3.get_object(Bucket=BUCKET, Key=invoice["xml_key"])["Body"].read())
    except InvalidCfdi:
        reasons = ["The stored XML could not be read."]
        record_needs_review(invoice_uuid, reasons, now)
        return {"result": "needs_review", "reasons": reasons}

    order_id = (invoice.get("match_proposal") or {}).get("order_id")
    order = None
    if order_id:
        order = purchase_orders.get_item(
            Key={"supplier_rfc": invoice["issuer_rfc"], "order_id": order_id},
            ConsistentRead=True,
        ).get("Item")

    reasons = check(invoice, cfdi, order)
    if reasons:
        recorded = record_needs_review(invoice_uuid, reasons, now)
        return {"result": "needs_review" if recorded else "skipped_already_decided", "reasons": reasons}

    try:
        record_matched(invoice, cfdi, order, now)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        # Either the invoice was already decided, or the order changed in
        # between (another invoice for the same order). Re-read to tell apart.
        latest = invoices.get_item(Key={"uuid": invoice_uuid}, ConsistentRead=True).get("Item") or {}
        if latest.get("status") != "match_proposed":
            return {"result": "skipped_already_decided"}
        # The order changed: fail so Lambda retries the event with fresh data
        raise RuntimeError(f"Order {order_id} changed while deciding invoice {invoice_uuid}; retrying") from exc
    return {"result": "matched", "order_id": order_id}


def handler(event, context):
    results = []
    for record in event["Records"]:
        new_image = record.get("dynamodb", {}).get("NewImage", {})
        invoice_uuid = new_image.get("uuid", {}).get("S", "")
        status = new_image.get("status", {}).get("S", "")

        # The EventBridge rule filters MODIFY + match_proposed; check again
        if record.get("eventName") != "MODIFY" or status != "match_proposed":
            summary = {"result": "skipped_not_pending"}
        else:
            summary = decide(invoice_uuid)

        summary = {"invoice": invoice_uuid, **summary}
        print(json.dumps(summary, default=str))
        results.append(summary)
    return results
