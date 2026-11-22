"""CFDI 4.0 parsing shared by read_invoice, propose_match and decision.

The same file is copied into each of those Lambda folders, so every package
stays self-contained (no layers). A test in each folder checks that its copy is
identical to this one, so the copies cannot drift apart.

The XML comes from a third party. Parsing relies on the protections of Expat
2.6.0+ (entity expansion, huge tokens), on a size limit, and on reading only
known elements and attributes. Amounts are returned as Decimal, never float.
"""

import pyexpat
from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import ParseError, fromstring

if pyexpat.version_info < (2, 6, 0):
    raise RuntimeError(f"Expat is too old to parse untrusted XML: {pyexpat.EXPAT_VERSION}")

NS = {
    "cfdi": "http://www.sat.gob.mx/cfd/4",
    "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
}
MAX_XML_BYTES = 1_000_000
MAX_LINES = 200
MAX_TEXT = 300  # longer supplier text is cut: it is a description, not a document


class InvalidCfdi(ValueError):
    """The file is not a stamped CFDI 4.0 this system can read."""


def _decimal(value, field, required=True):
    if value is None or value == "":
        if required:
            raise InvalidCfdi(f"missing {field}")
        return Decimal("0")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise InvalidCfdi(f"invalid number in {field}") from error
    if not number.is_finite() or number < 0:
        raise InvalidCfdi(f"invalid number in {field}")
    return number


def _text(value):
    return " ".join((value or "").split())[:MAX_TEXT]


def parse_invoice(content: bytes) -> dict:
    """Return the header and lines of a stamped CFDI 4.0, or raise InvalidCfdi."""
    if not content or len(content) > MAX_XML_BYTES:
        raise InvalidCfdi("empty or oversized file")
    try:
        root = fromstring(content)
    except (ParseError, ValueError) as error:
        raise InvalidCfdi("not well-formed XML") from error

    if root.tag != "{" + NS["cfdi"] + "}Comprobante":
        raise InvalidCfdi("not a CFDI 4.0")

    stamp = root.find(".//tfd:TimbreFiscalDigital", NS)
    issuer = root.find("cfdi:Emisor", NS)
    if stamp is None or issuer is None:
        raise InvalidCfdi("missing stamp or issuer")

    taxes = root.find("cfdi:Impuestos", NS)
    header = {
        "uuid": (stamp.get("UUID") or "").strip().upper(),
        "issuer_rfc": (issuer.get("Rfc") or "").strip().upper(),
        "issuer_name": _text(issuer.get("Nombre")),
        "type": root.get("TipoDeComprobante") or "",
        "currency": root.get("Moneda") or "",
        "subtotal": _decimal(root.get("SubTotal"), "SubTotal"),
        "discount": _decimal(root.get("Descuento"), "Descuento", required=False),
        "transferred_taxes": _decimal(
            taxes.get("TotalImpuestosTrasladados") if taxes is not None else None,
            "TotalImpuestosTrasladados", required=False),
        "withheld_taxes": _decimal(
            taxes.get("TotalImpuestosRetenidos") if taxes is not None else None,
            "TotalImpuestosRetenidos", required=False),
        "total": _decimal(root.get("Total"), "Total"),
    }
    if not header["uuid"] or not header["issuer_rfc"]:
        raise InvalidCfdi("missing UUID or issuer RFC")

    concepts = root.findall("cfdi:Conceptos/cfdi:Concepto", NS)
    if not concepts:
        raise InvalidCfdi("no lines")
    if len(concepts) > MAX_LINES:
        raise InvalidCfdi("too many lines")

    lines = []
    for number, concept in enumerate(concepts, start=1):
        lines.append({
            "line": number,
            "description": _text(concept.get("Descripcion")),
            "product_code": _text(concept.get("ClaveProdServ")),
            "supplier_code": _text(concept.get("NoIdentificacion")),
            "unit": _text(concept.get("Unidad") or concept.get("ClaveUnidad")),
            "quantity": _decimal(concept.get("Cantidad"), f"line {number} Cantidad"),
            "unit_price": _decimal(concept.get("ValorUnitario"), f"line {number} ValorUnitario"),
            "amount": _decimal(concept.get("Importe"), f"line {number} Importe"),
            "discount": _decimal(concept.get("Descuento"), f"line {number} Descuento", required=False),
        })

    header["lines"] = lines
    return header
