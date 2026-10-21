"""SQLite store. Prototype: one connection guarded by a lock."""
import json
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
  kind TEXT, idempotency_key TEXT UNIQUE, status TEXT DEFAULT 'sent', created_at REAL);
"""
TABLES = ["settings", "inbound_events", "requests", "events", "action_runs", "outbox"]


class Store:
    def __init__(self, path=":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def q(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def all(self, sql, args=()):
        return [dict(r) for r in self.q(sql, args).fetchall()]

    def one(self, sql, args=()):
        row = self.q(sql, args).fetchone()
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
