"""SQLite store. Prototype: one connection guarded by a lock."""
import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS inbound_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, source_event_id TEXT NOT NULL,
  received_at REAL NOT NULL, outcome TEXT, UNIQUE(channel, source_event_id));
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, type_code TEXT NOT NULL, employee_id TEXT NOT NULL, channel TEXT NOT NULL,
  source_event_id TEXT, raw_text TEXT, summary TEXT, details_json TEXT DEFAULT '{}', confidence REAL, provider TEXT,
  status TEXT NOT NULL, chain_json TEXT DEFAULT '[]', chain_index INTEGER DEFAULT 0, escalated_to TEXT,
  escalation_level INTEGER DEFAULT 0, escalated_at REAL, fulfiller_id TEXT, rejection_reason TEXT,
  created_at REAL NOT NULL, assigned_at REAL, last_reminder_at REAL, decided_at REAL, closed_at REAL, flags TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, ts REAL NOT NULL, actor TEXT, type TEXT NOT NULL,
  data_json TEXT DEFAULT '{}');
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS action_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, step TEXT, idempotency_key TEXT UNIQUE,
  attempts INTEGER DEFAULT 0, status TEXT, last_error TEXT, result_json TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, channel TEXT, to_person TEXT, subject TEXT, body TEXT,
  kind TEXT, idempotency_key TEXT UNIQUE, status TEXT DEFAULT 'sent', created_at REAL,
  delivery_status TEXT DEFAULT 'skipped', delivery_attempts INTEGER DEFAULT 0, delivery_error TEXT, delivery_next_at REAL,
  delivered_at REAL, delivery_note TEXT, from_person TEXT, read_at REAL, sent_to TEXT);
CREATE INDEX IF NOT EXISTS outbox_person ON outbox(to_person, id);
CREATE TABLE IF NOT EXISTS connections(
  tenant TEXT NOT NULL, app TEXT NOT NULL, auth_id TEXT, script_id TEXT, settings_json TEXT DEFAULT '{}', updated_at REAL,
  PRIMARY KEY(tenant, app));
CREATE TABLE IF NOT EXISTS delivery_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, ikey TEXT UNIQUE, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS roles(
  code TEXT PRIMARY KEY, name TEXT NOT NULL, holder_id TEXT, builtin INTEGER DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS type_rules(
  code TEXT PRIMARY KEY, rules_json TEXT NOT NULL DEFAULT '{}', reviewed_at REAL, updated_at REAL, updated_by TEXT);
CREATE TABLE IF NOT EXISTS policies(
  key TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL, updated_at REAL);
CREATE TABLE IF NOT EXISTS notification_prefs(
  person_id TEXT NOT NULL, category TEXT NOT NULL, slack INTEGER NOT NULL DEFAULT 1, email INTEGER NOT NULL DEFAULT 1, updated_at REAL, PRIMARY KEY(person_id, category));
CREATE TABLE IF NOT EXISTS slack_decisions(
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, request_id INTEGER, person_id TEXT, outbox_id INTEGER, created_at REAL NOT NULL, expires_at REAL NOT NULL,
  used_at REAL, outcome TEXT, clicked_by TEXT);
CREATE INDEX IF NOT EXISTS slack_decisions_outbox ON slack_decisions(outbox_id);
CREATE TABLE IF NOT EXISTS leave_types(
  code TEXT PRIMARY KEY, name TEXT NOT NULL, yearly_days REAL NOT NULL);
CREATE TABLE IF NOT EXISTS leave_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL, leave_type TEXT NOT NULL, year INTEGER NOT NULL, days REAL NOT NULL,
  kind TEXT NOT NULL, request_id INTEGER, ref TEXT UNIQUE, group_ref TEXT, dates_json TEXT DEFAULT '[]', note TEXT, actor TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS leave_ledger_person ON leave_ledger(person_id, leave_type, year);
CREATE INDEX IF NOT EXISTS leave_ledger_group ON leave_ledger(group_ref);
CREATE TRIGGER IF NOT EXISTS leave_ledger_no_update BEFORE UPDATE ON leave_ledger
  BEGIN SELECT RAISE(ABORT, 'leave records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS leave_ledger_no_delete BEFORE DELETE ON leave_ledger
  BEGIN SELECT RAISE(ABORT, 'leave records are append-only'); END;
"""
OUTBOX_MIGRATION = {"delivery_status": "TEXT DEFAULT 'skipped'", "delivery_attempts": "INTEGER DEFAULT 0", "delivery_error": "TEXT",
                    "delivery_next_at": "REAL", "delivered_at": "REAL", "delivery_note": "TEXT", "from_person": "TEXT", "read_at": "REAL", "sent_to": "TEXT"}
# "connections", "delivery_ledger", "roles", "type_rules", "policies", "leave_types", "leave_ledger", "slack_decisions" and "notification_prefs" are deliberately NOT here: a demo reset must not disconnect apps, reset the daily live-delivery cap or undo company setup
TABLES = ["settings", "inbound_events", "requests", "events", "action_runs", "outbox"]


class Store:
    def __init__(self, path=":memory:", schema=None):
        """`schema` defaults to a company database. The platform database passes its own and has no company migrations."""
        self.path = path
        self.custom_schema = schema
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(schema or SCHEMA)
        if not schema:
            self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Databases made before delivery existed get the new outbox columns."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(outbox)")}
        for col, decl in OUTBOX_MIGRATION.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE outbox ADD COLUMN {col} {decl}")
        if "read_at" not in have:  # messages from before notifications existed are not "new": a company must not open to hundreds of unread
            self.conn.execute("UPDATE outbox SET read_at=created_at WHERE read_at IS NULL")
            self.conn.execute("CREATE INDEX IF NOT EXISTS outbox_person ON outbox(to_person, id)")

    def q(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def all(self, sql, args=()):
        with self.lock:  # run and read under ONE lock: no other thread may execute or commit on this shared connection while a cursor is half-read
            cur = self.conn.execute(sql, args)
            rows = cur.fetchall()
            self.conn.commit()
            return [dict(r) for r in rows]

    def one(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            row = cur.fetchone()
            cur.close()
            self.conn.commit()
            return dict(row) if row else None

    def get_setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, str(value)))

    def reset(self):
        with self.lock:
            for t in TABLES:
                self.conn.execute(f"DROP TABLE IF EXISTS {t}")
            self.conn.commit()
            self.conn.executescript(SCHEMA)
            self.conn.commit()


def dumps(o):
    return json.dumps(o, ensure_ascii=False, default=str)
