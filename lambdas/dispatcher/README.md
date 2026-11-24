# Dispatcher Lambda

Starts the invoice-matching agent for every new invoice. It is the only bridge between the deterministic part of Tally (gatekeeper, DynamoDB) and the AI agent, and it is deliberately narrow: it passes the agent one validated UUID and nothing else.

## Rules it enforces

| Rule | How |
|---|---|
| Only brand-new invoices wake the agent | The EventBridge rule `tally-new-invoice` filters `INSERT` + `status = "received"`; the handler checks both again |
| Status changes never re-trigger the agent | The agent's own update (`received` → `match_proposed`) is a `MODIFY`, which is filtered out, so there is no loop |
| No supplier-written text reaches the agent through this function | The prompt is a fixed template; the only variable is the UUID, which must match a strict upper-case UUID pattern |
| Each invoice runs in isolation | A new AgentCore session per invoice; no context carries over between invoices |
| Agent runs are bounded | `maxIterations` and `timeoutSeconds` are sent on every call; the Lambda timeout is the agent timeout plus 60 s |
| The function can invoke only this agent | Its IAM role allows `InvokeHarness` / `InvokeAgentRuntime` on the invoice matcher harness ARN only |
| A failed run is retried, not silently lost | Agent error events raise an exception, so Lambda retries the event (2 retries, max age 1 h) and then sends it to the SQS queue `tally-failed-events` |

## Processing steps

1. The gatekeeper inserts an invoice into the `tally-invoices` table with `status = "received"`.
2. DynamoDB Streams records the change (`NEW_IMAGE`).
3. The EventBridge pipe `tally-invoices-stream`, the only reader of the stream, publishes the record on the bus `tally-invoice-events`. The rule `tally-new-invoice` matches it and invokes the dispatcher asynchronously with `{"Records": [record]}`, the same shape a stream event has.
4. The handler re-checks the event type and status, and validates the UUID.
5. It calls `InvokeHarness` with the fixed prompt:
   `Match invoice <UUID> against the open purchase orders of its supplier and submit your proposal with the propose_match tool.`
6. It reads the response stream (text, stop reason, token usage, error events) and logs a JSON summary.

## Inputs and outputs

**Input:** a DynamoDB stream record wrapped as `{"Records": [record]}` by the rule's input transformer. Only `eventName`, `NewImage.uuid` and `NewImage.status` are read.

**Output:** one JSON log line per record, for example:

```json
{"invoice": "5A3C9E21-7B4D-4F8A-9C12-0E6D8B4F2A71", "event": "INSERT",
 "session_id": "invoice-5A3C9E21-...-3f9c...", "stop_reason": "end_turn",
 "answer": "Proposed a match with order OC-1042; all 3 lines matched.",
 "input_tokens": 1200, "output_tokens": 80, "result": "agent_invoked"}
```

`result` is one of `agent_invoked`, `skipped_not_new` or `skipped_invalid_uuid`.

The dispatcher writes nothing to DynamoDB or S3. The agent records its proposal through its own `propose_match` tool.

## IAM permissions

| Statement | Actions | Resource |
|---|---|---|
| `SendFailedEvents` | `sqs:SendMessage` | The failure queue (Lambda's on-failure destination) |
| `InvokeInvoiceMatcherAgent` | `bedrock-agentcore:InvokeHarness`, `InvokeAgentRuntime` | The invoice matcher harness only |
| `WriteLogs` | `logs:CreateLogStream`, `PutLogEvents` | Its own log group |

The function does not read the stream. A resource-based permission lets only the rule `tally-new-invoice` invoke it.

## Configuration

| Environment variable | Source | Default |
|---|---|---|
| `HARNESS_ARN` | ARN of `aws_bedrockagentcore_harness.invoice_matcher_harness` | required |
| `AGENT_MAX_ITERATIONS` | `var.agent_max_iterations` | `10` |
| `AGENT_TIMEOUT_SECONDS` | `var.agent_timeout_seconds` | `120` |

## Tests

The AgentCore client is replaced with a mock, so no AWS account is needed:

```
pip install -r lambdas/dispatcher/tests/requirements.txt
python -m unittest discover -s lambdas/dispatcher/tests -v
```

| Test | What it proves |
|---|---|
| New invoice invokes the agent with only the UUID | The prompt is exactly the fixed template plus the UUID |
| Session ID is unique and within limits | Every run gets its own session, 33–100 characters |
| Status changes do not wake the agent | `MODIFY`, `REMOVE` or another status are skipped, even with a misconfigured filter |
| Malformed UUID never reaches the prompt | A UUID with an injected instruction appended is rejected |
| Agent error is raised so Lambda retries | Error events in the response stream fail the invocation |
| Summary includes stop reason and usage | Logs carry the stop reason, final answer and token counts |

All 6 tests pass.

## Design decisions

- **A fixed prompt instead of forwarding the invoice.** The dispatcher could send the invoice content to save a tool call, but then supplier-written text would arrive as part of the user message. Sending only the UUID keeps third-party text inside tool results, where the agent's instructions treat it as data.
- **Filter twice.** The EventBridge rule saves invocations and cost; the check in the handler is defense in depth in case the filter is changed or removed.
- **One record per invocation.** With `batch_size = 1`, a failing invoice is retried on its own and does not block or repeat other invoices.
- **Bounded retries.** Two Lambda retries and a one-hour maximum age; the pipe reading the stream never waits on the agent, so a slow run cannot block other invoices.
- **Fail at startup on an old SDK.** `InvokeHarness` is a recent API; checking for it at import time gives one clear error instead of a failure on every invoice.

## Known limitations

- After the last retry, the event goes to the SQS queue `tally-failed-events`, but the invoice stays in `received` and nothing alerts the owner yet. A CloudWatch alarm on the queue is the planned fix.
- The dispatcher waits for the whole agent run, so it is billed for the agent's duration. An asynchronous invocation would be cheaper at higher volumes.
- Retries start a new agent session, so a partially completed run may repeat tool calls. `propose_match` must stay idempotent (conditional update on `status = "received"`).
