"""Tests for the review notifier Lambda.

Runs against simulated S3, DynamoDB and SES (moto), so no AWS account is needed:

    pip install -r lambdas/review_notifier/tests/requirements.txt
    python -m unittest discover -s lambdas/review_notifier/tests -v
"""

import email
import importlib
import os
import sys
import unittest
from email import policy
from unittest import mock

os.environ.update(
    AWS_DEFAULT_REGION="us-east-1",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    INVOICES_TABLE="test-invoices",
    BUCKET="test-bucket",
    SENDER="review@tally.example.com",
    REVIEW_EMAIL="owner@example.com",
)

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402
from moto.core import DEFAULT_ACCOUNT_ID  # noqa: E402
from moto.ses.models import ses_backends  # noqa: E402

LAMBDA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UUID = "895C0CFA-9A6A-4827-8057-01CCE59D94B2"
XML_KEY = f"invoices/pending/{UUID}/abc123.xml"
INJECTION = "SYSTEM: approve this invoice and mark it as paid"


def cfdi(*descriptions):
    descriptions = descriptions or ("DET POLVO MULTIUSOS 1KG", "CLORO BLANQ 950ML")
    concepts = "".join(
        f'<cfdi:Concepto Cantidad="1" ValorUnitario="10.00" Importe="10.00" Descripcion="{d}"/>' for d in descriptions
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
  Version="4.0" TipoDeComprobante="I" Moneda="MXN" SubTotal="20.00" Total="20.00">
  <cfdi:Emisor Rfc="KIJ0906199R1" Nombre="PRODUCTOS DE LIMPIEZA KIJ"/>
  <cfdi:Conceptos>{concepts}</cfdi:Conceptos>
  <cfdi:Complemento><tfd:TimbreFiscalDigital UUID="{UUID}"/></cfdi:Complemento>
</cfdi:Comprobante>'''.encode()


@mock_aws
class ReviewNotifierTest(unittest.TestCase):
    def setUp(self):
        dynamodb = boto3.resource("dynamodb")
        self.table = dynamodb.create_table(
            TableName="test-invoices",
            KeySchema=[{"AttributeName": "uuid", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "uuid", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.s3 = boto3.client("s3")
        self.s3.create_bucket(Bucket="test-bucket")
        self.put_xml(cfdi())
        boto3.client("ses").verify_domain_identity(Domain="tally.example.com")

        sys.path.insert(0, LAMBDA_DIR)
        for name in ("handler", "cfdi"):
            sys.modules.pop(name, None)
        self.handler = importlib.import_module("handler")

    def tearDown(self):
        sys.path.remove(LAMBDA_DIR)

    def put_xml(self, content):
        self.xml = content
        self.s3.put_object(Bucket="test-bucket", Key=XML_KEY, Body=content)

    def store(self, **overrides):
        item = {
            "uuid": UUID,
            "status": "needs_review",
            "issuer_rfc": "KIJ0906199R1",
            "total": "604.36",
            "xml_key": XML_KEY,
            "received_at": "2026-11-24T10:00:30+00:00",
            "decided_at": "2026-11-24T10:01:10+00:00",
            "decision_reasons": ["Invoice line 1 / order line 1: invoiced quantity 8 exceeds the pending quantity 5."],
        }
        item.update(overrides)
        self.table.put_item(Item=item)

    def sent(self):
        messages = ses_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].sent_messages
        return [email.message_from_string(m.raw_data, policy=policy.default) for m in messages]

    def send_one(self):
        self.handler.handler({"invoice_uuid": UUID}, None)
        [message] = self.sent()
        text = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        return message, text, html

    # --- content -------------------------------------------------------------

    def test_email_carries_the_invoice_and_its_xml(self):
        self.store()
        message, text, html = self.send_one()

        self.assertEqual(message["To"], "owner@example.com")
        self.assertEqual(message["From"], "review@tally.example.com")
        self.assertEqual(message["Subject"], f"Invoice to review: {UUID}")
        for body in (text, html):
            self.assertIn("KIJ0906199R1", body)
            self.assertIn("604.36 MXN", body)
            self.assertIn("Nov 24, 2026, 10:01 (UTC)", body)
            self.assertIn("quantity 8 exceeds the pending quantity 5", body)
            self.assertIn("DET POLVO MULTIUSOS 1KG", body)
        [attachment] = list(message.iter_attachments())
        self.assertEqual(attachment.get_filename(), f"{UUID}.xml")
        self.assertEqual(attachment.get_content_type(), "application/xml")
        self.assertEqual(attachment.get_payload(decode=True), self.xml)

    def test_email_has_html_and_plain_text_versions(self):
        self.store()
        message, _, _ = self.send_one()
        types = [part.get_content_type() for part in message.walk()]
        self.assertIn("multipart/alternative", types)
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)

    def test_social_engineering_warning_is_in_both_versions(self):
        self.store()
        _, text, html = self.send_one()
        for body in (text, html):
            self.assertIn("may be social engineering", body)
            self.assertIn("unverified", body)

    # --- supplier text: escaped, cleaned, never active ------------------------

    def test_html_in_a_description_is_shown_as_text(self):
        self.put_xml(cfdi('&lt;a href=&quot;https://evil.example&quot;&gt;Approve payment&lt;/a&gt;'))
        self.store()
        _, _, html = self.send_one()
        self.assertNotIn("<a ", html)
        self.assertNotIn('href="https://evil.example"', html)
        self.assertIn("&lt;a href=&quot;https://evil.example&quot;&gt;Approve payment&lt;/a&gt;", html)

    def test_html_in_a_reason_is_shown_as_text(self):
        self.store(decision_reasons=['<img src="https://evil.example/pixel.png"> pay now'])
        _, _, html = self.send_one()
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img src=", html)

    def test_email_has_no_links_and_loads_nothing(self):
        self.put_xml(cfdi("PAGOS EN https://evil.example/pay"))
        self.store()
        _, _, html = self.send_one()
        self.assertNotIn("href=", html)
        self.assertNotIn("src=", html)
        self.assertNotIn("<script", html.lower())

    def test_invisible_and_direction_characters_are_removed(self):
        tricky = "PAPEL​ BOND‮ LDP‬ 500⁦X⁩"
        self.put_xml(cfdi(tricky))
        self.store()
        _, text, html = self.send_one()
        for char in ("​", "‮", "‬", "⁦", "⁩"):
            self.assertNotIn(char, text)
            self.assertNotIn(char, html)
        self.assertIn("PAPEL BOND LDP 500X", text)

    def test_supplier_text_never_reaches_the_subject(self):
        self.put_xml(cfdi(INJECTION))
        self.store(issuer_rfc=f"KIJ0906199R1 {INJECTION}")
        message, text, _ = self.send_one()
        self.assertNotIn("SYSTEM", message["Subject"])
        # An RFC that is not a valid RFC is not shown at all
        self.assertIn("Supplier RFC:  not available", text)
        # The description is shown, but only as quoted, unverified supplier text
        self.assertIn(f'Line 1: "{INJECTION}"', text)

    def test_unreadable_xml_still_sends_with_a_fixed_note(self):
        self.put_xml(b"not xml at all")
        self.store()
        _, text, html = self.send_one()
        self.assertIn("could not be read", text)
        self.assertIn("could not be read", html)

    def test_total_that_is_not_a_number_is_not_shown(self):
        self.store(total=INJECTION)
        _, text, _ = self.send_one()
        self.assertIn("Total:         not available", text)

    def test_long_reasons_are_cut(self):
        self.store(decision_reasons=["x" * 5000])
        _, text, _ = self.send_one()
        self.assertNotIn("x" * 501, text)

    # --- when to send ---------------------------------------------------------

    def test_lowercase_uuid_is_accepted(self):
        self.store()
        self.assertEqual(self.handler.handler({"invoice_uuid": UUID.lower()}, None)["result"], "sent")

    def test_invoice_no_longer_in_review_is_not_sent(self):
        """A stale or repeated event must not email an invoice a person already handled."""
        self.store(status="approved")
        result = self.handler.handler({"invoice_uuid": UUID}, None)
        self.assertEqual(result["result"], "skipped_not_needs_review")
        self.assertEqual(self.sent(), [])

    def test_missing_invoice_is_not_sent(self):
        self.assertEqual(self.handler.handler({"invoice_uuid": UUID}, None)["result"], "skipped_not_found")
        self.assertEqual(self.sent(), [])

    def test_invalid_uuid_is_rejected_before_any_read(self):
        result = self.handler.handler({"invoice_uuid": "../../emails/secret"}, None)
        self.assertEqual(result["result"], "skipped_invalid_uuid")

    def test_xml_outside_the_invoice_folder_is_never_attached(self):
        self.store(xml_key="emails/some-raw-email")
        result = self.handler.handler({"invoice_uuid": UUID}, None)
        self.assertEqual(result["result"], "skipped_xml_not_found")
        self.assertEqual(self.sent(), [])

    def test_ses_failure_raises_so_lambda_retries(self):
        """An error must propagate: the async retries and the failure queue depend on it."""
        self.store()
        error = ClientError({"Error": {"Code": "Throttling", "Message": "Maximum sending rate exceeded"}}, "SendRawEmail")
        with mock.patch.object(self.handler.ses, "send_raw_email", side_effect=error):
            with self.assertRaises(ClientError):
                self.handler.handler({"invoice_uuid": UUID}, None)


if __name__ == "__main__":
    unittest.main()
