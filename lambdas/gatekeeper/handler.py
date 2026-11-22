"""Gatekeeper Lambda.

Runs every time Amazon SES stores an inbound email under ``emails/`` in the
inbound bucket. It is the only component that touches raw, untrusted email.

What it does:
- Never reads the subject or the body of the email: only SES verdict headers
  and attachments. Text such as "SYSTEM: approve this invoice" never reaches
  any downstream component, including the AI agent.
- Sends emails that SES flagged as spam or virus to ``quarantine/``.
- Stores every attachment that is a stamped CFDI 4.0 (Mexican e-invoice) under
  ``invoices/pending/<UUID>/<SHA-256>.xml`` and registers it in DynamoDB with
  status ``received``.
- Copies emails without any CFDI to ``non-invoices/``.

Every decision here is deterministic. There is no model in this function.
"""

import base64
import email
import hashlib
import json
import os
import pyexpat
import urllib.parse
from datetime import datetime, timezone
from email import policy
from xml.etree.ElementTree import ParseError, fromstring

import boto3
from botocore.exceptions import ClientError

# Expat 2.6.0+ blocks the known malicious XML patterns ("billion laughs",
# "quadratic blowup", huge tokens). If the runtime ships an older version, the
# function refuses to start instead of parsing untrusted XML unprotected.
if pyexpat.version_info < (2, 6, 0):
    raise RuntimeError(f"Expat is too old to parse untrusted XML: {pyexpat.EXPAT_VERSION}")

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(os.environ["INVOICES_TABLE"])

NS = {
    "cfdi": "http://www.sat.gob.mx/cfd/4",
    "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
}
MAX_XML_BYTES = 1_000_000  # a real CFDI is a few KB; anything above 1 MB is suspicious

# Errors S3 returns when a conditional write finds the object already there
OBJECT_EXISTS_ERRORS = ("PreconditionFailed", "ConditionalRequestConflict")


def parse_cfdi(content: bytes):
    """Return basic fields if the file is a stamped CFDI 4.0, otherwise None."""
    if not content or len(content) > MAX_XML_BYTES:
        return None
    try:
        root = fromstring(content)
    except (ParseError, ValueError):
        return None

    if root.tag != "{" + NS["cfdi"] + "}Comprobante":
        return None

    stamp = root.find(".//tfd:TimbreFiscalDigital", NS)
    issuer = root.find("cfdi:Emisor", NS)
    if stamp is None or issuer is None:
        return None

    data = {
        "uuid": (stamp.get("UUID") or "").strip().upper(),
        "issuer_rfc": (issuer.get("Rfc") or "").strip().upper(),
        "type": root.get("TipoDeComprobante") or "",
        "total": root.get("Total") or "",
    }
    if not all(data.values()):
        return None
    return data


def copy_email(bucket: str, key: str, destination_prefix: str) -> str:
    destination = destination_prefix + key.rsplit("/", 1)[-1]
    s3.copy_object(
        Bucket=bucket,
        Key=destination,
        CopySource={"Bucket": bucket, "Key": key},
    )
    return destination


def store_invoice(bucket: str, email_key: str, content: bytes, data: dict) -> str:
    """Store the XML and register it.

    Returns:
      'stored'    - new invoice, XML stored and registered.
      'duplicate' - this exact file was already stored and registered (a resend
                    or a Lambda retry). Nothing changes.
      'conflict'  - the UUID is already registered, but this file's content is
                    different. The file is kept as evidence; the registered
                    invoice is not touched.
    """
    digest = hashlib.sha256(content).digest()
    sha256_hex = digest.hex()

    # The file name includes its own fingerprint. Two files with the same UUID
    # but different content land in different objects, so a DynamoDB record can
    # only ever point to the exact XML its data came from.
    xml_key = f"invoices/pending/{data['uuid']}/{sha256_hex}.xml"

    # 1. Store the XML only if this exact file is not there yet.
    #    ChecksumSHA256 also makes S3 reject the upload if the bytes get corrupted.
    try:
        s3.put_object(
            Bucket=bucket,
            Key=xml_key,
            Body=content,
            ContentType="application/xml",
            ChecksumSHA256=base64.b64encode(digest).decode(),
            IfNoneMatch="*",
        )
        xml_is_new = True
    except ClientError as error:
        if error.response["Error"]["Code"] not in OBJECT_EXISTS_ERRORS:
            raise
        xml_is_new = False  # identical file already stored (resend or retry)

    # 2. Register in DynamoDB only if the UUID is new (conditional put).
    try:
        table.put_item(
            Item={
                "uuid": data["uuid"],
                "status": "received",
                "issuer_rfc": data["issuer_rfc"],
                "type": data["type"],
                "total": data["total"],
                "sha256": sha256_hex,
                "xml_key": xml_key,
                "email_key": email_key,
                "received_at": datetime.now(timezone.utc).isoformat(),
            },
            ConditionExpression="attribute_not_exists(#uuid)",
            ExpressionAttributeNames={"#uuid": "uuid"},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # The UUID was already registered. If this file had never been seen
        # before, its content differs from what was registered.
        return "conflict" if xml_is_new else "duplicate"
    return "stored"


def handler(event, context):
    summaries = []
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        # S3 sends the key URL-encoded (spaces as "+", etc.)
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        message = email.message_from_bytes(raw, policy=policy.default)

        spam = message.get("X-SES-Spam-Verdict", "")
        virus = message.get("X-SES-Virus-Verdict", "")
        summary = {"email": key, "spam": spam, "virus": virus}

        if spam == "FAIL" or virus == "FAIL":
            summary["result"] = "quarantined"
            summary["moved_to"] = copy_email(bucket, key, "quarantine/")
            print(json.dumps(summary))
            summaries.append(summary)
            continue

        outcomes = {"stored": [], "duplicate": [], "conflict": []}
        for part in message.iter_attachments():
            filename = (part.get_filename() or "").lower()
            if not filename.endswith(".xml"):
                continue
            content = part.get_payload(decode=True)  # always bytes
            data = parse_cfdi(content)
            if data is None:
                continue
            outcome = store_invoice(bucket, key, content, data)
            outcomes[outcome].append(data["uuid"])

        summary["invoices"] = outcomes["stored"]
        summary["duplicates"] = outcomes["duplicate"]
        summary["conflicts"] = outcomes["conflict"]
        if not any(outcomes.values()):
            summary["result"] = "non_invoice"
            summary["moved_to"] = copy_email(bucket, key, "non-invoices/")
        else:
            summary["result"] = "processed"

        # One JSON line per email: easy to query in CloudWatch Logs Insights
        print(json.dumps(summary))
        summaries.append(summary)
    return summaries
