# AI usage and verification

I used Swagger UI and pgAdmin to inspect API behavior and database tables. Claude Desktop was the target host for the MCP integration. The automated MCP checks used the MCP Python SDK client.

## How output was verified

| Area | Verification |
| --- | --- |
| OpenAPI contract | Compared the served `/openapi.json` with `openapi.yaml`, checked documented routes against registered routes, and verified that Swagger UI points to the served schema. |
| PostgreSQL | Built the Docker image and ran the API against an isolated PostgreSQL instance. Checked fixture reads, search pagination, comment retries, audit entries, and webhook outbox handling. |
| MCP tools and safeguards | Ran a 20-call baseline eval. Follow-up checks confirmed that declined or unsupported approvals blocked writes, approved writes succeeded, and seeded instructions, PII, and ARR stayed out of MCP results. Checked output sizing and the oversized-result rejection path. |
| Documentation and code changes | Reviewed Git diffs for scope and whitespace, and checked documented commands, file paths, settings, and tool names against project files. |

The automated evals used disposable test data and locally signed JWTs. They verified server behavior through the SDK client. Live identity-provider configuration and Claude Desktop's confirmation UI were outside those checks.

See [TESTEVALS.md](TESTEVALS.md) for the scenarios and results and [DECISIONS.md](DECISIONS.md) for the MCP tool design.

## Claude Desktop configuration

Add this entry inside the existing "mcpServers" object in "claude_desktop_config.json". Adjust the paths if Docker or the repository is installed elsewhere.

```json
"customer-entitlement-support": {
  "command": "C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe",
  "args": [
    "compose",
    "--project-directory",
    "C:\\AI\\KN_Projects\\Project_submit\\customer_entitlement_support",
    "exec", "-T", "api",
    "python", "-m", "customer_entitlement_support.mcp_server"
  ]
}
```
