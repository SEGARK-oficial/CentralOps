#!/usr/bin/env python3
"""Database migration script for XDR Query migration.

Run this once to add new columns to the existing database without
losing data.  Safe to re-run — it checks for column existence first.

Usage:
    python scripts/migrate_db.py          # uses default path
    python scripts/migrate_db.py /path/to/sophos.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from uuid import uuid4


def get_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def migrate(db_path: str) -> None:
    print(f"Migrating database: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # ── 1. Add new columns to search_results ─────────────────────────
    if table_exists(cur, "search_results"):
        existing = get_columns(cur, "search_results")

        if "engine" not in existing:
            print("  Adding column search_results.engine")
            cur.execute("ALTER TABLE search_results ADD COLUMN engine TEXT NOT NULL DEFAULT 'search'")

        if "language" not in existing:
            print("  Adding column search_results.language")
            cur.execute("ALTER TABLE search_results ADD COLUMN language TEXT NOT NULL DEFAULT 'lucene'")

        if "error_message" not in existing:
            print("  Adding column search_results.error_message")
            cur.execute("ALTER TABLE search_results ADD COLUMN error_message TEXT")

        if "created_at" not in existing:
            print("  Adding column search_results.created_at")
            cur.execute("ALTER TABLE search_results ADD COLUMN created_at DATETIME")
    else:
        print("  Table search_results does not exist (will be created on first run)")

    # ── 2. Create action_runs table ───────────────────────────────────
    if not table_exists(cur, "action_runs"):
        print("  Creating table action_runs")
        cur.execute("""
            CREATE TABLE action_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                client_ids TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result_summary TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX ix_action_runs_id ON action_runs (id)")
    else:
        print("  Table action_runs already exists")

    # ── 3. Update predefined_queries default table ────────────────────
    # Change default from xdr_index to xdr_data for new queries.
    # Existing queries keep their current table value.

    # ── 4. Create application auth tables ─────────────────────────────
    if not table_exists(cur, "app_users"):
        print("  Creating table app_users")
        cur.execute("""
            CREATE TABLE app_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_active BOOLEAN NOT NULL DEFAULT 1,
                last_login_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX ix_app_users_id ON app_users (id)")
        cur.execute("CREATE INDEX ix_app_users_username ON app_users (username)")
        cur.execute("CREATE UNIQUE INDEX ix_app_users_uuid ON app_users (uuid)")
    else:
        print("  Table app_users already exists")
        existing = get_columns(cur, "app_users")
        if "uuid" not in existing:
            print("  Adding column app_users.uuid")
            cur.execute("ALTER TABLE app_users ADD COLUMN uuid TEXT")
        cur.execute("SELECT id FROM app_users WHERE uuid IS NULL OR TRIM(uuid) = ''")
        for (user_id,) in cur.fetchall():
            cur.execute(
                "UPDATE app_users SET uuid = ? WHERE id = ?",
                (str(uuid4()), user_id),
            )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_app_users_uuid ON app_users (uuid)")

    if not table_exists(cur, "user_sessions"):
        print("  Creating table user_sessions")
        cur.execute("""
            CREATE TABLE user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                user_agent TEXT,
                expires_at DATETIME NOT NULL,
                revoked_at DATETIME,
                last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES app_users(id)
            )
        """)
        cur.execute("CREATE INDEX ix_user_sessions_id ON user_sessions (id)")
        cur.execute("CREATE INDEX ix_user_sessions_user_id ON user_sessions (user_id)")
        cur.execute("CREATE INDEX ix_user_sessions_token_hash ON user_sessions (token_hash)")
    else:
        print("  Table user_sessions already exists")

    if not table_exists(cur, "audit_logs"):
        print("  Creating table audit_logs")
        cur.execute("""
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                user_role TEXT,
                action TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                method TEXT,
                status_code INTEGER,
                ip_address TEXT,
                user_agent TEXT,
                request_payload TEXT,
                detail TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES app_users(id)
            )
        """)
        cur.execute("CREATE INDEX ix_audit_logs_id ON audit_logs (id)")
        cur.execute("CREATE INDEX ix_audit_logs_user_id ON audit_logs (user_id)")
    else:
        print("  Table audit_logs already exists")
        existing = get_columns(cur, "audit_logs")
        if "request_payload" not in existing:
            print("  Adding column audit_logs.request_payload")
            cur.execute("ALTER TABLE audit_logs ADD COLUMN request_payload TEXT")

    if table_exists(cur, "history"):
        existing = get_columns(cur, "history")
        if "user_id" not in existing:
            print("  Adding column history.user_id")
            cur.execute("ALTER TABLE history ADD COLUMN user_id INTEGER")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_history_user_id ON history (user_id)")

    if table_exists(cur, "search_results"):
        existing = get_columns(cur, "search_results")
        if "user_id" not in existing:
            print("  Adding column search_results.user_id")
            cur.execute("ALTER TABLE search_results ADD COLUMN user_id INTEGER")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_search_results_user_id ON search_results (user_id)")

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        # Default paths to check
        candidates = [
            Path("data/sophos.db"),
            Path("backend/data/sophos.db"),
            Path("/app/data/sophos.db"),
        ]
        path = None
        for p in candidates:
            if p.exists():
                path = str(p)
                break
        if not path:
            print("No database found. Checked:", [str(p) for p in candidates])
            print("Pass the database path as argument: python scripts/migrate_db.py /path/to/sophos.db")
            sys.exit(1)

    migrate(path)
