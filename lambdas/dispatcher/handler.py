"""Dispatcher Lambda.

Triggered by the DynamoDB stream of the invoices table, only for newly
inserted invoices with status ``received`` (filtered at the event source
mapping). For each one, it invokes the invoice-matching agent on the
AgentCore harness and passes it the invoice UUID.

The prompt is fixed and built here. The only variable part is the UUID, read
from our own table and validated with a strict pattern, so no text written by
a supplier ever reaches the agent through this function. The agent reads the
invoice itself through its tools.
"""

import json
import os
import re
import uuid

import boto3

client = boto3.client("bedrock-agentcore")

# InvokeHarness is a recent API. If the runtime ships an older boto3, fail at
# startup with a clear message instead of failing on every invocation.
if not hasattr(client, "invoke_harness"):
    raise RuntimeError("The boto3 in this runtime does not support bedrock-agentcore InvokeHarness")

HARNESS_ARN = os.environ["HARNESS_ARN"]
MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "10"))
TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "120"))

# Same format the gatekeeper stores: upper-case CFDI UUID
UUID_PATTERN = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")

PROMPT_TEMPLATE = (
    "Match invoice {invoice_uuid} against the open purchase orders of its supplier "
    "and submit your proposal with the propose_match tool."
)

ERROR_EVENTS = ("internalServerException", "validationException", "runtimeClientError")


def invoke_agent(invoice_uuid: str) -> dict:
    """Invoke the harness for one invoice and return a summary of the run."""
    # A new session per run keeps every invoice isolated from the others.
    # Session IDs must be 33-100 characters: "invoice-" + UUID + "-" + 32 hex = 77.
    session_id = f"invoice-{invoice_uuid}-{uuid.uuid4().hex}"

    response = client.invoke_harness(
        harnessArn=HARNESS_ARN,
        runtimeSessionId=session_id,
        messages=[{"role": "user", "content": [{"text": PROMPT_TEMPLATE.format(invoice_uuid=invoice_uuid)}]}],
        maxIterations=MAX_ITERATIONS,
        timeoutSeconds=TIMEOUT_SECONDS,
    )

    text, stop_reason, usage = [], None, {}
    for event in response["stream"]:
        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            if "text" in delta:
                text.append(delta["text"])
        elif "messageStop" in event:
            # The agent stops once per loop iteration; the last one is the final reason.
            stop_reason = event["messageStop"]["stopReason"]
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {})
        else:
            for error_key in ERROR_EVENTS:
                if error_key in event:
                    # Raising makes the stream retry this record.
                    raise RuntimeError(f"{error_key}: {event[error_key].get('message', '')}")

    return {
        "session_id": session_id,
        "stop_reason": stop_reason,
        "answer": "".join(text)[-500:],  # last part of the final answer, for the logs
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
    }


def handler(event, context):
    results = []
    for record in event["Records"]:
        new_image = record.get("dynamodb", {}).get("NewImage", {})
        invoice_uuid = new_image.get("uuid", {}).get("S", "")
        status = new_image.get("status", {}).get("S", "")
        summary = {"invoice": invoice_uuid, "event": record.get("eventName")}

        # The event source mapping already filters INSERT + status "received".
        # Check again here so a misconfigured filter can never wake the agent.
        if record.get("eventName") != "INSERT" or status != "received":
            summary["result"] = "skipped_not_new"
        elif not UUID_PATTERN.match(invoice_uuid):
            summary["result"] = "skipped_invalid_uuid"
        else:
            summary.update(invoke_agent(invoice_uuid))
            summary["result"] = "agent_invoked"

        print(json.dumps(summary))
        results.append(summary)
    return results
