# Security governance and guardrails

This document covers the FastAPI service, the Support Copilot MCP server, PostgreSQL, and the escalation webhook. The repository's Docker Compose setup is intended for local development with fixture data.

## Identity and access

- The MCP server forwards the bearer token configured in `SUPPORT_MCP_ACCESS_TOKEN`. When that setting is empty, it can read the configured local fixture token. FastAPI validates OAuth tokens against the configured issuer, audience, expiry, subject, and RS256 signing key. It uses the token's scopes and `account_ids` to enforce access to customer records.
- Provision a short-lived delegated user token per MCP session in deployments that handle real data. Grant only the scopes and accounts the user needs. The current stdio server uses the supplied credential and does not bind it to the Claude Desktop sign-in or require a delegated user grant. A scoped service token can also authenticate.
- Set `SUPPORT_LOCAL_TOKEN_FILE` to an empty value outside local fixtures and leave `SUPPORT_LOCAL_ESCALATION` disabled. Use deployment-managed secrets for API tokens, database credentials, the cursor secret, and webhook signing. Keep them out of Git, prompts, screenshots, and shared logs.
- The provided Compose ports bind to localhost. For shared deployments, keep PostgreSQL on a private network, use TLS for network connections, and replace the development database credentials with a role scoped to the application database.

| Operation | Required scopes |
| --- | --- |
| Search cases | `cases:read` |
| Get a customer summary | `customers:read` and `cases:read` |
| Add a comment | `cases:comment` |
| Escalate a case | `cases:escalate` |
| Read ARR through REST | `finance:arr:read` in addition to the route's scopes |

See [TESTEVALS.md](TESTEVALS.md) for the exercised scenarios and [DECISIONS.md](DECISIONS.md) for tool design.
