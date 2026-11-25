# Decision Lambda

Deterministic check of the agent's proposal. When an invoice moves to `match_proposed`, it compares the invoice XML with the purchase order line by line and moves the invoice to `matched` or `needs_review`. There is no model in this function.

## Rules it enforces

| Rule | How |
|---|---|
| The code decides, not the agent | The agent's proposal only says which line goes with which; every number is read again from the stored XML and the order |
| Only income invoices match automatically | `TipoDeComprobante` must be `I`; credit notes and others go to review |
| Large purchases need a person | If the larger of `SubTotal` and `Total` is at or above `MAX_AUTO_MATCH_TOTAL` (default 10,000), the invoice goes to review even if everything else matches. `SubTotal` is the value of the goods before discounts and withholding, so neither can hide the size of a purchase. The gatekeeper applies the same rule to the declared amounts before the agent runs; this is the second line |
| Prices agree with the order | Unit price within `PRICE_TOLERANCE` (default 0.01) of the agreed price |
| No more than what was ordered | Invoiced quantity ≤ ordered − already invoiced, per line |
| The invoice adds up | `Importe = Cantidad × ValorUnitario`; `SubTotal = Σ Importe`; `Total = SubTotal − Descuento + Traslados − Retenciones` (one-cent rounding) |
| Discounts and taxes in the header are the ones the lines declare | `Descuento = Σ line Descuento`; `TotalImpuestosTrasladados = Σ line Traslado` (one cent per line); each line's IVA base is `Importe − Descuento` and its amount is `base × rate`. Without this, a supplier could inflate the taxes, or shrink the total with a discount, and the arithmetic above would still add up |
| Only plain IVA is matched automatically | IVA (`Impuesto 002`) at 16%, 8% (border regions) or 0%, or exempt. Any other tax, any other rate, and any withholding (`Retenciones`) send the invoice to a person |
| Same currency, open order, every line matched | Otherwise `needs_review` |
| Every problem is reported at once | All reasons are collected in `decision_reasons` |
| An order cannot be invoiced twice | On `matched`, invoiced quantities are added to the order in the same transaction, with an optimistic-lock condition on the order |
| Retries are harmless | Every write is conditional on `status = match_proposed` |
| It cannot touch the proposal, totals or `paid` | IAM `dynamodb:Attributes`: invoice `uuid`, `status`, `decision_reasons`, `decided_at`; order `invoiced_quantities`, `status` |

## Processing steps

1. The pipe publishes the stream record on the invoice events bus, and the rule `tally-match-proposed` delivers a `MODIFY` with `NewImage.status = match_proposed` as `{"Records": [record]}` (filtered by the rule and checked again in code).
2. Read the invoice (consistent read), the XML from S3 and the proposed order (key built from the invoice's RFC).
3. Run every check and collect the reasons.
4. With reasons: set `needs_review` and `decision_reasons`.
5. Without reasons: one `TransactWriteItems` sets the invoice to `matched` and the order's `invoiced_quantities` (closing the order when fully invoiced). If the order changed in between, the transaction is cancelled and the function raises, so Lambda retries the event with fresh data (2 retries, then the failure queue).

Output, one JSON log line per record:

```json
{"invoice": "2D8A4E6F-1B3C-4A5D-9E7F-6C0B8A2D4E13", "result": "needs_review",
 "reasons": ["Invoice line 1 / order line 2: unit price 215.00 differs from the agreed price 200.00 (15.00 more)."]}
```

`result` is `matched`, `needs_review`, `skipped_not_pending` or `skipped_already_decided`.

## IAM permissions

| Statement | Actions | Resource |
|---|---|---|
| `ReadInvoiceRecord` | `dynamodb:GetItem` | Invoices table |
| `RecordDecision` | `dynamodb:UpdateItem`, only `uuid`, `status`, `decision_reasons`, `decided_at` | Invoices table |
| `ReadInvoiceXml` | `s3:GetObject` | `invoices/pending/*` only |
| `ReadPurchaseOrders` | `dynamodb:GetItem` | Purchase orders table |
| `RecordInvoicedQuantities` | `dynamodb:UpdateItem`, only `supplier_rfc`, `order_id`, `invoiced_quantities`, `status` | Purchase orders table |
| `SendFailedEvents` | `sqs:SendMessage` | The failure queue (Lambda's on-failure destination) |
| `WriteLogs` | `logs:CreateLogStream`, `PutLogEvents` | Its own log group |

## Configuration

| Environment variable | Value |
|---|---|
| `INVOICES_TABLE` | Invoices table name |
| `PURCHASE_ORDERS_TABLE` | Purchase orders table name |
| `BUCKET` | Inbound bucket name |
| `PRICE_TOLERANCE` | Allowed unit price difference, default `0.01` |
| `MAX_AUTO_MATCH_TOTAL` | Invoices with a total at or above this amount always need a person, default `10000` |

## Tests

```
pip install -r lambdas/decision/tests/requirements.txt
python -m unittest discover -s lambdas/decision/tests -v
```

33 tests (moto): matching invoice recorded on the order; order closed when fully invoiced; rounding within tolerance; a total at the automatic limit goes to review and one cent below matches; a large purchase hidden by a discount goes to review; withholding, header taxes the lines do not declare, line IVA that is not base × rate, a rate that does not exist, a tax other than IVA and a header discount the lines do not declare all go to review; 8% border IVA matches; review for price above agreed, quantity above pending, line amount, total, unmatched line, no order, credit note, currency, closed order; all reasons reported together; a wrong pairing by the agent is caught; the same order cannot be invoiced twice; an order changed mid-decision is retried (with and without existing quantities) and then reviewed; a retry after a decision changes nothing; other stream events ignored; XML of another invoice; unreadable XML; shared `cfdi.py` identical. All pass.

## Design decisions

- **Re-read everything.** The function trusts neither the agent nor the tools' earlier reads; it only trusts the stored XML, whose fingerprint is in its file name, and the order table.
- **Transaction with optimistic lock.** Two invoices for the same order can be decided at the same moment; the condition on `invoiced_quantities` lets only one win, and the other is re-evaluated with the new quantities.
- **`matched` is not "pay".** SAT verification and a human are still required before an invoice is marked paid.

## Known limitations

- Taxes are checked as totals (`TotalImpuestosTrasladados`), not per tax rate.
- One invoice is matched to one order. Invoices that cover several orders go to review.
- Unit conversions (a box of 24 vs 24 pieces) are not handled: the quantity check would send them to review.
