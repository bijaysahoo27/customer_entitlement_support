import asyncio
import json
import os
import re
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from functools import cache
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
from secrets import compare_digest, token_bytes, token_urlsafe
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import Connection, OperationalError
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.exceptions import HTTPException as StarletteHTTPException

from .security import (Principal, authenticate, decode_cursor, encode_cursor,
                       quota_headers, search_fingerprint)
from .store import canonical_account_id, connect, initialize
from .webhook import run_delivery


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.database = os.environ["SUPPORT_DATABASE_URL"]
    initialize(app.state.database)
    app.state.local_token = None
    token_file = os.getenv("SUPPORT_LOCAL_TOKEN_FILE")
    if token_file:
        path = Path(token_file)
        try:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(token_urlsafe(32))
        except FileExistsError:
            pass
        app.state.local_token = path.read_text(encoding="ascii").strip()
        if len(app.state.local_token) < 32:
            raise ValueError("Local token must contain at least 32 characters")
    cursor_secret = os.getenv("SUPPORT_CURSOR_SECRET")
    app.state.cursor_secret = cursor_secret.encode() if cursor_secret else token_bytes(32)
    app.state.quota = int(os.getenv("SUPPORT_RATE_LIMIT", "120"))
    if app.state.quota < 1:
        raise ValueError("SUPPORT_RATE_LIMIT must be positive")
    url = os.getenv("SUPPORT_WEBHOOK_URL", "")
    secret = os.getenv("SUPPORT_WEBHOOK_SECRET", "")
    destination = urlparse(url)
    if (url or secret) and (destination.scheme != "https" or not destination.hostname
                            or destination.username is not None or not secret):
        raise ValueError("Configure an HTTPS webhook URL and signing secret together")
    task = asyncio.create_task(run_delivery(app.state.database, url, secret.encode())) if url else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Customer Entitlement & Support Case API", version="1.0.0",
              lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


@cache
def load_openapi_contract() -> dict:
    package_dir = Path(__file__).resolve().parent
    contract = package_dir / "openapi.yaml"
    if not contract.is_file():
        contract = package_dir.parents[1] / "openapi.yaml"
    return yaml.safe_load(contract.read_text(encoding="utf-8"))


app.openapi = load_openapi_contract


def problem(request: Request, code: int, detail: str, headers=None, **extensions) -> JSONResponse:
    title = HTTPStatus(code).phrase
    return JSONResponse({"type": f"/problems/{code}", "title": title, "status": code,
                         "detail": detail, "request_id": request.state.correlation_id, **extensions},
                        status_code=code, headers={**getattr(request.state, "quota_headers", {}),
                                                   **(headers or {})},
                        media_type="application/problem+json")


@app.middleware("http")
async def correlate(request: Request, call_next):
    supplied = request.headers.get("X-Correlation-ID", "")
    correlation_id = supplied if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", supplied) else str(uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


@app.exception_handler(StarletteHTTPException)
async def http_problem(request: Request, exception: StarletteHTTPException):
    return problem(request, exception.status_code, str(exception.detail), exception.headers)


@app.exception_handler(RequestValidationError)
async def validation_problem(request: Request, exception: RequestValidationError):
    errors = exception.errors()
    if any(error["type"] == "json_invalid" for error in errors):
        return problem(request, 400, "Malformed JSON request")
    issues = []
    for error in errors:
        source, *parts = error["loc"]
        field = str(parts[0]) if parts else source
        if source == "body":
            field = "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)
        issues.append({"field": field, "code": error["type"], "message": error["msg"]})
    return problem(request, 422, "Request validation failed", errors=issues)


@app.exception_handler(OperationalError)
async def data_problem(request: Request, exception: OperationalError):
    return problem(request, 503, "Support data is temporarily unavailable")


def database(request: Request) -> Iterator[Connection]:
    with connect(request.app.state.database) as db:
        yield db


Database = Annotated[Connection, Depends(database)]


def consumer(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
             request: Request, response: Response, db: Database) -> Principal:
    token = credentials.credentials if credentials else None
    local_token = request.app.state.local_token
    if local_token and token and compare_digest(token.encode(), local_token.encode()):
        scopes = {
            "customers:read", "cases:read", "cases:comment",
        }
        if os.getenv("SUPPORT_LOCAL_ESCALATION") == "1":
            scopes.add("cases:escalate")
        principal = Principal("local-fixture", frozenset(scopes), frozenset({"ACC-007"}), "application")
    else:
        if local_token and not os.getenv("SUPPORT_OAUTH_ISSUER"):
            token = None
        principal = authenticate(token)
    request.state.quota_headers = quota_headers(db, principal, request.app.state.quota)
    response.headers.update(request.state.quota_headers)
    return principal


Consumer = Annotated[Principal, Depends(consumer)]
Priority = Literal["P1", "P2", "P3", "P4"]
Tier = Literal["Platinum", "Gold", "Silver", "Standard"]


class Account(BaseModel):
    customer_id: str
    name: str
    contract_id: str | None
    support_tier: Tier
    renewal_date: str
    arr: int | None = None
    arr_currency: str | None = None


class Case(BaseModel):
    case_id: str
    customer_id: str
    subject: str
    status: Literal["open", "in_progress", "closed"]
    priority: Priority
    opened_at: str
    escalated_at: str | None


class CaseAction(BaseModel):
    case_id: str
    request_id: UUID
    actor: str
    message: str
    created_at: str


class CaseComment(CaseAction):
    kind: Literal["comment"]


class CaseEscalation(CaseAction):
    kind: Literal["escalation"]


class MonthlyUsage(BaseModel):
    customer_id: str
    month: str
    protected_tb: float
    active_users: int
    api_calls: int


class Usage(BaseModel):
    customer_id: str
    window_days: int
    active_users: int
    api_calls: int


class CustomerOverview(BaseModel):
    account: Account
    usage: MonthlyUsage | Usage | None
    cases: list[Case]


class CaseMatch(Case):
    customer_name: str
    support_tier: Tier
    renewal_date: str
    comments_etag: str


class CasePage(BaseModel):
    items: list[CaseMatch]
    next_cursor: str | None


CaseSort = Literal["opened_at", "-opened_at", "priority", "-priority", "renewal_date", "-renewal_date"]


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def account_view(row, actor: Principal) -> dict:
    result = dict(row)
    if "finance:arr:read" not in actor.scopes:
        del result["arr"]
        del result["arr_currency"]
    return result


@app.get("/v1/customers", operation_id="listCustomers", include_in_schema=False,
         response_model_exclude_unset=True)
def list_customers(db: Database, actor: Consumer,
                   limit: Annotated[int, Query(ge=1, le=100)] = 50,
                   offset: Annotated[int, Query(ge=0)] = 0) -> list[Account]:
    actor.require("customers:read")
    grants = sorted(actor.accounts)
    return [account_view(row, actor) for row in db.execute(
        """SELECT * FROM accounts WHERE customer_id = ANY(%s)
           ORDER BY customer_id LIMIT %s OFFSET %s""", (grants, limit, offset)
    )]


@app.get("/v1/customers/{id}", operation_id="getCustomerOverview", response_model_exclude_unset=True)
def customer_overview(id: str, db: Database, actor: Consumer, response: Response) -> CustomerOverview:
    actor.require("customers:read", "cases:read")
    customer_id = canonical_account_id(id)
    if customer_id not in actor.accounts:
        raise HTTPException(404, "Customer not found")
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        account = db.execute("SELECT * FROM accounts WHERE customer_id = %s", (customer_id,)).fetchone()
        if account is None:
            raise HTTPException(404, "Customer not found")
        cases = [dict(row) for row in db.execute("""
            SELECT * FROM cases WHERE customer_id = %s ORDER BY opened_at, case_id
        """, (customer_id,))]
        usage = db.execute("""
            SELECT * FROM monthly_usage WHERE customer_id = %s ORDER BY month DESC LIMIT 1
        """, (customer_id,)).fetchone()
        if usage is None:
            usage = db.execute("SELECT * FROM usage WHERE customer_id = %s", (customer_id,)).fetchone()
    response.headers["Cache-Control"] = "private, no-store"
    return {"account": account_view(account, actor), "usage": dict(usage) if usage else None,
            "cases": cases}


@app.get("/v1/cases", operation_id="findCases")
def find_cases(
    db: Database, actor: Consumer, request: Request,
    customer_id: str | None = None, support_tier: Tier | None = None,
    priority: Priority | None = None, open_only: bool = False,
    older_than_hours: Annotated[int | None, Query(ge=0, le=87600)] = None,
    renewal_within_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    sort: CaseSort = "opened_at",
    cursor: Annotated[str | None, Query(min_length=1)] = None,
) -> CasePage:
    """Find cases by case and entitlement fields. Open includes in-progress cases."""
    actor.require("cases:read")
    grants = sorted(actor.accounts)
    fingerprint = search_fingerprint([actor.identifier, grants, customer_id, support_tier, priority,
                                      open_only, older_than_hours, renewal_within_days, sort, limit])
    secret = request.app.state.cursor_secret
    anchor = decode_cursor(secret, cursor, fingerprint) if cursor is not None else None
    now = datetime.fromisoformat(anchor[0]) if anchor else datetime.now(timezone.utc)
    field = sort.lstrip("-")
    column = {"opened_at": "c.opened_at", "priority": "c.priority", "renewal_date": "a.renewal_date"}[field]
    descending = sort.startswith("-")
    direction, comparison = ("DESC", "<") if descending else ("ASC", ">")
    conditions = ["c.customer_id = ANY(%s)"]
    params = [grants]
    for predicate, value in (
        ("c.customer_id = %s", canonical_account_id(customer_id) if customer_id is not None else None),
        ("a.support_tier = %s", support_tier),
        ("c.priority = %s", priority),
    ):
        if value is not None:
            conditions.append(predicate)
            params.append(value)
    if open_only:
        conditions.append("c.status IN ('open', 'in_progress')")
    if older_than_hours is not None:
        conditions.append("c.opened_at < %s")
        params.append((now - timedelta(hours=older_than_hours)).isoformat())
    if renewal_within_days is not None:
        conditions.append("a.renewal_date BETWEEN %s AND %s")
        params.extend([now.date().isoformat(),
                       (now.date() + timedelta(days=renewal_within_days)).isoformat()])
    if anchor:
        _, value, case_id = anchor
        conditions.append(f"({column} {comparison} %s OR ({column} = %s AND c.case_id > %s))")
        params.extend([value, value, case_id])
    where = " AND ".join(conditions)
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        rows = [dict(row) for row in db.execute(f"""
            SELECT c.*, a.name AS customer_name, a.support_tier, a.renewal_date
            FROM cases c JOIN accounts a USING (customer_id)
            WHERE {where}
            ORDER BY {column} {direction}, c.case_id ASC LIMIT %s
        """, [*params, limit + 1])]
        items = rows[:limit]
        for item in items:
            case = {key: item[key] for key in Case.model_fields}
            item["comments_etag"] = case_etag(db, case)
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = encode_cursor(secret, fingerprint, now.isoformat(), last[field], last["case_id"])
    return {"items": items, "next_cursor": next_cursor}


def require_case(db: Connection, case_id: str, actor: Principal):
    case = db.execute("SELECT * FROM cases WHERE case_id = %s FOR UPDATE", (case_id,)).fetchone()
    if case is None or case["customer_id"] not in actor.accounts:
        raise HTTPException(404, "Case not found")
    return case


def case_etag(db: Connection, case) -> str:
    actions = db.execute("""
        SELECT case_id, request_id, kind, actor, message, created_at
        FROM case_actions WHERE case_id = %s
        UNION ALL
        SELECT case_id, request_id, kind, actor, message, created_at
        FROM action_requests WHERE case_id = %s
        ORDER BY created_at, request_id, actor, kind, message
    """, (case["case_id"], case["case_id"]))
    version = {**dict(case), "actions": [dict(row) for row in actions]}
    encoded = json.dumps(version, sort_keys=True, separators=(",", ":")).encode()
    return '"' + sha256(encoded).hexdigest() + '"'


def record_action(db: Connection, case_id: str, payload: ActionRequest, actor: Principal,
                  if_match: str | None, kind: Literal["comment", "escalation"]) -> dict:
    request_id = str(payload.request_id)
    with db.transaction():
        case = require_case(db, case_id, actor)
        if if_match is not None and not re.fullmatch(r'"[!#-~]*"', if_match, flags=re.ASCII):
            raise HTTPException(400, "If-Match must contain one strong ETag")
        previous = db.execute("""
            SELECT case_id, request_id, kind, actor, message, created_at
            FROM action_requests WHERE case_id = %s AND principal = %s AND request_id = %s
        """, (case_id, actor.identifier, request_id)).fetchone()
        if previous:
            if (previous["kind"], previous["message"]) != (kind, payload.message):
                raise HTTPException(409, "Request ID already used for a different action")
            return dict(previous)
        if db.execute("SELECT 1 FROM case_actions WHERE case_id = %s AND request_id = %s",
                      (case_id, request_id)).fetchone():
            raise HTTPException(409, "Request ID belongs to a legacy case action")
        if if_match is not None and if_match != case_etag(db, case):
            raise HTTPException(412, "If-Match does not match the current case version")
        if case["status"] == "closed":
            raise HTTPException(409, "Case is closed")
        if kind == "escalation" and case["escalated_at"] is not None:
            raise HTTPException(409, "Case is already escalated")
        now = datetime.now(timezone.utc).isoformat()
        action = {"case_id": case_id, "request_id": request_id, "kind": kind, "actor": actor.actor,
                  "message": payload.message, "created_at": now}
        db.execute("""
            INSERT INTO action_requests (case_id, principal, request_id, kind, actor, message, created_at)
            VALUES (%(case_id)s, %(principal)s, %(request_id)s, %(kind)s,
                %(actor)s, %(message)s, %(created_at)s)
        """, {**action, "principal": actor.identifier})
        db.execute("""
            INSERT INTO audit_log (principal, case_id, action, outcome, request_id, created_at)
            VALUES (%s, %s, %s, 'committed', %s, %s)
        """, (actor.identifier, case_id, action["kind"], request_id, now))
        if kind == "escalation":
            db.execute("UPDATE cases SET escalated_at = %s WHERE case_id = %s", (now, case_id))
            event_id = str(uuid4())
            event = {"event_id": event_id, "type": "case.escalated", "occurred_at": now, "data": action}
            db.execute("""
                INSERT INTO webhook_outbox (event_id, payload, next_attempt) VALUES (%s, %s, 0)
            """, (event_id, json.dumps(event).encode("utf-8")))
        return action


@app.post("/v1/cases/{id}/comments", status_code=status.HTTP_201_CREATED,
          operation_id="addCaseComment")
def add_comment(id: str, payload: ActionRequest, db: Database, actor: Consumer,
                if_match: Annotated[list[str] | None, Header()] = None) -> CaseComment:
    """Append a comment to an open case. Reuse request_id when retrying."""
    actor.require("cases:comment")
    tag = ",".join(if_match) if if_match else None
    return record_action(db, id, payload, actor, tag, "comment")


@app.post("/v1/cases/{id}/escalations", status_code=status.HTTP_201_CREATED,
          operation_id="escalateCase")
def escalate_case(id: str, payload: ActionRequest, db: Database, actor: Consumer,
                  if_match: Annotated[list[str], Header()]) -> CaseEscalation:
    """Escalate an open case using its current version and queue a webhook event."""
    actor.require("cases:escalate")
    return record_action(db, id, payload, actor, ",".join(if_match), "escalation")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, access_log=False)
