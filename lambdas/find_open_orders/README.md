# find_open_orders tool

Lambda behind the agent's `find_open_orders` tool (AgentCore Gateway target). Given an invoice UUID, it returns the open purchase orders of that invoice's supplier. It is read-only.

## Rules it enforces

| Rule | How |
|---|---|
| The agent cannot pick the supplier | The tool takes the invoice UUID, not an RFC, and looks the supplier up itself; extra arguments are ignored |
| It reads as little of the invoice as possible | `ProjectionExpression` on `uuid`, `issuer_rfc`, `status`, enforced by an IAM `dynamodb:Attributes` condition |
| The agent never sees money | Order lines are returned without prices |
| Only orders that can still be invoiced | Status `open`; fully invoiced lines are hidden; pending quantity is shown |
| Bounded output | At most 20 orders, most recent first; text cut to 300 characters |
| Only invoices waiting for a proposal | Status must be `received` |

## Input and output

Input: `{"invoice_uuid": "9B2F6C14-5E3A-4D71-A8C0-3F1E7D9B2A56"}`

Output:

```json
{
  "invoice_uuid": "9B2F6C14-5E3A-4D71-A8C0-3F1E7D9B2A56",
  "supplier_rfc": "EKU9003173C9",
  "orders": [
    {"order_id": "OC-1001", "created_at": "2026-11-15", "lines": [
      {"order_line": 1, "description": "Anticongelante verde galón", "unit": "pieza", "quantity_pending": "8"},
      {"order_line": 2, "description": "Aceite 25W50 Quaker litro", "unit": "pieza", "quantity_pending": "10"}
    ]}
  ],
  "note": "Only orders with status open are listed. An empty list means no open order fits."
}
```

## IAM permissions

| Statement | Actions | Resource |
|---|---|---|
| `ReadInvoiceSupplier` | `dynamodb:GetItem`, only attributes `uuid`, `issuer_rfc`, `status` | Invoices table |
| `QueryPurchaseOrders` | `dynamodb:Query` | Purchase orders table |
| `WriteLogs` | `logs:CreateLogStream`, `PutLogEvents` | Its own log group |

## Configuration

| Environment variable | Value |
|---|---|
| `INVOICES_TABLE` | Invoices table name |
| `PURCHASE_ORDERS_TABLE` | Purchase orders table name |

## Tests

```
pip install -r lambdas/find_open_orders/tests/requirements.txt
python -m unittest discover -s lambdas/find_open_orders/tests -v
```

8 tests (moto): only open orders of the invoice's supplier, a `supplier_rfc` argument from the agent is ignored, the projection matches the IAM condition, no prices returned, pending quantities and hidden fully invoiced lines, 20-order cap, invoice not pending, invalid and unknown UUID. All pass.

## Design decisions

- **UUID in, not RFC.** If the tool accepted an RFC, a prompt injection could make the agent list another supplier's orders. Taking the UUID moves that decision from the model to code.
- **Attribute-level IAM.** Even a bug in this function cannot read the invoice total, file location or proposal.

## Known limitations

- Open orders are filtered after the query (`FilterExpression`), so a supplier with thousands of closed orders makes the query read them all. A sparse index on open orders would fix it at higher volume.
