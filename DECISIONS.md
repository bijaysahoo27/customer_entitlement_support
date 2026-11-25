# Support Copilot MCP decisions

## Tool design

| Tool | Input and response | Reason |
| --- | --- | --- |
| `search_cases` | Optional filters, a bounded page size, and an opaque cursor. Returns selected case fields, a case version, and the next cursor. | Matches the REST search while limiting output and exposing the version needed for writes. Priority and support tier use enums. |
| `get_customer_summary` | Requires a customer ID. Returns name, tier, renewal, usage totals, and case counts. | The REST resource includes complete case summaries and may include finance fields. The tool leaves those out and directs detailed queries to search. |
| `add_case_comment` | Requires a case ID, UUID, and bounded message. Accepts an optional case version. Returns a short write receipt. | Uses the same REST request semantics. A stable UUID makes a retry safe and the receipt avoids echoing the comment. |
| `escalate_case` | Requires a case ID, UUID, current case version, reason enum, and bounded details. Returns a short write receipt. | The tool combines the reason and details into the REST message. A required version prevents acting on stale case information. |

The schemas reject unknown properties. Tool descriptions explain when to call each operation and how to retry. Reads are marked read-only. The escalation tool is marked as a potentially destructive write with external effects because an escalation can trigger webhook delivery. Annotations and instructions inform the host and model. API scopes, account grants, case state, and version checks enforce access and write preconditions.

## Runtime and testing

Claude Desktop launches the stdio process using `docker compose exec -T api python -m customer_entitlement_support.mcp_server`. The process sends HTTP requests to FastAPI over the API container's loopback interface. It reads its API token from the configured local token file or `SUPPORT_MCP_ACCESS_TOKEN` in the container environment. Tool inputs and the Claude Desktop configuration contain no tokens.

From WSL, start or rebuild the API with `docker compose up -d --build --wait`. Quit and reopen Claude Desktop after saving its config. Ask Claude to use `search_cases` to find open P1 cases for `ACC-007`. The fixture case is `CS-10231`.

Escalation requires `cases:escalate`. For a local test with no webhook destination configured, run `SUPPORT_LOCAL_ESCALATION=1 docker compose up -d --build --wait api`, reconnect Claude, and ask it to look up the case and request your approval before escalating it. A successful escalation changes the case and queues a webhook event. The same action UUID and reason can be replayed. A second escalation with a different UUID returns a conflict for an already escalated case.
