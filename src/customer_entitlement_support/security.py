import json
import os
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from hmac import compare_digest, digest
from urllib.parse import urlparse

import jwt
from fastapi import HTTPException

from .store import canonical_account_id


@dataclass(frozen=True)
class Principal:
    identifier: str
    scopes: frozenset[str]
    accounts: frozenset[str]
    actor: str

    def require(self, *scopes: str) -> None:
        if not self.scopes.issuperset(scopes):
            challenge = 'Bearer error="insufficient_scope", scope="' + " ".join(scopes) + '"'
            raise HTTPException(403, "Required scope is missing",
                                headers={"WWW-Authenticate": challenge})


@lru_cache(maxsize=4)
def key_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, timeout=3)


def authenticate(token: str | None) -> Principal:
    unauthorized = HTTPException(401, "A valid access token is required",
                                 headers={"WWW-Authenticate": "Bearer"})
    if not token:
        raise unauthorized
    issuer = os.getenv("SUPPORT_OAUTH_ISSUER")
    audience = os.getenv("SUPPORT_OAUTH_AUDIENCE")
    jwks_url = os.getenv("SUPPORT_OAUTH_JWKS_URL", "")
    if not issuer or not audience or urlparse(jwks_url).scheme != "https":
        raise HTTPException(503, "Authentication provider is unavailable")
    try:
        if jwt.get_unverified_header(token).get("alg") != "RS256":
            raise unauthorized
        key = key_client(jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=audience,
                            issuer=issuer, options={"require": ["exp", "iss", "aud", "sub"]})
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(503, "Authentication provider is unavailable") from None
    except jwt.PyJWTError:
        raise unauthorized from None
    scope = claims.get("scope", "")
    accounts = claims.get("account_ids", [])
    if not isinstance(scope, str) or not isinstance(accounts, list) or not all(
        isinstance(account, str) for account in accounts
    ):
        raise unauthorized
    client = claims.get("client_id", claims.get("azp", ""))
    if not isinstance(client, str) or not claims["sub"]:
        raise unauthorized
    identifier = json.dumps([issuer, claims["sub"], client], separators=(",", ":"))
    actor = "copilot" if client and client == os.getenv("SUPPORT_COPILOT_CLIENT_ID") else "application"
    return Principal(identifier, frozenset(scope.split()),
                     frozenset(map(canonical_account_id, accounts)), actor)


def quota_headers(db, principal: Principal, limit: int) -> dict[str, str]:
    now = int(time.time())
    start = now - now % 60
    with db:
        row = db.execute("""
            INSERT INTO api_quota VALUES (?, ?, 1)
            ON CONFLICT(principal) DO UPDATE SET window_start = excluded.window_start,
              used = CASE WHEN api_quota.window_start = excluded.window_start
                THEN min(api_quota.used + 1, ?) ELSE 1 END
            RETURNING used
        """, (principal.identifier, start, limit + 1)).fetchone()
    reset = start + 60 - now
    headers = {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(max(0, limit - row["used"])),
               "X-RateLimit-Reset": str(reset)}
    if row["used"] > limit:
        raise HTTPException(429, "Request quota exhausted",
                            headers={**headers, "Retry-After": str(reset)})
    return headers


def search_fingerprint(parameters: list) -> str:
    return sha256(json.dumps(parameters, separators=(",", ":")).encode()).hexdigest()


def encode_cursor(secret: bytes, fingerprint: str, as_of: str, value: str, case_id: str) -> str:
    body = json.dumps([fingerprint, as_of, value, case_id], separators=(",", ":")).encode()
    return urlsafe_b64encode(digest(secret, body, "sha256") + body).decode().rstrip("=")


def decode_cursor(secret: bytes, cursor: str, fingerprint: str) -> tuple[str, str, str]:
    try:
        raw = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        signature, body = raw[:32], raw[32:]
        if (urlsafe_b64encode(raw).decode().rstrip("=") != cursor
                or not compare_digest(signature, digest(secret, body, "sha256"))):
            raise ValueError
        expected, as_of, value, case_id = json.loads(body)
        if expected != fingerprint or not all(isinstance(item, str) for item in (as_of, value, case_id)):
            raise ValueError
        return as_of, value, case_id
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(400, "Invalid cursor for this search") from None
