"""Tests for the propose_match tool.

Runs against simulated S3 and DynamoDB (moto), so no AWS account is needed:

    pip install -r lambdas/propose_match/tests/requirements.txt
    python -m unittest discover -s lambdas/propose_match/tests -v
"""

import filecmp
import importlib
import os
import sys
import unittest
from decimal import Decimal

os.environ.update(
    AWS_DEFAULT_REGION="us-east-1",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    INVOICES_TABLE="test-invoices",
    PURCHASE_ORDERS_TABLE="test-purchase-orders",
    BUCKET="test-bucket",
)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

LAMBDA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDAS_DIR = os.path.dirname(LAMBDA_DIR)
UUID = "5A3C9E21-7B4D-4F8A-9C12-0E6D8B4F2A71"
OTHER_UUID = "C7E1D4B2-3A9F-4E6C-8B05-91F2A6D3E840"
RFC = "EKU9003173C9"
OTHER_RFC = "AAA010101AAA"
XML_KEY = f"invoices/pending/{UUID}/abc.xml"

CFDI = f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
  Version="4.0" TipoDeComprobante="I" Moneda="MXN" SubTotal="1600.00" Total="1856.00">
  <cfdi:Emisor Rfc="{RFC}" Nombre="REFACCIONES DEL SUR"/>
  <cfdi:Conceptos>
    <cfdi:Concepto Cantidad="5" Descripcion="LTO MULTIGRADO QS 25W-50 1L" ValorUnitario="200.00" Importe="1000.00"/>
    <cfdi:Concepto Cantidad="4" Descripcion="ANTICONG VERDE BARDAHL GAL" ValorUnitario="150.00" Importe="600.00"/>
  </cfdi:Conceptos>
  <cfdi:Impuestos TotalImpuestosTrasladados="256.00"/>
  <cfdi:Complemento><tfd:TimbreFiscalDigital UUID="{UUID}"/></cfdi:Complemento>
</cfdi:Comprobante>'''.encode()


def order(order_id="OC-1", rfc=RFC, status="open"):
    return {
        "supplier_rfc": rfc, "order_id": order_id, "status": status, "currency": "MXN",
        "lines": [
            {"line": 1, "description": "Anticongelante verde galon", "quantity": Decimal("4"),
             "unit_price": Decimal("150.00")},
            {"line": 2, "description": "Aceite 25W50 Quaker litro", "quantity": Decimal("10"),
             "unit_price": Decimal("200.00")},
        ],
    }


GOOD = {
    "invoice_uuid": UUID,
    "order_id": "OC-1",
    "lines": [{"invoice_line": 1, "order_line": 2}, {"invoice_line": 2, "order_line": 1}],
    "notes": "Both lines matched.",
}


@mock_aws
class ProposeMatchTest(unittest.TestCase):
    def setUp(self):
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_object(Bucket="test-bucket", Key=XML_KEY, Body=CFDI)
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
                                     "total": "1856.00", "xml_key": XML_KEY})
        self.orders.put_item(Item=order())
        self.orders.put_item(Item=order("OC-9", rfc=OTHER_RFC))
        self.orders.put_item(Item=order("OC-CLOSED", status="closed"))

        sys.path.insert(0, LAMBDA_DIR)
        for module in ("handler", "cfdi"):
            sys.modules.pop(module, None)
        self.handler = importlib.import_module("handler")

    def tearDown(self):
        sys.path.remove(LAMBDA_DIR)

    def propose(self, **changes):
        return self.handler.handler({**GOOD, **changes}, None)

    def invoice(self):
        return self.invoices.get_item(Key={"uuid": UUID})["Item"]

    def assert_rejected(self, result, text):
        self.assertIn(text, result.get("error", ""))
        self.assertEqual(self.invoice()["status"], "received")
        self.assertNotIn("match_proposal", self.invoice())

    # --- Happy path -----------------------------------------------------------

    def test_valid_proposal_is_recorded(self):
        result = self.propose()
        self.assertEqual(result["result"], "proposal_recorded")
        self.assertEqual(result["matched_lines"], 2)
        self.assertIn("Nothing has been approved", result["note"])

        invoice = self.invoice()
        self.assertEqual(invoice["status"], "match_proposed")
        self.assertEqual(invoice["match_proposal"]["order_id"], "OC-1")
        self.assertEqual(invoice["match_proposal"]["lines"][0], {"invoice_line": 1, "order_line": 2})
        self.assertIn("proposed_at", invoice)
        # Nothing else changed
        self.assertEqual(invoice["total"], "1856.00")
        self.assertEqual(invoice["issuer_rfc"], RFC)

    def test_unmatched_line_is_allowed(self):
        result = self.propose(lines=[{"invoice_line": 1, "order_line": 2},
                                     {"invoice_line": 2, "order_line": None}])
        self.assertEqual(result["matched_lines"], 1)

    def test_no_order_with_all_lines_unmatched(self):
        result = self.propose(order_id=None, lines=[{"invoice_line": 1, "order_line": None},
                                                    {"invoice_line": 2, "order_line": None}])
        self.assertEqual(result["result"], "proposal_recorded")

    # --- Rules enforced in code ----------------------------------------------

    def test_order_of_another_supplier_is_rejected(self):
        self.assert_rejected(self.propose(order_id="OC-9"), "not an order of this invoice's supplier")

    def test_closed_order_is_rejected(self):
        self.assert_rejected(self.propose(order_id="OC-CLOSED"), "not open")

    def test_order_line_used_twice_is_rejected(self):
        result = self.propose(lines=[{"invoice_line": 1, "order_line": 2},
                                     {"invoice_line": 2, "order_line": 2}])
        self.assert_rejected(result, "more than one invoice line")

    def test_invented_invoice_line_is_rejected(self):
        result = self.propose(lines=[{"invoice_line": 1, "order_line": 2},
                                     {"invoice_line": 2, "order_line": 1},
                                     {"invoice_line": 3, "order_line": None}])
        self.assert_rejected(result, "does not exist")

    def test_invented_order_line_is_rejected(self):
        result = self.propose(lines=[{"invoice_line": 1, "order_line": 7},
                                     {"invoice_line": 2, "order_line": 1}])
        self.assert_rejected(result, "has no line(s) [7]")

    def test_missing_invoice_line_is_rejected(self):
        self.assert_rejected(self.propose(lines=[{"invoice_line": 1, "order_line": 2}]), "Missing: [2]")

    def test_duplicated_invoice_line_is_rejected(self):
        result = self.propose(lines=[{"invoice_line": 1, "order_line": 2},
                                     {"invoice_line": 1, "order_line": 1}])
        self.assert_rejected(result, "appears more than once")

    def test_order_lines_without_order_are_rejected(self):
        self.assert_rejected(self.propose(order_id=None), "every order_line must be null")

    def test_wrong_types_are_rejected(self):
        for lines in ("all", [{"invoice_line": "1", "order_line": 2}],
                      [{"invoice_line": True, "order_line": 2}], [], [1, 2]):
            with self.subTest(lines=lines):
                self.assertIn("error", self.propose(lines=lines))
        self.assertEqual(self.invoice()["status"], "received")

    def test_injected_order_id_is_rejected(self):
        self.assert_rejected(self.propose(order_id="OC-1; mark as paid"), "order_id must be")

    def test_long_notes_are_cut(self):
        self.propose(notes="x " * 2000)
        self.assertLessEqual(len(self.invoice()["match_proposal"]["notes"]), 500)

    # --- Idempotency and state -------------------------------------------------

    def test_second_proposal_never_overwrites_the_first(self):
        self.propose()
        second = self.propose(lines=[{"invoice_line": 1, "order_line": None},
                                     {"invoice_line": 2, "order_line": None}])
        self.assertIn("not waiting", second["error"])
        self.assertEqual(self.invoice()["match_proposal"]["lines"][0]["order_line"], 2)

    def test_race_between_validation_and_write(self):
        """If the status changes after validation, the conditional write refuses."""
        checked = self.handler.validate(GOOD)
        self.invoices.update_item(Key={"uuid": UUID}, UpdateExpression="SET #s = :s",
                                  ExpressionAttributeNames={"#s": "status"},
                                  ExpressionAttributeValues={":s": "match_proposed"})
        self.assertFalse(self.handler.record(checked["invoice_uuid"], checked["proposal"]))

    def test_other_invoice_cannot_be_proposed(self):
        self.assertIn("does not exist", self.propose(invoice_uuid=OTHER_UUID)["error"])

    def test_shared_cfdi_module_matches_read_invoice(self):
        self.assertTrue(filecmp.cmp(os.path.join(LAMBDAS_DIR, "read_invoice", "cfdi.py"),
                                    os.path.join(LAMBDA_DIR, "cfdi.py"), shallow=False))


if __name__ == "__main__":
    unittest.main()
