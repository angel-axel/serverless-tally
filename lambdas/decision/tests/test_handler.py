"""Tests for the decision Lambda.

Runs against simulated S3 and DynamoDB (moto), so no AWS account is needed:

    pip install -r lambdas/decision/tests/requirements.txt
    python -m unittest discover -s lambdas/decision/tests -v
"""

import filecmp
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
    BUCKET="test-bucket",
    PRICE_TOLERANCE="0.01",
    MAX_AUTO_MATCH_TOTAL="10000",
)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

LAMBDA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDAS_DIR = os.path.dirname(LAMBDA_DIR)
UUID = "5A3C9E21-7B4D-4F8A-9C12-0E6D8B4F2A71"
SECOND_UUID = "C7E1D4B2-3A9F-4E6C-8B05-91F2A6D3E840"
RFC = "EKU9003173C9"


def cfdi(uuid=UUID, oil=("5", "200.00", "1000.00"), antifreeze=("4", "150.00", "600.00"),
         total=None, kind="I", currency="MXN", subtotal=None, root_taxes=None, line_tax=None,
         rate="0.160000", tax="002", discount=None, withheld=None):
    """A CFDI with IVA declared on each line, as a real one has.

    line_tax overrides the IVA of the first line; discount (on the first line)
    and withheld (an ISR retention on the first line) are optional.
    """
    lines = [(oil, "LTO MULTIGRADO QS 25W-50 1L"), (antifreeze, "ANTICONG VERDE BARDAHL GAL")]
    concepts, line_sum, tax_sum, disc_sum = [], Decimal("0"), Decimal("0"), Decimal("0")
    for index, ((quantity, price, amount), description) in enumerate(lines):
        d = Decimal(discount) if (discount and index == 0) else Decimal("0")
        base = Decimal(amount) - d
        iva = (base * Decimal(rate)).quantize(Decimal("0.01"))
        if index == 0 and line_tax is not None:
            iva = Decimal(line_tax)
        retention = (f'<cfdi:Retenciones><cfdi:Retencion Base="{base}" Impuesto="001" TipoFactor="Tasa" '
                     f'TasaOCuota="0.100000" Importe="{withheld}"/></cfdi:Retenciones>') if (withheld and index == 0) else ""
        disc_attr = f' Descuento="{d}"' if d else ""
        concepts.append(
            f'<cfdi:Concepto Cantidad="{quantity}" Descripcion="{description}" ValorUnitario="{price}" '
            f'Importe="{amount}"{disc_attr}><cfdi:Impuestos><cfdi:Traslados><cfdi:Traslado Base="{base}" '
            f'Impuesto="{tax}" TipoFactor="Tasa" TasaOCuota="{rate}" Importe="{iva}"/></cfdi:Traslados>'
            f'{retention}</cfdi:Impuestos></cfdi:Concepto>')
        line_sum += Decimal(amount); tax_sum += iva; disc_sum += d
    subtotal = subtotal or str(line_sum)
    taxes = root_taxes or str(tax_sum)
    held = Decimal(withheld) if withheld else Decimal("0")
    total = total or str(Decimal(subtotal) - disc_sum + Decimal(taxes) - held)
    disc_root = f' Descuento="{disc_sum}"' if disc_sum else ""
    held_root = f' TotalImpuestosRetenidos="{held}"' if held else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
  Version="4.0" TipoDeComprobante="{kind}" Moneda="{currency}" SubTotal="{subtotal}"{disc_root} Total="{total}">
  <cfdi:Emisor Rfc="{RFC}" Nombre="REFACCIONES DEL SUR"/>
  <cfdi:Conceptos>
    {"".join(concepts)}
  </cfdi:Conceptos>
  <cfdi:Impuestos{held_root} TotalImpuestosTrasladados="{taxes}"/>
  <cfdi:Complemento><tfd:TimbreFiscalDigital UUID="{uuid}"/></cfdi:Complemento>
</cfdi:Comprobante>'''.encode()


PROPOSAL = {
    "order_id": "OC-1",
    "lines": [{"invoice_line": 1, "order_line": 2}, {"invoice_line": 2, "order_line": 1}],
    "notes": "",
}


def stream_record(uuid=UUID, event_name="MODIFY", status="match_proposed"):
    return {"eventName": event_name,
            "dynamodb": {"NewImage": {"uuid": {"S": uuid}, "status": {"S": status}}}}


@mock_aws
class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.s3 = boto3.client("s3")
        self.s3.create_bucket(Bucket="test-bucket")
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
        self.put_order()

        sys.path.insert(0, LAMBDA_DIR)
        for module in ("handler", "cfdi"):
            sys.modules.pop(module, None)
        self.handler = importlib.import_module("handler")

    def tearDown(self):
        sys.path.remove(LAMBDA_DIR)

    # --- Helpers ---------------------------------------------------------------

    def put_order(self, invoiced=None, currency="MXN", status="open"):
        item = {
            "supplier_rfc": RFC, "order_id": "OC-1", "status": status, "currency": currency,
            "lines": [
                {"line": 1, "description": "Anticongelante verde galon",
                 "quantity": Decimal("4"), "unit_price": Decimal("150.00")},
                {"line": 2, "description": "Aceite 25W50 Quaker litro",
                 "quantity": Decimal("10"), "unit_price": Decimal("200.00")},
            ],
        }
        if invoiced is not None:
            item["invoiced_quantities"] = invoiced
        self.orders.put_item(Item=item)

    def propose(self, content, uuid=UUID, proposal=PROPOSAL):
        key = f"invoices/pending/{uuid}/abc.xml"
        self.s3.put_object(Bucket="test-bucket", Key=key, Body=content)
        self.invoices.put_item(Item={"uuid": uuid, "status": "match_proposed", "issuer_rfc": RFC,
                                     "xml_key": key, "match_proposal": proposal})

    def decide(self, uuid=UUID):
        return self.handler.handler({"Records": [stream_record(uuid)]}, None)[0]

    def invoice(self, uuid=UUID):
        return self.invoices.get_item(Key={"uuid": uuid})["Item"]

    def order(self):
        return self.orders.get_item(Key={"supplier_rfc": RFC, "order_id": "OC-1"})["Item"]

    def assert_review(self, result, text):
        self.assertEqual(result["result"], "needs_review")
        self.assertTrue(any(text in reason for reason in result["reasons"]), result["reasons"])
        self.assertEqual(self.invoice()["status"], "needs_review")
        self.assertNotIn("invoiced_quantities", self.order())

    # --- Matches ---------------------------------------------------------------

    def test_matching_invoice_is_matched_and_quantities_recorded(self):
        self.propose(cfdi())
        result = self.decide()

        self.assertEqual(result["result"], "matched")
        invoice = self.invoice()
        self.assertEqual(invoice["status"], "matched")
        self.assertEqual(invoice["decision_reasons"], [])
        order = self.order()
        self.assertEqual(order["invoiced_quantities"], {"1": Decimal("4"), "2": Decimal("5")})
        self.assertEqual(order["status"], "open")  # 5 of 10 liters still pending

    def test_order_closes_when_fully_invoiced(self):
        self.put_order(invoiced={"1": Decimal("0"), "2": Decimal("5")})
        self.propose(cfdi())
        self.assertEqual(self.decide()["result"], "matched")
        self.assertEqual(self.order()["status"], "closed")

    def test_rounding_within_tolerance_matches(self):
        self.propose(cfdi(oil=("5", "200.004", "1000.02")))
        self.assertEqual(self.decide()["result"], "matched")

    # --- Needs review ----------------------------------------------------------

    def test_price_above_the_agreed_price(self):
        self.propose(cfdi(oil=("5", "210.00", "1050.00")))
        self.assert_review(self.decide(), "differs from the agreed price 200.00")

    def test_quantity_above_what_is_pending(self):
        self.propose(cfdi(oil=("12", "200.00", "2400.00")))
        self.assert_review(self.decide(), "exceeds the pending quantity 10")

    def test_line_amount_does_not_add_up(self):
        self.propose(cfdi(oil=("5", "200.00", "1100.00")))
        self.assert_review(self.decide(), "the line amount is")

    def test_total_does_not_add_up(self):
        self.propose(cfdi(total="9999.00"))
        self.assert_review(self.decide(), "declares a total of 9999.00")

    def test_unmatched_line(self):
        self.propose(cfdi(), proposal={**PROPOSAL, "lines": [{"invoice_line": 1, "order_line": 2},
                                                              {"invoice_line": 2, "order_line": None}]})
        result = self.decide()
        self.assert_review(result, "Invoice line 2 has no matching order line.")
        # Supplier text never goes into a reason: reasons reach the reviewer's email
        self.assertNotIn("ANTICONG", " ".join(result["reasons"]))

    def test_no_order(self):
        self.propose(cfdi(), proposal={"order_id": None, "lines": [
            {"invoice_line": 1, "order_line": None}, {"invoice_line": 2, "order_line": None}]})
        self.assert_review(self.decide(), "No open purchase order")

    def test_credit_note(self):
        self.propose(cfdi(kind="E"))
        self.assert_review(self.decide(), "only income invoices")

    def test_currency_differs(self):
        self.propose(cfdi(currency="USD"))
        self.assert_review(self.decide(), "Currency 'USD'")

    def test_closed_order(self):
        self.put_order(status="closed")
        self.propose(cfdi())
        self.assert_review(self.decide(), "no longer open")

    def test_large_invoice_always_goes_to_review(self):
        with mock.patch.object(self.handler, "MAX_AUTO_MATCH_TOTAL", Decimal("1000")):
            self.propose(cfdi())  # total 1856.00, everything else matches
            self.assert_review(self.decide(), "at or above the automatic matching limit of 1000")

    def test_invoice_exactly_at_the_limit_goes_to_review(self):
        """The limit is inclusive: a total equal to it needs a person, as in the gatekeeper."""
        with mock.patch.object(self.handler, "MAX_AUTO_MATCH_TOTAL", Decimal("1856.00")):
            self.propose(cfdi())
            self.assert_review(self.decide(), "at or above the automatic matching limit of 1856.00")

    def test_invoice_one_cent_below_the_limit_matches(self):
        with mock.patch.object(self.handler, "MAX_AUTO_MATCH_TOTAL", Decimal("1856.01")):
            self.propose(cfdi())
            self.assertEqual(self.decide()["result"], "matched")

    # --- discounts, withholding and taxes: the size of the purchase ---------

    def test_large_purchase_hidden_by_a_discount_goes_to_review(self):
        """Live test case 5: goods worth 19,968 with a 12,000 discount, Total 9,242.88."""
        with mock.patch.object(self.handler, "MAX_AUTO_MATCH_TOTAL", Decimal("1500")):
            self.propose(cfdi(discount="1000.00"))  # SubTotal 1600, Total 696
            self.assert_review(self.decide(), "the larger of its subtotal and total")

    def test_withheld_taxes_go_to_review(self):
        """Live test case 4: an absurd withholding made the Total small with consistent arithmetic."""
        self.propose(cfdi(withheld="100.00"))
        self.assert_review(self.decide(), "in taxes; a person must review it")

    def test_inflated_header_taxes_go_to_review(self):
        """Taxes in the header that the lines do not declare would make the business overpay."""
        self.propose(cfdi(root_taxes="5000.00"))
        self.assert_review(self.decide(), "The header declares 5000.00 in taxes, but the line taxes add up to")

    def test_line_iva_not_equal_to_base_times_rate_goes_to_review(self):
        self.propose(cfdi(line_tax="400.00", root_taxes="496.00"))
        self.assert_review(self.decide(), "the line declares 400.00 in IVA, but 16% of 1000.00 is 160.00 (240.00 more)")

    def test_iva_rate_that_does_not_exist_goes_to_review(self):
        self.propose(cfdi(rate="0.500000"))
        self.assert_review(self.decide(), "is not allowed; only 16%, 8% and 0% are")

    def test_tax_other_than_iva_goes_to_review(self):
        self.propose(cfdi(tax="003"))
        self.assert_review(self.decide(), "a tax other than IVA (SAT code 003)")

    def test_reduced_border_iva_rate_matches(self):
        """8% IVA is legal in the northern and southern border regions."""
        self.propose(cfdi(rate="0.080000"))
        self.assertEqual(self.decide()["result"], "matched")

    def test_header_discount_that_the_lines_do_not_declare_goes_to_review(self):
        content = cfdi().replace(b'SubTotal="1600.00"', b'SubTotal="1600.00" Descuento="500.00"').replace(
            b'Total="1856.00"', b'Total="1356.00"')
        self.propose(content)
        self.assert_review(self.decide(), "The header declares a discount of 500.00, but the line discounts add up to 0.00 (500.00 more)")

    def test_all_reasons_are_reported_together(self):
        self.propose(cfdi(oil=("12", "210.00", "2520.00")))
        reasons = self.decide()["reasons"]
        self.assertTrue(any("pending quantity" in r for r in reasons))
        self.assertTrue(any("agreed price" in r for r in reasons))

    def test_wrong_pairing_by_the_agent_is_caught(self):
        """If the agent pairs oil with antifreeze, prices and quantities do not agree."""
        self.propose(cfdi(), proposal={**PROPOSAL, "lines": [{"invoice_line": 1, "order_line": 1},
                                                              {"invoice_line": 2, "order_line": 2}]})
        result = self.decide()
        self.assertEqual(result["result"], "needs_review")

    # --- Double invoicing and concurrency ---------------------------------------

    def test_same_order_cannot_be_invoiced_twice(self):
        self.propose(cfdi())
        self.assertEqual(self.decide()["result"], "matched")

        # A second invoice for the same 4 gallons of antifreeze
        self.propose(cfdi(uuid=SECOND_UUID), uuid=SECOND_UUID)
        result = self.decide(SECOND_UUID)
        self.assertEqual(result["result"], "needs_review")
        self.assertTrue(any("exceeds the pending quantity 0" in r for r in result["reasons"]))

    def test_order_changed_while_deciding_is_retried(self):
        """Another invoice updates the order between our read and our write."""
        self.propose(cfdi())
        original_check = self.handler.check

        def check_then_change_order(*args):
            reasons = original_check(*args)
            self.orders.update_item(
                Key={"supplier_rfc": RFC, "order_id": "OC-1"},
                UpdateExpression="SET invoiced_quantities = :q",
                ExpressionAttributeValues={":q": {"1": Decimal("4"), "2": Decimal("0")}})
            return reasons

        with mock.patch.object(self.handler, "check", side_effect=check_then_change_order):
            with self.assertRaises(RuntimeError):
                self.decide()
        # Nothing was written: the invoice waits for the retry
        self.assertEqual(self.invoice()["status"], "match_proposed")
        self.assertEqual(self.order()["invoiced_quantities"], {"1": Decimal("4"), "2": Decimal("0")})

        # The retry sees the new quantities and sends it to review
        self.assertEqual(self.decide()["result"], "needs_review")

    def test_order_changed_while_deciding_with_existing_quantities(self):
        """Same race, when the order already had invoiced quantities (per-line lock)."""
        self.put_order(invoiced={"1": Decimal("0"), "2": Decimal("0")})
        self.propose(cfdi())
        original_check = self.handler.check

        def check_then_change_order(*args):
            reasons = original_check(*args)
            self.orders.update_item(
                Key={"supplier_rfc": RFC, "order_id": "OC-1"},
                UpdateExpression="SET invoiced_quantities.#l = :q",
                ExpressionAttributeNames={"#l": "1"},
                ExpressionAttributeValues={":q": Decimal("4")})
            return reasons

        with mock.patch.object(self.handler, "check", side_effect=check_then_change_order):
            with self.assertRaises(RuntimeError):
                self.decide()
        self.assertEqual(self.invoice()["status"], "match_proposed")
        self.assertEqual(self.order()["invoiced_quantities"], {"1": Decimal("4"), "2": Decimal("0")})

    def test_retry_after_decision_changes_nothing(self):
        self.propose(cfdi())
        self.decide()
        self.assertEqual(self.decide()["result"], "skipped_not_pending")
        self.assertEqual(self.order()["invoiced_quantities"], {"1": Decimal("4"), "2": Decimal("5")})

    # --- Inputs ------------------------------------------------------------------

    def test_other_stream_events_are_ignored(self):
        records = [stream_record(event_name="INSERT", status="received"),
                   stream_record(event_name="MODIFY", status="matched"),
                   stream_record(event_name="REMOVE")]
        results = self.handler.handler({"Records": records}, None)
        self.assertTrue(all(r["result"] == "skipped_not_pending" for r in results))

    def test_xml_of_another_invoice_is_not_trusted(self):
        self.propose(cfdi(uuid=SECOND_UUID))
        self.assert_review(self.decide(), "does not belong")

    def test_unreadable_xml(self):
        self.propose(b"not xml")
        self.assert_review(self.decide(), "could not be read")

    def test_shared_cfdi_module_matches_read_invoice(self):
        self.assertTrue(filecmp.cmp(os.path.join(LAMBDAS_DIR, "read_invoice", "cfdi.py"),
                                    os.path.join(LAMBDA_DIR, "cfdi.py"), shallow=False))


if __name__ == "__main__":
    unittest.main()
