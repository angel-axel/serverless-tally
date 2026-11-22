"""Tests for the find_open_orders tool.

Runs against simulated DynamoDB (moto), so no AWS account is needed:

    pip install -r lambdas/find_open_orders/tests/requirements.txt
    python -m unittest discover -s lambdas/find_open_orders/tests -v
"""

import importlib
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

os.environ.update(
    AWS_DEFAULT_REGION="us-east-1",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    INVOICES_TABLE="test-invoices",
    PURCHASE_ORDERS_TABLE="test-purchase-orders",
)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

LAMBDA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UUID = "5A3C9E21-7B4D-4F8A-9C12-0E6D8B4F2A71"
RFC = "EKU9003173C9"
OTHER_RFC = "AAA010101AAA"


def order(order_id, rfc=RFC, status="open", created_at="2026-11-01", invoiced=None):
    item = {
        "supplier_rfc": rfc,
        "order_id": order_id,
        "status": status,
        "currency": "MXN",
        "created_at": created_at,
        "lines": [
            {"line": 1, "description": "Aceite 25W50 Quaker litro", "unit": "pieza",
             "quantity": Decimal("10"), "unit_price": Decimal("200.00")},
            {"line": 2, "description": "Anticongelante verde galon", "unit": "pieza",
             "quantity": Decimal("4"), "unit_price": Decimal("150.00")},
        ],
    }
    if invoiced is not None:
        item["invoiced_quantities"] = invoiced
    return item


@mock_aws
class FindOpenOrdersTest(unittest.TestCase):
    def setUp(self):
        dynamodb = boto3.resource("dynamodb")
        self.invoices = dynamodb.create_table(
            TableName="test-invoices",
            KeySchema=[{"AttributeName": "uuid", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "uuid", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.orders = dynamodb.create_table(
            TableName="test-purchase-orders",
            KeySchema=[{"AttributeName": "supplier_rfc", "KeyType": "HASH"},
                       {"AttributeName": "order_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "supplier_rfc", "AttributeType": "S"},
                                  {"AttributeName": "order_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.invoices.put_item(Item={"uuid": UUID, "status": "received", "issuer_rfc": RFC,
                                     "total": "1160.00", "xml_key": "invoices/pending/x.xml"})

        sys.path.insert(0, LAMBDA_DIR)
        sys.modules.pop("handler", None)
        self.handler = importlib.import_module("handler")

    def tearDown(self):
        sys.path.remove(LAMBDA_DIR)

    def run_tool(self, **arguments):
        return self.handler.handler({"invoice_uuid": UUID, **arguments}, None)

    def test_returns_open_orders_of_the_invoice_supplier_only(self):
        self.orders.put_item(Item=order("OC-1"))
        self.orders.put_item(Item=order("OC-2", status="closed"))
        self.orders.put_item(Item=order("OC-9", rfc=OTHER_RFC))

        result = self.run_tool()
        self.assertEqual([o["order_id"] for o in result["orders"]], ["OC-1"])
        self.assertEqual(result["supplier_rfc"], RFC)

    def test_supplier_cannot_be_chosen_by_the_agent(self):
        """An extra supplier_rfc argument is ignored: the RFC comes from the invoice."""
        self.orders.put_item(Item=order("OC-9", rfc=OTHER_RFC))
        result = self.run_tool(supplier_rfc=OTHER_RFC)
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["supplier_rfc"], RFC)

    def test_reads_only_allowed_invoice_attributes(self):
        """Matches the IAM condition: uuid, issuer_rfc and status only."""
        with mock.patch.object(self.handler.invoices, "get_item",
                               wraps=self.handler.invoices.get_item) as get_item:
            self.run_tool()
        kwargs = get_item.call_args.kwargs
        self.assertEqual(kwargs["ProjectionExpression"], "#uuid, issuer_rfc, #status")
        self.assertEqual(set(kwargs["ExpressionAttributeNames"].values()), {"uuid", "status"})

    def test_no_prices_are_returned(self):
        self.orders.put_item(Item=order("OC-1"))
        flat = str(self.run_tool())
        self.assertNotIn("unit_price", flat)
        self.assertNotIn("200", flat)

    def test_pending_quantities_and_fully_invoiced_lines(self):
        self.orders.put_item(Item=order("OC-1", invoiced={"1": Decimal("10"), "2": Decimal("1")}))
        lines = self.run_tool()["orders"][0]["lines"]
        # Line 1 is fully invoiced and hidden; line 2 has 3 pending
        self.assertEqual(lines, [{"order_line": 2, "description": "Anticongelante verde galon",
                                  "unit": "pieza", "quantity_pending": "3"}])

    def test_most_recent_first_and_capped(self):
        for n in range(25):
            self.orders.put_item(Item=order(f"OC-{n:02d}", created_at=f"2026-10-{n + 1:02d}"))
        orders = self.run_tool()["orders"]
        self.assertEqual(len(orders), 20)
        self.assertEqual(orders[0]["order_id"], "OC-24")

    def test_invoice_not_pending(self):
        self.invoices.update_item(Key={"uuid": UUID}, UpdateExpression="SET #s = :s",
                                  ExpressionAttributeNames={"#s": "status"},
                                  ExpressionAttributeValues={":s": "match_proposed"})
        self.assertIn("not waiting", self.run_tool()["error"])

    def test_invalid_and_unknown_uuid(self):
        self.assertIn("error", self.handler.handler({"invoice_uuid": "nope"}, None))
        unknown = self.handler.handler({"invoice_uuid": "C7E1D4B2-3A9F-4E6C-8B05-91F2A6D3E840"}, None)
        self.assertIn("does not exist", unknown["error"])


if __name__ == "__main__":
    unittest.main()
