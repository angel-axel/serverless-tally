# propose_match tool

Lambda behind the agent's `propose_match` tool (AgentCore Gateway target). It is the only tool that writes: it records which invoice line corresponds to which purchase order line, and moves the invoice from `received` to `match_proposed`. The decision Lambda checks the numbers afterwards.

## Rules it enforces

| Rule | How |
|---|---|
| A proposal is validated before it is stored | Invoice exists and is `received`; every invoice line listed exactly once; order belongs to the invoice's supplier and is open; every order line exists and is used at most once; no order means every line unmatched |
| The agent cannot point to another supplier's order | The order key is built from the invoice's RFC, never from the agent |
| Lines are checked against the real files | The invoice lines come from the stored XML, the order lines from the table |
| A proposal is never overwritten | `UpdateItem` with `ConditionExpression` `status = received`; a retry or second call gets an error |
| It can only write the proposal | IAM `dynamodb:Attributes` allows `uuid`, `status`, `match_proposal`, `proposed_at` only: no totals, RFC or `paid` |
| The agent never approves anything | The response says the proposal will be checked separately; there is no approval path |
| Bounded input | `order_id` pattern `[A-Za-z0-9_-]{1,64}`; integer line numbers (booleans rejected); notes cut to 500 characters |

## Input and output

Input:

```json
{
  "invoice_uuid": "9B2F6C14-5E3A-4D71-A8C0-3F1E7D9B2A56",
  "order_id": "OC-1001",
  "lines": [{"invoice_line": 1, "order_line": 2}, {"invoice_line": 2, "order_line": 1}],
  "notes": "Both lines matched."
}
```

Output: `{"result": "proposal_recorded", "invoice_uuid": "...", "order_id": "OC-1001", "matched_lines": 2, "total_lines": 2, "note": "The proposal will be checked by a separate system. Nothing has been approved."}`, or `{"error": "..."}` with the reason, so the agent can correct the proposal.

Stored on the invoice record:

```json
{"status": "match_proposed",
 "match_proposal": {"order_id": "OC-1001", "lines": [{"invoice_line": 1, "order_line": 2}, ...], "notes": "..."},
 "proposed_at": "2026-11-22T16:40:12+00:00"}
```

## IAM permissions

| Statement | Actions | Resource |
|---|---|---|
| `ReadInvoiceRecord` | `dynamodb:GetItem` | Invoices table |
| `RecordMatchProposal` | `dynamodb:UpdateItem`, only attributes `uuid`, `status`, `match_proposal`, `proposed_at` | Invoices table |
| `ReadInvoiceXml` | `s3:GetObject` | `invoices/pending/*` only |
| `ReadPurchaseOrders` | `dynamodb:GetItem`, `Query` | Purchase orders table |
| `WriteLogs` | `logs:CreateLogStream`, `PutLogEvents` | Its own log group |

## Configuration

| Environment variable | Value |
|---|---|
| `INVOICES_TABLE` | Invoices table name |
| `PURCHASE_ORDERS_TABLE` | Purchase orders table name |
| `BUCKET` | Inbound bucket name |

## Tests

```
pip install -r lambdas/propose_match/tests/requirements.txt
python -m unittest discover -s lambdas/propose_match/tests -v
```

18 tests (moto): valid proposal stored without touching other attributes, unmatched line, no order; rejections for another supplier's order, closed order, order line used twice, invented invoice or order line, missing or duplicated invoice line, order lines without an order, wrong types, injected `order_id`; notes cut; a second proposal never overwrites the first; a status change between validation and write is refused; another invoice cannot be proposed; shared `cfdi.py` identical. All pass.

## Design decisions

- **Line numbers only.** The proposal stores which line goes with which, never quantities or prices. The decision Lambda reads the numbers from the source, so the agent cannot inject figures.
- **Errors go back to the agent.** A rejected proposal is not an exception: the agent receives the reason and can fix it within the same run, bounded by the harness iteration limit.
- **Validation plus a conditional write.** Validation catches bad proposals; the condition catches races (two runs for the same invoice).

## Known limitations

- Like the other tools, it cannot verify that the UUID is the one the agent was given. A manipulated agent could propose for another invoice in `received`, but the proposal still has to pass every check here and in the decision Lambda.
