# Support Copilot MCP decisions

## Tool design

| Tool | Input and response | Reason |
| --- | --- | --- |
| `search_cases` | Optional filters, a bounded page size, and an opaque cursor. Returns selected case fields, a case version, and the next cursor. | Matches the REST search while limiting output and exposing the version needed for writes. Priority and support tier use enums. |
| `get_customer_summary` | Requires a customer ID. Returns tier, renewal, usage totals, and case counts. | The tool omits names, finance fields, and full case summaries. Search provides structured case details. |
| `add_case_comment` | Requires a case ID, UUID, and bounded message. Accepts an optional case version. Returns a short write receipt. | Uses the same REST request semantics. A stable UUID makes a retry safe and the receipt avoids echoing the comment. |
| `escalate_case` | Requires a case ID, UUID, current case version, reason enum, and bounded details. Returns a short write receipt. | The tool combines the reason and details into the REST message. A required version prevents acting on stale case information. |

The schemas reject unknown properties. Tool descriptions explain when to call each operation and how to retry. Reads are marked read-only. The escalation tool is marked as a potentially destructive write with external effects because an escalation can trigger webhook delivery. API scopes, account grants, case state, and version checks enforce access and write preconditions.
