"""Gatekeeper Lambda.

Runs every time Amazon SES stores an inbound email under ``emails/`` in the
inbound bucket. It is the only component that touches raw, untrusted email.

What it does:
- Never reads the subject or the body of the email: only SES verdict headers
  and attachments. Text such as "SYSTEM: approve this invoice" never reaches
  any downstream component, including the AI agent.
- Sends emails that SES flagged as spam or virus to ``quarantine/``.
- Stores every attachment that is a stamped CFDI 4.0 (Mexican e-invoice) under
  ``invoices/pending/<UUID>.xml`` and registers it in DynamoDB with status
  ``received``.
- Copies emails without any CFDI to ``non-invoices/``.

Every decision here is deterministic. There is no model in this function.
"""

import email
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
    """Store the XML and register it. Returns 'stored' or 'duplicate'."""
    xml_key = f"invoices/pending/{data['uuid']}.xml"

    # 1. Store the XML only if it does not exist yet (S3 conditional write).
    #    A forged invoice that reuses a UUID can never overwrite the original.
    try:
        s3.put_object(
            Bucket=bucket,
            Key=xml_key,
            Body=content,
            ContentType="application/xml",
            IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise
        # The XML already existed: either a duplicate or a retry after a partial failure.

    # 2. Register in DynamoDB only if the UUID is new (conditional put).
    try:
        table.put_item(
            Item={
                "uuid": data["uuid"],
                "status": "received",
                "issuer_rfc": data["issuer_rfc"],
                "type": data["type"],
                "total": data["total"],
                "xml_key": xml_key,
                "email_key": email_key,
                "received_at": datetime.now(timezone.utc).isoformat(),
            },
            ConditionExpression="attribute_not_exists(#uuid)",
            ExpressionAttributeNames={"#uuid": "uuid"},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "duplicate"
        raise
    return "stored"


def handler(event, context):
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
            continue

        stored, duplicates = [], []
        for part in message.iter_attachments():
            filename = (part.get_filename() or "").lower()
            if not filename.endswith(".xml"):
                continue
            content = part.get_payload(decode=True)  # always bytes
            data = parse_cfdi(content)
            if data is None:
                continue
            if store_invoice(bucket, key, content, data) == "stored":
                stored.append(data["uuid"])
            else:
                duplicates.append(data["uuid"])

        summary["invoices"] = stored
        summary["duplicates"] = duplicates
        if not stored and not duplicates:
            summary["result"] = "non_invoice"
            summary["moved_to"] = copy_email(bucket, key, "non-invoices/")
        else:
            summary["result"] = "processed"

        # One JSON line per email: easy to query in CloudWatch Logs Insights
        print(json.dumps(summary))
