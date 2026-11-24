# Review notifier Lambda

Sends one email to a review address every time an invoice moves to status `needs_review`, with the CFDI XML attached, so a person can decide what to do with it.

An invoice reaches `needs_review` from two places:

- **The gatekeeper**, when the guardrail flags the supplier's text or cannot screen it. The record is inserted directly with that status.
- **The decision Lambda**, when the agent's proposal does not hold up against the XML and the purchase order (price, quantity, totals, no open order, over the automatic limit).

The notifier does not care which one it was: it reacts to the status, so any future component that sends invoices to review is covered without changes.

## How it is triggered

```
DynamoDB stream ──► EventBridge Pipe ──► tally-invoice-events bus ──► rule "needs_review" ──► this Lambda
```

The rule matches `INSERT` or `MODIFY` events whose new image has `status = needs_review`. Its input transformer delivers only the UUID:

```json
{"invoice_uuid": "895C0CFA-9A6A-4827-8057-01CCE59D94B2"}
```

## What it does

1. Validates the UUID format.
2. Reads the invoice record again with a consistent read. If it no longer exists or is no longer in `needs_review`, it does nothing: the event is stale or someone already handled it.
3. Reads the XML from `invoices/pending/<UUID>/…` (any other location is refused).
4. Sends a plain-text email through SES with the XML attached.

## Rules it enforces

| Rule (plain language) | How it is enforced |
|---|---|
| The notifier cannot change an invoice | IAM allows only `dynamodb:GetItem` on the invoices table and `s3:GetObject` on `invoices/pending/*` |
| It can only email the review address, from the review sender | IAM allows `ses:SendRawEmail` only on the domain identity, with conditions on `ses:FromAddress` and `ses:Recipients` |
| Supplier text never goes in the subject | The subject is `Factura por revisar: <UUID>`; the UUID is checked against its format first |
| Nothing in the email is rendered | Plain text only, no HTML part. The RFC is shown only if it has a valid RFC format, the total only if it is a number |
| A stale or repeated event does not bother a person who already decided | The record is read again; the email is sent only while the status is `needs_review` |
| A failed email is not lost | Any error propagates: Lambda retries twice, then the event goes to the failure queue `tally-failed-events` |

## The email

```
Subject: Factura por revisar: 895C0CFA-9A6A-4827-8057-01CCE59D94B2

Una factura necesita revisión humana antes de cualquier pago.

UUID:          895C0CFA-9A6A-4827-8057-01CCE59D94B2
RFC emisor:    KIJ0906199R1
Total:         604.36 MXN
Recibida:      2026-11-24T10:00:30+00:00
Marcada:       2026-11-24T10:01:10+00:00

Motivos:
- Invoice line 1 quantity 8 exceeds the pending quantity 5 of order line 1.

El XML de la factura va adjunto tal como se recibió. …

Attachment: 895C0CFA-9A6A-4827-8057-01CCE59D94B2.xml
```

## IAM permissions

| Sid | Actions | Resource |
|---|---|---|
| `ReadInvoiceRecord` | `dynamodb:GetItem` | Invoices table |
| `ReadInvoiceXml` | `s3:GetObject` | `invoices/pending/*` |
| `SendReviewEmail` | `ses:SendRawEmail`, only from the review sender to the review address | The SES domain identity |
| `SendFailedEvents` | `sqs:SendMessage` | The failure queue (Lambda's on-failure destination) |
| `WriteLogs` | `logs:CreateLogStream`, `logs:PutLogEvents` | Its own log group |

## Configuration

| Environment variable | Meaning |
|---|---|
| `INVOICES_TABLE` | Invoices table name |
| `BUCKET` | Inbound bucket name |
| `SENDER` | Sender address, `review@<domain>` by default |
| `REVIEW_EMAIL` | Where review emails go |

The sender is authorized by the SES domain identity that already receives the invoices, so no new sending identity is needed. The SES account is out of the sandbox, so `REVIEW_EMAIL` can be any address; if an account were in the sandbox, that address would also have to be verified in SES.

## Tests

```
pip install -r lambdas/review_notifier/tests/requirements.txt
python -m unittest discover -s lambdas/review_notifier/tests -v
```

11 tests (moto): the email carries the invoice and its XML, plain text only, lowercase UUID, stale event not sent, missing invoice, invalid UUID refused before any read, XML outside the invoice folder never attached, supplier text never in the subject, a non-numeric total not shown, long reasons cut, SES failure raised so Lambda retries.

## Known limitations

- EventBridge delivers at least once, so in rare cases the same invoice may be emailed twice. The notifier does not write a "notified" mark on purpose: that write would be another change on a `needs_review` invoice and would trigger the rule again.
- Decision reasons can include up to 60 characters of a line description, which is supplier text. The email is plain text and the reviewer is a person, so this is accepted, but the decision Lambda could use fixed wording instead.
