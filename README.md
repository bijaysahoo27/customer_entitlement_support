# Customer Entitlement & Support Case API

Internal Support, Finance, and Customer Success tools can retrieve customer entitlements, support cases, and product usage through one API. A Support Copilot can use the same OpenAPI contract to search cases and add comments.

## Tech stack

| Layer | Technology |
| --- | --- |
| Runtime and REST API | Python 3.13, FastAPI, Uvicorn, and Pydantic |
| Database | PostgreSQL 17 with Psycopg 3 |
| API contract and documentation | OpenAPI 3.1, PyYAML, and Swagger UI |
| Agent integration | Python MCP SDK over stdio, with Claude Desktop as the host |
| HTTP client | HTTPX for MCP-to-API requests and webhook delivery |
| Authentication | OAuth 2.0 bearer tokens and RS256 JWT validation with PyJWT, plus a local fixture token |
| Local development | Docker Compose, Make, and WSL |
| Python packaging | uv and Hatchling |

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

To start with Docker Compose directly, run `docker compose up --build --wait --wait-timeout 60`. Use `docker compose logs -f api` to follow the logs. Run `make down` to stop the services. PostgreSQL data persists in the `postgres-data` volume. The local token stays in the `support-data` volume.

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

FastAPI serves the [checked-in API contract](openapi.yaml) at `/openapi.json`, and Swagger UI displays it at <http://127.0.0.1:8000/docs>. Use **Authorize** and select **bearerAuth** to paste an access token. Restart the app after editing the contract, or rebuild and restart with `make up` when using Docker Compose.

Access tokens must be RS256-signed JWTs with `iss`, `aud`, `sub`, and `exp` claims. Configure the identity provider to include a space-separated `scope` string and an `account_ids` array of permitted customer IDs. Use interactive authorization code with PKCE and OIDC sign-in, or client credentials for service integrations. Set `SUPPORT_COPILOT_CLIENT_ID` to identify the Copilot client via its token's `client_id` or `azp` claim.

The default quota is 120 requests per principal per minute. Override it with `SUPPORT_RATE_LIMIT`. Set the same high-entropy `SUPPORT_CURSOR_SECRET` across workers and restarts to keep cursors valid. With no configured secret, cursors work within one process lifetime.

To enable webhook delivery, configure `SUPPORT_WEBHOOK_URL` as an HTTPS receiver and set `SUPPORT_WEBHOOK_SECRET` to the shared signing secret. The worker retries previously queued escalation events with the same event ID until an HTTP 2xx acknowledgment is received. Keep the database and its backups access-controlled because queued events contain escalation reasons.

## Fixture data

| System | Local source | Data |
| --- | --- | --- |
| Case system | `cases.json` | Case IDs, account IDs, priorities, status, opening timestamps, and comments |
| Entitlement system | `accounts.csv` and `entitlements.xml` | Account names, USD ARR, support tier, and renewal date. The XML feed supplies the latest tier and renewal. |
| Usage warehouse | `usage.sql` loaded into PostgreSQL | Monthly protected TB, API calls, and active users |

At startup, FastAPI connects to `SUPPORT_DATABASE_URL` and calls `initialize()` in `store.py` to create and seed the PostgreSQL tables. Compose provides a local database with development credentials by default. For a standalone API process, set `SUPPORT_DATABASE_URL` to its PostgreSQL connection URL. Fixture edits take effect when a fresh database is seeded. Existing SQLite data requires a separate import.

The appendix uses `ACC-77` in the CSV and `ACC-007` in the other sources. The mock maps `ACC-77` to `ACC-007`, which is the ID returned by the API. Both IDs work for customer lookup and case filtering. The sample provides a contract end date, so `contract_id` is null. PostgreSQL stores imported comments with their original author and timestamp.

## Data model

PostgreSQL stores comments, their idempotency keys, and audit entries in one transaction. The write transaction locks the case row before evaluating an ETag or deciding whether a request is a replay. A replay returns the persisted action result. Incoming user actions live in `action_requests` and imported comments live in `case_actions`.

| Tables | Keys and indexes |
| --- | --- |
| `accounts`, `cases` | Primary keys use platform IDs. Cases reference accounts and are indexed by `(customer_id, opened_at, case_id)` for account lookups and ordered results. |
| `monthly_usage`, `usage` | `(customer_id, month)` identifies a monthly report and supports a reverse scan for the latest month. The compatibility table uses `customer_id`. |
| `action_requests`, `case_actions` | `(case_id, principal, request_id)` scopes idempotency and indexes new action history by case. Imported actions use `(case_id, request_id)`. |
| `audit_log`, `api_quota` | Audit uses a sequence primary key and a `(case_id, sequence)` index for per-case review. Quota uses one row per principal. |
| `webhook_outbox` | `event_id` identifies a delivery across retries. A partial index on `next_attempt` covers only undelivered rows. |

### Inspect PostgreSQL

While Compose is running, open PostgreSQL from the project directory in WSL.

```bash
docker compose exec db psql -U support -d support
```

At the `psql` prompt, use `\dt` to list tables and `\q` to exit. To see the latest comments added through the API, run:

```sql
SELECT case_id, request_id, actor, message, created_at
FROM action_requests
ORDER BY created_at DESC
LIMIT 5;
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
