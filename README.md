# Customer Entitlement & Support Case API

Internal Support, Finance, and Customer Success tools can retrieve customer entitlements, support cases, and product usage through one API. A Support Copilot can use the same OpenAPI contract to search cases and add comments.

## Run locally

In a WSL terminal with Git, Docker Compose, and Make available, replace `<repository-url>` with the project’s Git clone URL, then run:

```bash
git clone "<repository-url>" customer_entitlement_support
cd customer_entitlement_support
make up
```

`make up` builds the image, starts the API in the background, and waits up to 60 seconds for it to become healthy. Once it’s healthy, Make prints both URLs and returns control to your terminal.

- [API docs](http://127.0.0.1:8000/docs)
- [Health check](http://127.0.0.1:8000/health)

To start with Docker Compose directly, run `docker compose up --build --wait --wait-timeout 60`. Use `docker compose logs -f api` to follow the logs. Run `make down` to stop the API. The SQLite database persists in the `support-data` Docker volume.

Compose generates a local bearer token on first boot. Retrieve it once startup completes and use it for the request examples or Swagger UI.

```bash
export ACCESS_TOKEN="$(docker compose exec -T api cat /data/local-token)"
curl -fsS -H "Authorization: Bearer $ACCESS_TOKEN" http://127.0.0.1:8000/v1/customers/ACC-007
```

### Authenticate requests

To use an OAuth provider with Compose, replace these example identity settings and start the API from the same WSL shell. Set `SUPPORT_LOCAL_TOKEN_FILE` to an empty value to disable the fixture token.

```bash
export SUPPORT_OAUTH_ISSUER='https://identity.example.com/'
export SUPPORT_OAUTH_AUDIENCE='customer-entitlement-support'
export SUPPORT_OAUTH_JWKS_URL='https://identity.example.com/.well-known/jwks.json'
export SUPPORT_LOCAL_TOKEN_FILE=''
make up
```

Import [openapi.yaml](openapi.yaml) as the platform contract. FastAPI also exposes its generated route documentation at <http://127.0.0.1:8000/docs>. Use **Authorize** to paste an OAuth access token.

Access tokens must be RS256-signed JWTs with `iss`, `aud`, `sub`, and `exp` claims. Configure the identity provider to include a space-separated `scope` string and an `account_ids` array of permitted customer IDs. Use interactive authorization code with PKCE and OIDC sign-in, or client credentials for service integrations. Set `SUPPORT_COPILOT_CLIENT_ID` to identify the Copilot client via its token's `client_id` or `azp` claim.

The default quota is 120 requests per principal per minute. Override it with `SUPPORT_RATE_LIMIT`. Set the same high-entropy `SUPPORT_CURSOR_SECRET` across workers and restarts to keep cursors valid. With no configured secret, cursors work within one process lifetime.

To enable webhook delivery, configure `SUPPORT_WEBHOOK_URL` as an HTTPS receiver and set `SUPPORT_WEBHOOK_SECRET` to the shared signing secret. The worker retries previously queued escalation events with the same event ID until an HTTP 2xx acknowledgment is received. Keep the database and its backups access-controlled because queued events contain escalation reasons.

## Fixture data

| System | Local source | Data |
| --- | --- | --- |
| Case system | `cases.json` | Case IDs, account IDs, priorities, status, opening timestamps, and comments |
| Entitlement system | `accounts.csv` and `entitlements.xml` | Account names, USD ARR, support tier, and renewal date. The XML feed supplies the latest tier and renewal. |
| Usage warehouse | `usage.sql` loaded into SQLite | Monthly protected TB, API calls, and active users |

At startup, FastAPI calls `initialize()` in `store.py`. It creates and seeds `/data/support.sqlite3` inside the container on first use. Later starts reuse the database in the `support-data` volume, so fixture edits only take effect when a fresh database is seeded.

The appendix uses `ACC-77` in the CSV and `ACC-007` in the other sources. The mock maps `ACC-77` to `ACC-007`, which is the ID returned by the API. Both IDs work for customer lookup and case filtering. The sample provides a contract end date, so `contract_id` is null. SQLite stores imported comments with their original author and timestamp.

## Data model

SQLite fits the local fixture dataset and stores comments, their idempotency keys, and audit entries in one transaction. The write transaction serializes changes before evaluating an ETag or deciding whether a request is a replay. A replay returns the persisted action result. Incoming user actions live in `action_requests` and imported comments live in `case_actions`.

| Tables | Keys and indexes |
| --- | --- |
| `accounts`, `cases` | Primary keys use platform IDs. Cases reference accounts and are indexed by `(customer_id, opened_at, case_id)` for account lookups and ordered results. |
| `monthly_usage`, `usage` | `(customer_id, month)` identifies a monthly report and supports a reverse scan for the latest month. The compatibility table uses `customer_id`. |
| `action_requests`, `case_actions` | `(case_id, principal, request_id)` scopes idempotency and indexes new action history by case. Imported actions use `(case_id, request_id)`. |
| `audit_log`, `api_quota` | Audit uses a sequence primary key and a `(case_id, sequence)` index for per-case review. Quota uses one row per principal. |
| `webhook_outbox` | `event_id` identifies a delivery across retries. A partial index on `next_attempt` covers only undelivered rows. |

### Inspect SQLite

While the API is running, use the container’s Python SQLite module to list tables and inspect account rows. Run this from the project directory in WSL.

```bash
docker compose exec -T api python - <<'PY'
import sqlite3
from contextlib import closing

with closing(sqlite3.connect("file:/data/support.sqlite3?mode=ro", uri=True)) as db:
    tables = db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    print("Tables:", ", ".join(name for (name,) in tables))
    for row in db.execute("SELECT customer_id, name, support_tier FROM accounts ORDER BY customer_id"):
        print(*row, sep=" | ")
PY
```

## Traditional apps and iPaaS

Send `Authorization: Bearer <access-token>` with requests. Import the OpenAPI contract into an API client or workflow tool.

| Method and route | Use |
| --- | --- |
| `GET /v1/customers/{id}` | Retrieve entitlement, usage, and case summaries. Requires `customers:read` and `cases:read`. ARR requires the Finance scope. |
| `GET /v1/cases` | Filter and sort cases. Pass `next_cursor` to fetch the next page. Each case includes a `comments_etag`. |
| `POST /v1/cases/{id}/comments` | Add a comment with a client-generated request ID and optional `If-Match`. |

`GET /v1/customers` is a compatibility list route. For the customer overview, set `ACCESS_TOKEN` to a token with `customers:read`, `cases:read`, and access to `ACC-007`.

```bash
curl -fsS -H "Authorization: Bearer $ACCESS_TOKEN" \
  http://127.0.0.1:8000/v1/customers/ACC-007
```

## Support Copilot

Set `ACCESS_TOKEN` to an OAuth access token with the scopes needed by the Copilot. Map the OpenAPI operation IDs to typed agent tools. For “Which Platinum customers have P1 cases open for more than 48 hours and a renewal in the next 90 days?”, call `findCases` with these filters.

```bash
curl -fsS -H "Authorization: Bearer $ACCESS_TOKEN" \
  'http://127.0.0.1:8000/v1/cases?support_tier=Platinum&priority=P1&open_only=true&older_than_hours=48&renewal_within_days=90'
```

Use `addCaseComment` with an explicit case ID, a UUID `request_id`, and a `message` containing the comment. Reuse the same request ID and payload for workflow retries. Each action records the consumer type, and closed cases reject mutations with HTTP 409.

To make a comment conditional on the version you reviewed, copy the case's `comments_etag` from `GET /v1/cases`, including its quotes. A stale tag returns `412`.

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'If-Match: "paste-comments-etag-here"' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"641404b1-6850-4ab8-bd9a-bab255766cb0","message":"Confirmed the incident with the customer."}' \
  http://127.0.0.1:8000/v1/cases/CS-10231/comments
```
