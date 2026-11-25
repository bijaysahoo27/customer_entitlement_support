import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import Priority, Tier

server = Server("customer-entitlement-support")
CaseId = Annotated[str, StringConstraints(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]
ETag = Annotated[str, StringConstraints(pattern=r'^"[a-f0-9]{64}"$')]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchCases(ToolInput):
    customer_id: CaseId | None = None
    support_tier: Tier | None = None
    priority: Priority | None = None
    open_only: bool = False
    older_than_hours: int | None = Field(default=None, ge=0, le=87600)
    renewal_within_days: int | None = Field(default=None, ge=0, le=3650)
    limit: int = Field(default=5, ge=1, le=15)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)


class CustomerSummary(ToolInput):
    customer_id: CaseId


class Comment(ToolInput):
    case_id: CaseId
    request_id: UUID = Field(description="New UUID per action. Reuse on retries with the identical message.")
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    case_version: ETag | None = Field(default=None, description="Use the exact version returned by search_cases.")


class Escalation(ToolInput):
    case_id: CaseId
    request_id: UUID = Field(description="New UUID per escalation. Reuse on retries with the same reason and details.")
    case_version: ETag = Field(description="Use the exact version returned by search_cases.")
    reason: Literal["major_incident", "sla_risk", "customer_impact", "management_review"]
    details: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


TOOLS = {
    "search_cases": (
        SearchCases,
        "Find authorized support cases. Use before writing to verify the case ID and get its version. "
        "open_only includes in-progress cases. For pagination, reuse every filter and the same limit with next_cursor.",
        True, False,
    ),
    "get_customer_summary": (
        CustomerSummary,
        "Get one authorized customer's entitlement tier, renewal, latest usage, and case counts. "
        "Use search_cases for case details and versions.",
        True, False,
    ),
    "add_case_comment": (
        Comment,
        "Append an internal comment to an open case. Use only when the user requests a write. "
        "Confirm the case and message, and reuse request_id for identical retries.",
        False, False,
    ),
    "escalate_case": (
        Escalation,
        "Escalate an open case and queue a webhook that may notify another system. Obtain explicit user approval "
        "for this case and reason before calling. Use a freshly reviewed case_version from search_cases. "
        "If the version changed, re-read and ask for approval again.",
        False, True,
    ),
}


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name=name, description=description, inputSchema=model.model_json_schema(),
            annotations=types.ToolAnnotations(
                readOnlyHint=read_only, destructiveHint=destructive, idempotentHint=not read_only,
                openWorldHint=destructive,
            ),
        )
        for name, (model, description, read_only, destructive) in TOOLS.items()
    ]


def token() -> str:
    value = os.getenv("SUPPORT_MCP_ACCESS_TOKEN")
    if not value:
        path = os.getenv("SUPPORT_LOCAL_TOKEN_FILE")
        if path:
            value = Path(path).read_text(encoding="ascii").strip()
    if not value:
        raise ValueError("Configure an MCP access token or enable the local API token.")
    return value


async def request(method: str, path: str, **options) -> dict:
    try:
        access_token = token()
    except (OSError, ValueError) as exc:
        raise ValueError("The MCP server could not load its API access token.") from exc
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000", timeout=10, trust_env=False,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as client:
        try:
            response = await client.request(method, path, **options)
        except httpx.RequestError as exc:
            raise ValueError("The support API could not be reached. Reuse the same request ID if retrying a write.") from exc
    if not response.is_success:
        hints = {
            400: "Check the cursor, filters, or case version.",
            401: "The API rejected the configured access token.",
            403: "The access token lacks the required scope.",
            404: "The resource does not exist or is outside the caller's account access.",
            409: "Check whether the case is closed, already escalated, or the request ID was reused.",
            412: "The case changed. Search again and review before retrying.",
            422: "The API rejected an input field.",
            429: "The request quota is exhausted. Try again after the quota resets.",
        }
        raise ValueError(f"Support API HTTP {response.status_code}. {hints.get(response.status_code, 'Check API availability. Reuse the same request ID if retrying a write.')}")
    return response.json()


async def execute(arguments: ToolInput) -> dict:
    if isinstance(arguments, SearchCases):
        result = await request("GET", "/v1/cases", params=arguments.model_dump(exclude_none=True))
        fields = ("case_id", "customer_id", "subject", "status", "priority", "opened_at", "renewal_date", "escalated_at")
        cases = [
            {**{key: row[key] for key in fields}, "case_version": row["comments_etag"]}
            for row in result["items"]
        ]
        return {"cases": cases, "next_cursor": result["next_cursor"]}
    if isinstance(arguments, CustomerSummary):
        result = await request("GET", f"/v1/customers/{quote(arguments.customer_id, safe='')}")
        account = result["account"]
        fields = ("customer_id", "name", "support_tier", "renewal_date")
        usage = result["usage"]
        cases = result["cases"]
        return {
            **{key: account[key] for key in fields},
            "usage": {key: value for key, value in usage.items() if key != "customer_id"} if usage else None,
            "case_count": len(cases),
            "open_case_count": sum(case["status"] in ("open", "in_progress") for case in cases),
        }
    escalation = isinstance(arguments, Escalation)
    resource = "escalations" if escalation else "comments"
    message = f"{arguments.reason}: {arguments.details}" if escalation else arguments.message
    result = await request(
        "POST", f"/v1/cases/{quote(arguments.case_id, safe='')}/{resource}",
        json={"request_id": str(arguments.request_id), "message": message},
        headers={"If-Match": arguments.case_version} if arguments.case_version else {},
    )
    return {key: result[key] for key in ("case_id", "request_id", "kind", "created_at")}


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    model = TOOLS[name][0]
    result = await execute(model.model_validate(arguments))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result, separators=(",", ":")))],
    )


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
