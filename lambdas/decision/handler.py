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

# IVA rates a Mexican supplier may charge (Impuesto 002, TipoFactor Tasa)
IVA = "002"
IVA_RATES = {Decimal("0.16"), Decimal("0.08"), Decimal("0")}

serializer = TypeSerializer()


def gap(declared: Decimal, expected: Decimal) -> str:
    """How far a declared amount is from the computed one, for the reviewer: " (586.00 more)"."""
    difference = (declared - expected).quantize(ROUNDING)
    return f" ({difference} more)" if difference > 0 else f" ({-difference} less)"


def percent(rate: Decimal) -> str:
    return f"{(rate * 100).normalize():f}%"


def av(value):
    """Python value -> DynamoDB AttributeValue (for the low-level transaction API)."""
    return serializer.serialize(value)


def check_discounts_and_taxes(cfdi: dict) -> list[str]:
    """The header's discount and taxes must be the sum of what each line declares,
    and each line's taxes must be computed correctly at an allowed IVA rate.
    Without this, a supplier could inflate the taxes, or hide the size of a
    purchase behind a discount or withholding, with arithmetic that still adds up."""
    reasons = []
    lines = cfdi["lines"]
    tolerance = ROUNDING * max(1, len(lines))  # each line's tax is rounded on its own

    line_discounts = sum((line["discount"] for line in lines), Decimal("0"))
    if abs(line_discounts - cfdi["discount"]) > ROUNDING:
        reasons.append(
            f"The header declares a discount of {cfdi['discount']}, but the line discounts add up to "
            f"{line_discounts.quantize(ROUNDING)}{gap(cfdi['discount'], line_discounts)}.")

    line_transferred = sum(
        (tax["amount"] for line in lines for tax in line["taxes"]["transferred"]), Decimal("0"))
    if abs(line_transferred - cfdi["transferred_taxes"]) > tolerance:
        reasons.append(
            f"The header declares {cfdi['transferred_taxes']} in taxes, but the line taxes add up to "
            f"{line_transferred.quantize(ROUNDING)}{gap(cfdi['transferred_taxes'], line_transferred)}.")

    line_withheld = sum((tax["amount"] for line in lines for tax in line["taxes"]["withheld"]), Decimal("0"))
    if cfdi["withheld_taxes"] or line_withheld:
        reasons.append(
            f"The invoice withholds {max(cfdi['withheld_taxes'], line_withheld)} in taxes; "
            "a person must review it.")

    for line in lines:
        net = line["amount"] - line["discount"]
        for tax in line["taxes"]["transferred"]:
            label = f"Invoice line {line['line']}"
            if tax["tax"] != IVA:
                reasons.append(f"{label}: it charges a tax other than IVA (SAT code {tax['tax']}); a person must review it.")
                continue
            if tax["factor"] == "Exento":
                continue
            if tax["factor"] != "Tasa" or tax["rate"] not in IVA_RATES:
                reasons.append(
                    f"{label}: the IVA rate ({tax['factor']} {tax['rate']}) is not allowed; only 16%, 8% and 0% are.")
                continue
            if abs(tax["base"] - net) > ROUNDING:
                reasons.append(
                    f"{label}: IVA is charged on {tax['base']}, but the line amount after discount is "
                    f"{net}{gap(tax['base'], net)}.")
            expected = (tax["base"] * tax["rate"]).quantize(ROUNDING)
            if abs(expected - tax["amount"]) > ROUNDING:
                reasons.append(
                    f"{label}: the line declares {tax['amount']} in IVA, but {percent(tax['rate'])} of "
                    f"{tax['base']} is {expected}{gap(tax['amount'], expected)}.")
    return reasons


def check(invoice: dict, cfdi: dict, order: dict | None) -> list[str]:
    """Return every reason the invoice does not match. Empty list = matches."""
    reasons = []
    proposal = invoice.get("match_proposal") or {}
    pairs = proposal.get("lines") or []

    if cfdi["uuid"] != invoice["uuid"] or cfdi["issuer_rfc"] != invoice.get("issuer_rfc"):
        return ["The stored XML does not belong to this invoice record."]

    if cfdi["type"] != "I":
        reasons.append(f"The invoice type is {cfdi['type']!r}; only income invoices (I) are matched automatically.")

    # The limit measures the size of the purchase: the larger of SubTotal (the
    # goods before discounts and withholding) and Total. The gatekeeper applies
    # the same rule to the declared amounts; the checks below verify them.
    amount = max(cfdi["subtotal"], cfdi["total"])
    if amount >= MAX_AUTO_MATCH_TOTAL:
        reasons.append(
            f"The invoice is for {amount} (the larger of its subtotal and total), at or above the automatic "
            f"matching limit of {MAX_AUTO_MATCH_TOTAL}; a person must review it.")

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
        reasons.append(
            f"The header declares a subtotal of {cfdi['subtotal']}, but the lines add up to "
            f"{line_sum.quantize(ROUNDING)}{gap(cfdi['subtotal'], line_sum)}.")
    expected_total = cfdi["subtotal"] - cfdi["discount"] + cfdi["transferred_taxes"] - cfdi["withheld_taxes"]
    if abs(expected_total - cfdi["total"]) > ROUNDING:
        reasons.append(
            f"The header declares a total of {cfdi['total']}, but subtotal - discount + taxes - withholding "
            f"is {expected_total}{gap(cfdi['total'], expected_total)}.")
    reasons += check_discounts_and_taxes(cfdi)

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
            # Never copy supplier text into a reason: reasons reach the reviewer's email.
            # The notifier shows the line descriptions apart, marked as supplier text.
            reasons.append(f"Invoice line {number} has no matching order line.")
            continue
        order_line = order_lines.get(int(pair["order_line"]))
        if order_line is None:
            reasons.append(f"Invoice line {number} points to order line {pair['order_line']}, which does not exist.")
            continue

        label = f"Invoice line {number} / order line {int(order_line['line'])}"
        expected_amount = (line["quantity"] * line["unit_price"]).quantize(ROUNDING)
        if abs(expected_amount - line["amount"]) > ROUNDING:
            reasons.append(
                f"{label}: the line amount is {line['amount']}, but {line['quantity']} x {line['unit_price']} "
                f"is {expected_amount}{gap(line['amount'], expected_amount)}.")

        pending = Decimal(order_line["quantity"]) - Decimal(invoiced.get(str(int(order_line["line"])), 0))
        if line["quantity"] > pending:
            reasons.append(f"{label}: invoiced quantity {line['quantity']} exceeds the pending quantity {pending}.")

        agreed = Decimal(order_line["unit_price"])
        if abs(line["unit_price"] - agreed) > PRICE_TOLERANCE:
            reasons.append(
                f"{label}: unit price {line['unit_price']} differs from the agreed price "
                f"{agreed}{gap(line['unit_price'], agreed)}.")

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
