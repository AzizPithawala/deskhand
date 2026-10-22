"""Deskhand's own leave records. Every change to anyone's leave is one line in an append-only ledger (database triggers forbid editing or deleting it):
the yearly allowance (grant), leave taken (use) and an admin's correction (adjust), each with who did it and why. A balance is the sum of a person's lines for the year.
`LedgerHR` offers the same few methods the engine already calls on the demo's MockHR, so retries, read-before-write and parking are reused: a leave is keyed by
`deskhand-<request id>` and can never be deducted twice. Counting rules (kept deliberately simple): Monday to Friday only, a half day is 0.5, the calendar year is the leave year."""
import json
import sqlite3
import time
import uuid
from datetime import date, timedelta

from .adapters import ActionError

DEFAULT_TYPES = [("casual", "Casual leave", 12), ("sick", "Sick leave", 8)]
MAX_RANGE_DAYS = 120  # a range longer than this is not expanded: it is almost certainly a mistake
MAX_YEARLY_DAYS = 366


def num(x):
    """12.0 -> 12, 1.5 -> 1.5: people read days, not floats."""
    x = round(float(x), 2)
    return int(x) if x == int(x) else x


def _date(v):
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def leave_dates(dates, date_range=False):
    """The calendar days a request covers: every day between two dates when it is a range, else exactly the days listed."""
    days = sorted({d for d in (_date(v) for v in (dates or [])) if d})
    if date_range and len(days) >= 2:
        span = (days[-1] - days[0]).days
        if span > MAX_RANGE_DAYS:
            raise ValueError(f"a range of more than {MAX_RANGE_DAYS} days")
        return [days[0] + timedelta(i) for i in range(span + 1)]
    return days


def working_days(dates, date_range=False, half_day=False):
    """[(day, weight)], Monday to Friday only. A half day is one day counted as 0.5."""
    try:
        days = [d for d in leave_dates(dates, date_range) if d.weekday() < 5]
    except ValueError:
        return []
    if half_day:
        return [(days[0], 0.5)] if days else []
    return [(d, 1.0) for d in days]


class Ledger:
    def __init__(self, store, today=None):
        self.s, self.today = store, today or date.today
        for code, name, days in DEFAULT_TYPES:
            self.s.q("INSERT OR IGNORE INTO leave_types(code,name,yearly_days) VALUES(?,?,?)", (code, name, days))

    # ---------- types and allowances ----------
    def types(self):
        return self.s.all("SELECT code, name, yearly_days FROM leave_types ORDER BY rowid")

    def codes(self):
        return [t["code"] for t in self.types()]

    def set_yearly(self, code, days):
        if code not in self.codes():
            raise ValueError("no such leave type")
        if isinstance(days, bool) or not isinstance(days, (int, float)) or not 0 <= days <= MAX_YEARLY_DAYS or (days * 2) != int(days * 2):
            raise ValueError(f"the yearly allowance must be 0 to {MAX_YEARLY_DAYS} days, in steps of half a day")
        old = self.s.one("SELECT yearly_days FROM leave_types WHERE code=?", (code,))["yearly_days"]
        self.s.q("UPDATE leave_types SET yearly_days=? WHERE code=?", (float(days), code))
        return old

    # ---------- balances ----------
    def year(self):
        return self.today().year

    def _grant(self, person, code, year):
        t = self.s.one("SELECT yearly_days FROM leave_types WHERE code=?", (code,))
        if t:  # created once per person, type and year; the ref makes a repeat a no-op
            self.s.q("INSERT OR IGNORE INTO leave_ledger(person_id,leave_type,year,days,kind,ref,note,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (person, code, year, t["yearly_days"], "grant", f"grant:{person}:{code}:{year}", "Yearly allowance", "system", time.time()))

    def balance(self, person, code, year=None):
        y = year or self.year()
        self._grant(person, code, y)
        return round(self.s.one("SELECT COALESCE(SUM(days),0) b FROM leave_ledger WHERE person_id=? AND leave_type=? AND year=?", (person, code, y))["b"], 2)

    def balances(self, person, year=None):
        return {c: self.balance(person, c, year) for c in self.codes()}

    def entries(self, person, limit=200):
        return self.s.all("SELECT id, leave_type, year, days, kind, request_id, dates_json, note, actor, created_at FROM leave_ledger WHERE person_id=? ORDER BY id DESC LIMIT ?",
                          (person, min(max(limit, 1), 500)))

    def recorded_dates(self, person):
        out = set()
        for r in self.s.all("SELECT dates_json FROM leave_ledger WHERE person_id=? AND kind='use'", (person,)):
            out.update(json.loads(r["dates_json"] or "[]"))
        return out

    # ---------- checking a request before anyone decides ----------
    def check(self, person, details):
        """What this request would do to the balance, and what the approver should know. Never blocks: the approver decides (a low balance is a warning)."""
        code = details.get("leave_type") if details.get("leave_type") in self.codes() else "casual"
        days = working_days(details.get("dates"), details.get("date_range"), details.get("half_day"))
        n = round(sum(w for _, w in days), 2)
        year = days[0][0].year if days else self.year()
        bal = self.balance(person, code, year)
        warnings = []
        if not days:
            warnings.append("No working days in these dates: Saturday and Sunday are not counted.")
        elif bal - n < 0:
            warnings.append(f"{num(n)} working day{'s' if n != 1 else ''} needed, {num(bal)} left in {code} leave: the balance would be {num(bal - n)}.")
        overlap = sorted({d.isoformat() for d, _ in days} & self.recorded_dates(person))
        if overlap:
            warnings.append("Overlaps leave already recorded for " + ", ".join(overlap[:4]) + ".")
        return {"days": num(n), "type": code, "year": year, "balance": num(bal), "after": num(bal - n), "warnings": warnings}

    # ---------- writing (append only) ----------
    def find(self, ref):
        rows = self.s.all("SELECT * FROM leave_ledger WHERE group_ref=? AND kind='use' ORDER BY id", (ref,))
        if not rows:
            return None
        dates = sorted({d for r in rows for d in json.loads(r["dates_json"] or "[]")})
        return {"leave_id": f"L{rows[0]['id']}", "emp_id": rows[0]["person_id"], "type": rows[0]["leave_type"], "dates": dates,
                "days": num(-sum(r["days"] for r in rows)), "ref": ref}

    def apply(self, person, code, days, ref, request_id, reason, actor="system"):
        """Record leave taken: one line per calendar year it touches, all or none. Idempotent on `ref`."""
        if code not in self.codes():
            raise ActionError("unknown leave type", retryable=False)
        done = self.find(ref)
        if done:
            return done
        by_year = {}
        for d, w in days:
            by_year.setdefault(d.year, []).append((d, w))
        for y in by_year:
            self._grant(person, code, y)
        with self.s.lock:
            try:
                for y, items in sorted(by_year.items()):
                    self.s.conn.execute("INSERT INTO leave_ledger(person_id,leave_type,year,days,kind,request_id,ref,group_ref,dates_json,note,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (person, code, y, -round(sum(w for _, w in items), 2), "use", request_id, f"{ref}:{y}", ref,
                                         json.dumps([d.isoformat() for d, _ in items]), (reason or "")[:200], actor, time.time()))
                self.s.conn.commit()
            except sqlite3.IntegrityError:  # another run recorded this very leave first: that is the answer
                self.s.conn.rollback()
            except Exception:
                self.s.conn.rollback()
                raise
        return self.find(ref)

    def adjust(self, person, code, days, note, actor):
        """An admin's correction: plus or minus days with a reason, in the current year."""
        if code not in self.codes():
            raise ValueError("no such leave type")
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days == 0 or abs(days) > MAX_YEARLY_DAYS or (days * 2) != int(days * 2):
            raise ValueError("adjust by a non-zero number of days, in steps of half a day")
        y = self.year()
        self._grant(person, code, y)
        self.s.q("INSERT INTO leave_ledger(person_id,leave_type,year,days,kind,ref,note,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (person, code, y, float(days), "adjust", "adjust:" + uuid.uuid4().hex, note, actor, time.time()))
        return self.balance(person, code, y)


class LedgerHR:
    """The leave 'HR system' of a real company: the ledger above, behind the three methods the engine calls on the demo's MockHR."""
    label = "the leave record"
    applied = "recorded in your leave balance"
    records_balances = True

    def __init__(self, ledger):
        self.ledger = ledger

    @property
    def leaves(self):
        return self.ledger.s.all("SELECT group_ref FROM leave_ledger WHERE kind='use' GROUP BY group_ref")

    def reset(self):  # a company's records are never reset
        pass

    def heal(self):
        pass

    def list_leave_balances(self, emp_id):
        return {c: num(b) for c, b in self.ledger.balances(emp_id).items()}

    def find_leave_by_ref(self, ref):
        return self.ledger.find(ref)

    def check(self, emp_id, details):
        return self.ledger.check(emp_id, details)

    def create_leave_request(self, emp_id, dates, leave_type, reason, ref, half_day=False, date_range=False):
        days = working_days(dates, date_range, half_day)
        if not days:
            raise ActionError("there are no working days in those dates", retryable=False)
        rid = int(ref.rsplit("-", 1)[-1]) if ref.rsplit("-", 1)[-1].isdigit() else None
        return self.ledger.apply(emp_id, leave_type if leave_type in self.ledger.codes() else "casual", days, ref, rid, reason)
