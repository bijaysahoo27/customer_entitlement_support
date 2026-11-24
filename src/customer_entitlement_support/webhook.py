import asyncio
import logging
import time
from contextlib import closing
from hmac import digest
from pathlib import Path
from sqlite3 import OperationalError

import httpx

from .store import connect

logger = logging.getLogger("uvicorn.error")


def claim(path: Path):
    now = int(time.time())
    with closing(connect(path)) as db, db:
        return db.execute("""
            UPDATE webhook_outbox SET attempts = attempts + 1, next_attempt = ?
            WHERE event_id = (
                SELECT event_id FROM webhook_outbox
                WHERE delivered_at IS NULL AND next_attempt <= ?
                ORDER BY next_attempt, event_id LIMIT 1
            )
            RETURNING event_id, payload, attempts
        """, (now + 60, now)).fetchone()


def finish(path: Path, event_id: str, attempt: int, delivered: bool) -> None:
    now = int(time.time())
    with closing(connect(path)) as db, db:
        db.execute("""
            UPDATE webhook_outbox SET delivered_at = ?, next_attempt = ?
            WHERE event_id = ? AND attempts = ?
        """, (now if delivered else None, now + min(300, 2 ** min(attempt, 9)), event_id, attempt))


async def deliver(path: Path, client: httpx.AsyncClient, url: str, secret: bytes) -> bool:
    event = await asyncio.to_thread(claim, path)
    if event is None:
        return False
    payload = event["payload"]
    timestamp = str(int(time.time()))
    signature = digest(secret, timestamp.encode("ascii") + b"." + payload, "sha256").hex()
    try:
        async with asyncio.timeout(10):
            response = await client.post(url, content=payload, headers={
                "Content-Type": "application/json", "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature": "sha256=" + signature,
                "X-Correlation-ID": event["event_id"],
            })
        delivered = response.is_success
    except (httpx.HTTPError, TimeoutError):
        delivered = False
    await asyncio.to_thread(finish, path, event["event_id"], event["attempts"], delivered)
    return True


async def run_delivery(path: Path, url: str, secret: bytes) -> None:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if await deliver(path, client, url, secret):
                    continue
            except OperationalError:
                logger.warning("Webhook queue is temporarily unavailable")
            await asyncio.sleep(1)
