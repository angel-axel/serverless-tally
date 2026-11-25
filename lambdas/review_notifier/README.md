# Review notifier Lambda

Sends one email to a review address every time an invoice moves to status `needs_review`, with the CFDI XML attached, so a person can decide what to do with it. The email is HTML (template `review_email.html`) with a plain-text alternative carrying the same content.

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
4. Reads the line descriptions from the XML with the shared `cfdi.py`, removes control and invisible characters (zero-width spaces, bidirectional overrides) and escapes them.
5. Sends the email through SES: `multipart/alternative` (plain text and HTML) plus the XML attached.

## Rules it enforces

| Rule (plain language) | How it is enforced |
|---|---|
| The notifier cannot change an invoice | IAM allows only `dynamodb:GetItem` on the invoices table and `s3:GetObject` on `invoices/pending/*` |
| It can only email the review address, from the review sender | IAM allows `ses:SendRawEmail` only on the domain identity, with conditions on `ses:FromAddress` and `ses:Recipients` |
| Supplier text never goes in the subject | The subject is `Invoice to review: <UUID>`; the UUID is checked against its format first |
| Supplier text cannot become markup | Every value is passed through `html.escape` before it enters the template; the template is fixed and has no links, images, scripts or external resources |
| Supplier text is shown only as supplier text | Line descriptions appear in quotes, under "Invoice lines, as written by the supplier (unverified)", inside the "Why it was stopped" box, next to a warning that phrases such as "URGENT" or "pre-approved" may be social engineering. Decision reasons never include supplier text |
| Text cannot look different from what it says | Control, format (zero-width, bidirectional override) and private-use characters are removed before escaping |
| No value is shown unless it has its expected form | The RFC is shown only if it has a valid RFC format, the total only if it is a number, dates only if they parse |
| A stale or repeated event does not bother a person who already decided | The record is read again; the email is sent only while the status is `needs_review` |
| A failed email is not lost | Any error propagates: Lambda retries twice, then the event goes to the failure queue `tally-failed-events` |

## The email

Subject: `Invoice to review: <UUID>`. Body, in English, in this order: total, supplier RFC, UUID, received and flagged dates; a "Why it was stopped" box with the reasons, the invoice lines as written by the supplier (quoted, marked unverified) and the social-engineering warning; a note about the attachment (open it in a text editor, not a browser); and a footer saying the email has no links and never asks for information or payments. The XML is attached as `<UUID>.xml`.

To change the look, edit `review_email.html`. It uses `string.Template` placeholders (`$uuid`, `$rfc`, `$total`, `$received`, `$flagged`, `$reasons`, `$supplier_lines`); the code fills them with escaped values. Keep it free of links and external resources.

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

17 tests (moto): the email carries the invoice and its XML; HTML and plain-text versions; the social-engineering warning in both; HTML in a description and in a reason shown as text; no links, images or scripts; invisible and bidirectional characters removed; supplier text never in the subject; unreadable XML still sends a fixed note; a non-numeric total not shown; long reasons cut; lowercase UUID; stale event not sent; missing invoice; invalid UUID refused before any read; XML outside the invoice folder never attached; SES failure raised so Lambda retries.

## Known limitations

- EventBridge delivers at least once, so in rare cases the same invoice may be emailed twice. The notifier does not write a "notified" mark on purpose: that write would be another change on a `needs_review` invoice and would trigger the rule again.
- Mail clients turn text that looks like a URL or an email address into a link, even when it is escaped. Supplier descriptions can contain such text; the warning in the email tells the reviewer not to open links inside supplier text, and the template itself has none.
