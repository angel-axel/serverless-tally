"""find_open_orders tool (AgentCore Gateway Lambda target).

Returns the open purchase orders of the supplier of one invoice. Read-only.

- It takes the invoice UUID, not a supplier RFC: the function looks up the
  supplier itself, so the agent cannot ask for another supplier's orders.
- Its IAM role may only read the uuid, issuer_rfc and status attributes of
  the invoice record.
- It returns descriptions, quantities and units, never prices.
- Only invoices waiting for a proposal (status "received") are served.
"""

import json
import os
import re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

dynamodb = boto3.resource("dynamodb")
invoices = dynamodb.Table(os.environ["INVOICES_TABLE"])
purchase_orders = dynamodb.Table(os.environ["PURCHASE_ORDERS_TABLE"])

UUID_PATTERN = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
MAX_ORDERS = 20
MAX_TEXT = 300


def error(message: str) -> dict:
    return {"error": message}


def text(value) -> str:
    return " ".join(str(value or "").split())[:MAX_TEXT]


def remaining(order: dict, line: dict) -> Decimal:
    invoiced = (order.get("invoiced_quantities") or {}).get(str(line["line"]), Decimal("0"))
    return Decimal(line["quantity"]) - Decimal(invoiced)


def find_open_orders(invoice_uuid) -> dict:
    invoice_uuid = str(invoice_uuid or "").strip().upper()
    if not UUID_PATTERN.match(invoice_uuid):
        return error("invoice_uuid must be a CFDI UUID like 5A3C9E21-7B4D-4F8A-9C12-0E6D8B4F2A71.")

    # Read only the attributes the IAM policy allows
    item = invoices.get_item(
        Key={"uuid": invoice_uuid},
        ProjectionExpression="#uuid, issuer_rfc, #status",
        ExpressionAttributeNames={"#uuid": "uuid", "#status": "status"},
        ConsistentRead=True,
    ).get("Item")
    if item is None:
        return error(f"Invoice {invoice_uuid} does not exist.")
    if item.get("status") != "received":
        return error(f"Invoice {invoice_uuid} is not waiting for a match proposal.")

    supplier_rfc = item["issuer_rfc"]
    orders, query = [], {
        "KeyConditionExpression": Key("supplier_rfc").eq(supplier_rfc),
        "FilterExpression": Attr("status").eq("open"),
    }
    while True:
        page = purchase_orders.query(**query)
        orders.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    # Most recent first, capped so a supplier with many orders cannot flood the agent
    orders.sort(key=lambda order: order.get("created_at", ""), reverse=True)
    orders = orders[:MAX_ORDERS]

    return {
        "invoice_uuid": invoice_uuid,
        "supplier_rfc": supplier_rfc,
        "orders": [
            {
                "order_id": order["order_id"],
                "created_at": order.get("created_at", ""),
                "lines": [
                    {
                        "order_line": int(line["line"]),
                        "description": text(line.get("description")),
                        "unit": text(line.get("unit")),
                        "quantity_pending": str(remaining(order, line)),
                    }
                    for line in order.get("lines", [])
                    if remaining(order, line) > 0
                ],
            }
            for order in orders
        ],
        "note": "Only orders with status open are listed. An empty list means no open order fits.",
    }


def handler(event, context):
    arguments = event if isinstance(event, dict) else {}
    result = find_open_orders(arguments.get("invoice_uuid"))
    print(json.dumps({
        "tool": "find_open_orders",
        "invoice_uuid": arguments.get("invoice_uuid"),
        "result": "error" if "error" in result else "ok",
        "error": result.get("error"),
        "orders": [order["order_id"] for order in result.get("orders", [])],
    }))
    return result
