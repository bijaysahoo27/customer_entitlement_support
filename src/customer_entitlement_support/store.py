import csv
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree

FIXTURES = Path(__file__).with_name("fixtures")
ACCOUNT_ALIASES = {"ACC-77": "ACC-007"}


def canonical_account_id(account_id: str) -> str:
    return ACCOUNT_ALIASES.get(account_id, account_id)


def utc_timestamp(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False, timeout=0.25)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(path: Path) -> None:
    with closing(connect(path)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                customer_id TEXT PRIMARY KEY, name TEXT, contract_id TEXT,
                support_tier TEXT, renewal_date TEXT, arr INTEGER, arr_currency TEXT
            );
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY, customer_id TEXT REFERENCES accounts,
                subject TEXT, status TEXT, priority TEXT, opened_at TEXT, escalated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS usage (
                customer_id TEXT PRIMARY KEY REFERENCES accounts,
                window_days INTEGER, active_users INTEGER, api_calls INTEGER
            );
            CREATE TABLE IF NOT EXISTS case_actions (
                case_id TEXT REFERENCES cases, request_id TEXT, kind TEXT,
                actor TEXT, message TEXT, created_at TEXT,
                PRIMARY KEY (case_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS monthly_usage (
                customer_id TEXT REFERENCES accounts, month TEXT,
                protected_tb REAL, api_calls INTEGER, active_users INTEGER,
                PRIMARY KEY (customer_id, month)
            );
            CREATE TABLE IF NOT EXISTS action_requests (
                case_id TEXT NOT NULL REFERENCES cases, principal TEXT NOT NULL,
                request_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
                message TEXT NOT NULL, created_at TEXT NOT NULL, etag TEXT,
                PRIMARY KEY (case_id, principal, request_id)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                sequence INTEGER PRIMARY KEY, principal TEXT NOT NULL,
                case_id TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL,
                request_id TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS webhook_outbox (
                event_id TEXT PRIMARY KEY, payload BLOB NOT NULL, next_attempt INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS webhook_pending
                ON webhook_outbox (next_attempt) WHERE delivered_at IS NULL;
            CREATE INDEX IF NOT EXISTS cases_customer_opened
                ON cases (customer_id, opened_at, case_id);
            CREATE INDEX IF NOT EXISTS audit_case_sequence
                ON audit_log (case_id, sequence);
            CREATE TABLE IF NOT EXISTS api_quota (
                principal TEXT PRIMARY KEY, window_start INTEGER NOT NULL, used INTEGER NOT NULL
            );
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
                return
            with (FIXTURES / "accounts.csv").open(encoding="utf-8", newline="") as source:
                for account in csv.DictReader(source):
                    db.execute("""
                        INSERT INTO accounts VALUES (:customer_id, :name, NULL,
                            :support_tier, :contract_end, :arr_usd, 'USD')
                    """, {**account, "customer_id": canonical_account_id(account["account_id"])})
            feed = ElementTree.parse(FIXTURES / "entitlements.xml").getroot()
            updated = db.execute("""
                UPDATE accounts SET support_tier = ?, renewal_date = ? WHERE customer_id = ?
            """, (feed.findtext("tier"), feed.findtext("renewal"),
                  canonical_account_id(feed.attrib["id"])))
            if updated.rowcount != 1:
                raise ValueError(f"Entitlement feed references unknown account {feed.attrib['id']}")
            for case in json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8")):
                db.execute("""
                    INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, NULL)
                """, (case["id"], canonical_account_id(case["accountId"]), case["subject"],
                      case["Status"], case["priority"], utc_timestamp(case["openedAt"])))
                for index, comment in enumerate(case["Comments"]):
                    request_id = str(uuid5(NAMESPACE_URL, f"mock-case:{case['id']}:comment:{index}"))
                    db.execute("""
                        INSERT INTO case_actions VALUES (?, ?, 'comment', ?, ?, ?)
                    """, (case["id"], request_id, comment["author"], comment["body"],
                          utc_timestamp(comment["at"])))
            db.execute((FIXTURES / "usage.sql").read_text(encoding="utf-8"))
