"""Running a request type's after-approval steps (CLAUDE.md rule 21). The engine calls this when a request is routed (a snapshot of the steps is taken, so later edits never change a
routed request), when it is approved (the steps start), and the worker calls run_pending() every few seconds.

Each step is one row in step_runs: waiting, ready (waits for a person to press Run), pending (queued), running, then succeeded or rehearsed, or failed / unknown / skipped, which park the
request for an admin. At most once: an automatic retry happens only when the call provably did not leave (429, could not connect); any other doubt is "outcome unknown" and a person decides.
A company in rehearsal, the demo company and chaos runs never call the app. The only real call before going live is an admin's test run."""
import json
import threading
import time
from datetime import date

from . import steps, viasocket_adapter as vs
from .db import dumps

ACTIVE = ("waiting", "ready", "pending", "running")
DONE = ("succeeded", "rehearsed")
BAD = ("failed", "unknown", "skipped")
MAX_ATTEMPTS, PER_SWEEP, STUCK_S = 3, 3, 300
BACKOFF = [1, 4, 16]
WORDS = {"waiting": "Waiting for the step before", "ready": "Waiting for someone to press Run", "pending": "Queued", "running": "Running", "succeeded": "Done", "rehearsed": "Not sent (rehearsal)",
         "failed": "Needs attention", "unknown": "Needs attention: outcome unknown", "skipped": "Skipped"}


def _engine():
    from . import engine  # imported late: the engine imports this module
    return engine


def expect_check(spec):
    """The check that a success body looks like it did when an admin tested the step (rule 10): HTTP 200 alone is not success."""
    if not spec:
        return None
    msg = "The app answered in a different shape than when this step was tested."

    def check(body):
        if spec.get("kind") == "array":
            return "" if isinstance(body, list) else msg
        keys = spec.get("keys") or []
        if not isinstance(body, dict) or (keys and sum(k in body for k in keys) * 2 < len(keys)):
            return msg
        return ""
    return check


def learn_expect(body):
    """What to expect from this action from now on, from a real answer. None when the answer has no fields to recognise it by."""
    if isinstance(body, list):
        return {"kind": "array"}
    if isinstance(body, dict) and body:
        return {"kind": "object", "keys": sorted(str(k) for k in body)[:40]}
    return None


def summarise(body):
    """All that is kept of an app's answer: its field names and one short reference. Never the whole body."""
    if isinstance(body, dict):
        ref = next((str(body[k])[:80] for k in ("id", "url", "key", "_rowNumber", "number", "name") if isinstance(body.get(k), (str, int)) and not isinstance(body.get(k), bool)), None)
        return {"keys": sorted(str(k) for k in body)[:20], **({"ref": ref} if ref else {})}
    return {"items": len(body)} if isinstance(body, list) else {}


def flat(value, prefix=""):
    """key: value pairs of a nested input, for a human-readable line."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in flat(v, f"{prefix}{k}.")]
    return [(prefix.rstrip("."), value if not isinstance(value, list) else ", ".join(str(x) for x in value))]


class StepRunner:
    def __init__(self, engine):
        self.e = engine
        self._flight = threading.Lock()

    # ---------- modes ----------
    def _mode(self):
        """live: the app is really called. rehearsal and demo: it never is."""
        if getattr(self.e, "is_demo", False) or self.e._mock_depth:
            return "demo"
        return "live" if self.e.live is not None and self.e.live.mode() == "live" else "rehearsal"

    # ---------- the snapshot, taken when a request is routed ----------
    def snapshot(self, rid, code):
        """The company's enabled, tested steps for this type as they are now, kept with the request so a later edit never changes it. Nothing is stored when there are none."""
        ok, _ = steps.eligible(self.e.d, code)
        if not ok:
            return
        rows = [r for r in self.e.s.all("SELECT * FROM type_steps WHERE type_code=? AND enabled=1 ORDER BY position", (code,)) if steps._row_view(r)["tested"]]
        if not rows:
            return
        snap = {"close": steps.close_when_done(self.e.s, code), "steps": [
            {"position": i, "app": r["app_slug"], "app_name": r["app_name"], "action_id": r["action_id"], "action_name": r["action_name"], "version_id": r["action_version_id"],
             "label": r["label"], "run_mode": r["run_mode"], "input": json.loads(r["input_json"]), "expect": json.loads(r["expect_json"]) if r["expect_json"] else None}
            for i, r in enumerate(rows)]}
        self.e.s.q("UPDATE requests SET steps_json=? WHERE id=?", (dumps(snap), rid))
        self.e.audit(rid, "system", "step_planned", {"steps": [s["label"] for s in snap["steps"]]})

    def _snap(self, rid):
        row = self.e.s.one("SELECT steps_json FROM requests WHERE id=?", (rid,))
        return json.loads(row["steps_json"]) if row and row["steps_json"] else None

    def has_steps(self, rid):
        return bool(self._snap(rid))

    def _rows(self, rid):
        return self.e.s.all("SELECT * FROM step_runs WHERE request_id=? ORDER BY position", (rid,))

    def unfinished(self, rid):
        return any(r["status"] not in DONE for r in self._rows(rid))

    def has_runs(self, rid):
        return bool(self.e.s.one("SELECT 1 x FROM step_runs WHERE request_id=? LIMIT 1", (rid,)))

    # ---------- what a request's steps are made of ----------
    def context(self, rid):
        """The request fields a step may use (steps.TOKENS), from the request and the directory. Never the raw message."""
        r = self.e.req(rid)
        emp = self.e.d.emp(r["employee_id"]) or {}
        mgr = self.e.d.emp(emp.get("manager_id")) if emp.get("manager_id") else None
        approvers = []
        for ev in self.e.s.all("SELECT actor FROM events WHERE request_id=? AND type='approved_stage' ORDER BY id", (rid,)):
            n = self.e.name(ev["actor"])
            if n not in approvers:
                approvers.append(n)
        day = lambda ts: date.fromtimestamp(ts).isoformat() if ts else self.e.today().isoformat()  # noqa: E731
        ctx = {"request.id": rid, "request.ref": f"deskhand-{rid}", "request.summary": r["summary"] or "", "request.type": (self.e.d.type(r["type_code"]) or {}).get("name", ""),
               "request.created_date": day(r["created_at"]), "request.approved_date": day(r["decided_at"]), "requester.name": emp.get("name", ""), "requester.email": emp.get("email") or "",
               "requester.department": emp.get("department") or "", "requester.manager_name": mgr["name"] if mgr else "", "approvers.names": ", ".join(approvers)}
        for k in steps.DETAILS.get(r["type_code"], []):
            v = r["details"].get(k)
            ctx[f"detail.{k}"] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else ("" if v is None else str(v))
        return ctx

    def _fulfiller(self, rid):
        r = self.e.req(rid)
        t = self.e.d.type(r["type_code"])
        return self.e.d.resolve_rule(t.get("fulfiller_rule"), self.e.d.emp(r["employee_id"])) or self.e.d.role_holder("admin")

    def can_run(self, r, actor_id):
        """Who may press Run: the person who gave the final approval, the person who fulfils this kind of request, or an admin."""
        ful = self._fulfiller(r["id"])
        return actor_id == self.e._decider(r["id"]) or bool(ful and actor_id == ful["id"]) or self.e._is_admin(actor_id)

    # ---------- starting, moving on, finishing ----------
    def begin(self, rid, approved_by=None):
        """The request is approved: make a run for each step in the snapshot and start the first."""
        snap = self._snap(rid)
        mode, now = self._mode(), self.e.now()
        for st in snap["steps"]:
            self.e.s.q("INSERT OR IGNORE INTO step_runs(request_id,position,step_key,status,run_mode,mode_at_creation,label,app_slug,action_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (rid, st["position"], f"{rid}:{st['position']}", "waiting", st["run_mode"], mode, st["label"], st["app"], st["action_id"], now))
        who = f" by {self.e.name(approved_by)}" if approved_by else ""
        self.e._notify(rid, self.e.req(rid)["employee_id"], f"Your request #{rid} was approved{who}. Deskhand is doing the next steps and will tell you when it is done.", "approved", by=approved_by)
        self._advance(rid)

    def _set(self, row_id, status, **cols):
        sets = ", ".join(["status=?"] + [f"{k}=?" for k in cols])
        self.e.s.q(f"UPDATE step_runs SET {sets} WHERE id=?", (status, *cols.values(), row_id))

    def _advance(self, rid):
        """Start whatever is next. Stops at a step that needs a person, is queued or running, or has failed."""
        while True:
            r = self.e.req(rid)
            if not r or r["status"] != "FULFILLING":
                return
            row = next((x for x in self._rows(rid) if x["status"] not in DONE), None)
            if row is None:
                return self._finished(rid)
            if row["status"] in BAD:  # a step that needs a person is first in line: the request waits with the admin
                self.e._set_status(rid, "PARKED", "system", f"step '{row['label']}' needs a decision")
                return
            if row["status"] != "waiting":
                return
            if row["run_mode"] == "confirm":
                self._set(row["id"], "ready")
                ful = self._fulfiller(rid)
                self.e._notify(rid, ful["id"] if ful else None, f"Request #{rid}: the step '{row['label']}' is ready and waits for you to press Run on the request page.", "task",
                               subject=f"Run a step: request #{rid}")
                return
            if self._mode() != "live":
                self._rehearse(row)
                continue
            self._set(row["id"], "pending", next_at=None)
            return

    def _rehearse(self, row):
        """Record what would have run, tell the rehearsing admin, and carry on as if it had worked so the rest of the flow can be rehearsed."""
        rid = row["request_id"]
        st = self._snap(rid)["steps"][row["position"]]
        inp = steps.render(st["input"], self.context(rid))
        self._set(row["id"], "rehearsed", input_json=dumps(inp), finished_at=self.e.now())
        self.e.audit(rid, "system", "step_rehearsed", {"position": row["position"], "label": row["label"], "app": row["app_slug"]})
        shown = "; ".join(f"{k}: {v}" for k, v in flat(inp))[:600]
        ful = self._fulfiller(rid)
        self.e._notify(rid, ful["id"] if ful else None, f"Step {row['position'] + 1} for request #{rid}, '{row['label']}' ({st['app_name']}: {st['action_name']}), would have run now. "
                       f"Nothing was sent to the app. Values: {shown}", "step_rehearsed", n=row["position"], subject=f"Rehearsal: a step for request #{rid}")

    def _finished(self, rid):
        r, snap = self.e.req(rid), self._snap(rid)
        self.e.audit(rid, "system", "steps_finished", {"steps": len(snap["steps"])})
        if snap["close"]:
            self.e._set_status(rid, "FULFILLED", "system", "all steps finished")
            self.e._notify(rid, r["employee_id"], f"Done: your request #{rid} ({self.e.d.type(r['type_code'])['name']}) is complete.", "fulfilled", by=self.e._decider(rid))
            self.e._set_status(rid, "CLOSED", "system")
        else:
            self.e._hand_to_fulfiller(rid)

    def _park(self, row, status, error, code=""):
        """A step that did not finish: a person decides what happens next. Nothing is lost and nothing runs twice by itself."""
        rid = row["request_id"]
        error = vs.redact(str(error or ""), self._secrets())[:300]
        self._set(row["id"], status, error=error, finished_at=self.e.now())
        self.e.audit(rid, "system", "action_failed", {"step": row["label"], "position": row["position"], "app": row["app_slug"], "code": code, "outcome": status, "attempt": row["attempts"], "error": error})
        if self.e.req(rid)["status"] == "FULFILLING":
            self.e._set_status(rid, "PARKED", "system", f"step '{row['label']}' {'may or may not have run' if status == 'unknown' else 'did not finish'}")
        admin = self.e.d.role_holder("admin")
        what = "may or may not have happened in the app: check it first" if status == "unknown" else "did not finish"
        self.e._notify(rid, admin["id"] if admin else None, f"Request #{rid} is parked: the step '{row['label']}' {what}. {error} Then choose Run again or Mark as done on the request page.",
                       "parked", subject=f"Needs attention: request #{rid}")
        self.e._notify(rid, self.e.req(rid)["employee_id"], f"Your request #{rid} hit a delay in a step. It's with the admin and nothing is lost.", "delay")

    def _secrets(self):
        return vs.ConnectionStore(self.e.s, getattr(self.e, "org_id", None) or (self.e.live.tenant if self.e.live else "acme-demo")).secrets()

    # ---------- the worker ----------
    def run_pending(self, limit=PER_SWEEP):
        """Run the queued steps of this company (live only). A step that was running when the app stopped is parked as unknown, never run again by itself."""
        out = {"ran": 0}
        if self._mode() != "live" or not self._flight.acquire(blocking=False):
            return out
        try:
            for row in self.e.s.all("SELECT * FROM step_runs WHERE status='running' AND started_at<?", (time.time() - STUCK_S,)):
                self._park(row, "unknown", "The app stopped before an answer came back.", "stuck")
            now = self.e.now()
            for row in self.e.s.all("SELECT * FROM step_runs WHERE status='pending' AND (next_at IS NULL OR next_at<=?) ORDER BY id LIMIT ?", (now, limit)):
                claimed = self.e.s.q("UPDATE step_runs SET status='running', attempts=attempts+1, started_at=? WHERE id=? AND status='pending'", (time.time(), row["id"])).rowcount
                if claimed == 1:  # exactly one runner gets a row
                    self._execute(self.e.s.one("SELECT * FROM step_runs WHERE id=?", (row["id"],)))
                    out["ran"] += 1
        finally:
            self._flight.release()
        return out

    def _execute(self, row):
        rid, pos = row["request_id"], row["position"]
        r = self.e.req(rid)
        if not r or r["status"] != "FULFILLING" or self._mode() != "live" or row["mode_at_creation"] != "live":
            self._set(row["id"], "skipped", error="The request or the delivery mode changed before this step ran.", finished_at=self.e.now())
            self.e.audit(rid, "system", "step_skipped", {"position": pos, "label": row["label"]})
            return
        st = self._snap(rid)["steps"][pos]
        try:
            inp = steps.render(st["input"], self.context(rid))
            if len(json.dumps(inp, default=str)) > 30000:
                raise ValueError("The values for this step are too large.")
        except Exception as ex:  # nothing was sent: a permanent failure a person can read
            return self._park(row, "failed", f"Deskhand could not prepare the values: {ex}", "prepare")
        self.e.s.q("UPDATE step_runs SET input_json=? WHERE id=?", (dumps(inp), row["id"]))
        self.e.audit(rid, "system", "step_started", {"position": pos, "label": row["label"], "app": st["app"], "attempt": row["attempts"]})
        try:
            res = self.e.live.factory().run_step(st["app"], st["version_id"], inp, expect_check(st["expect"]))
        except Exception:  # we cannot tell whether the call left: a person must look
            return self._park(row, "unknown", "Deskhand stopped unexpectedly while the step was running.", "exception")
        if res.ok:
            self._set(row["id"], "succeeded", result_summary=dumps(summarise(res.data)), error=None, finished_at=self.e.now())
            self.e.audit(rid, "system", "step_succeeded", {"position": pos, "label": row["label"], "attempt": row["attempts"]})
            self._advance(rid)
        elif res.kind == vs.RETRYABLE and res.safe_retry:  # provably not delivered (rate limit, could not connect): safe to try again, a few times
            if row["attempts"] >= MAX_ATTEMPTS:
                return self._park(row, "failed", res.error or "The app kept refusing the step for now.", res.code)
            wait = BACKOFF[min(row["attempts"] - 1, 2)]
            self._set(row["id"], "pending", next_at=self.e.now() + wait, error=vs.redact(res.error or "", self._secrets())[:300])
            self.e.audit(rid, "system", "step_retry", {"position": pos, "label": row["label"], "attempt": row["attempts"], "code": res.code, "next_backoff_s": wait})
        elif res.kind == vs.RETRYABLE:  # it may have been delivered: never send again on our own
            self._park(row, "unknown", res.error or "No answer came back in time.", res.code)
        else:
            self._park(row, "failed", res.error or "The app refused the step.", res.code)

    # ---------- people ----------
    def confirm(self, rid, pos, actor_id):
        """Someone presses Run on a step that was waiting for it."""
        r = self.e.req(rid)
        row = self.e.s.one("SELECT * FROM step_runs WHERE request_id=? AND position=?", (rid, pos))
        if not row or row["status"] != "ready" or r["status"] != "FULFILLING":
            raise _engine().InvalidTransition("this step is not waiting for Run")
        if not self.can_run(r, actor_id):
            self.e.audit(rid, actor_id, "unauthorized_action_refused", {"action": "run_step"})
            raise _engine().NotAuthorized("only the approver, the person who completes this kind of request, or an admin can press Run")
        self.e.audit(rid, actor_id, "step_confirmed", {"position": pos, "label": row["label"]})
        if self._mode() != "live":
            self._rehearse(row)
        else:
            self._set(row["id"], "pending", next_at=None)
        self._advance(rid)
        return self.e.req(rid)

    def replay(self, rid, actor_id, pos=None, how="again", reason=""):
        """An admin looks at a parked request and chooses: run the step again, or say it was done (after checking the app)."""
        r = self.e.req(rid)
        if not self.e._is_admin(actor_id):
            raise _engine().NotAuthorized("only an admin can do this")
        bad = [x for x in self._rows(rid) if x["status"] in BAD]
        row = next((x for x in bad if pos is None or x["position"] == pos), None)
        if r["status"] != "PARKED" or not row:
            raise _engine().InvalidTransition("no step of this request is waiting for a decision")
        if how == "mark_done":
            reason = " ".join(str(reason or "").split())
            if len(reason) < 3 or len(reason) > 200:
                raise _engine().Refused("say what you checked, in 3 to 200 characters")
            self._set(row["id"], "succeeded", result_summary=dumps({"manual": True}), error=None, finished_at=self.e.now())
            self.e.audit(rid, actor_id, "step_marked_done", {"position": row["position"], "label": row["label"], "reason": reason})
        elif how == "again":
            mode = self._mode()
            for x in bad:  # steps skipped together (a mode change) start together; a failed one starts alone
                if x["id"] == row["id"] or x["status"] == "skipped":
                    self._set(x["id"], "waiting", attempts=0, error=None, next_at=None, mode_at_creation=mode)
            self.e.audit(rid, actor_id, "step_replayed", {"position": row["position"], "label": row["label"]})
        else:
            raise _engine().Refused("choose Run again or Mark as done")
        self.e._set_status(rid, "FULFILLING", actor_id, f"step '{row['label']}': {how.replace('_', ' ')}")
        self._advance(rid)
        return self.e.req(rid)

    def skip_unfinished(self, why):
        """The delivery mode changed: nothing prepared under the other mode runs now. The requests they belong to are parked so an admin decides, in the new mode."""
        n, parked = 0, set()
        for row in self.e.s.all(f"SELECT * FROM step_runs WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
            self._set(row["id"], "skipped", error=why, finished_at=self.e.now())
            self.e.audit(row["request_id"], "system", "step_skipped", {"position": row["position"], "label": row["label"], "why": why})
            n += 1
            parked.add(row["request_id"])
        for rid in sorted(parked):
            if self.e.req(rid)["status"] == "FULFILLING":
                self.e._set_status(rid, "PARKED", "system", why)
                admin = self.e.d.role_holder("admin")
                self.e._notify(rid, admin["id"] if admin else None, f"Request #{rid} is parked: {why}. Choose Run again on the request page when you are ready.", "parked", subject=f"Needs attention: request #{rid}")
        return n

    def on_cancel(self, rid):
        for row in self.e.s.all("SELECT * FROM step_runs WHERE request_id=? AND status IN ('waiting','ready','pending')", (rid,)):
            self._set(row["id"], "skipped", error="The request was cancelled.", finished_at=self.e.now())

    # ---------- what people see ----------
    def view(self, rid, with_input=False):
        snap = self._snap(rid)
        out = []
        for row in self._rows(rid):
            st = snap["steps"][row["position"]] if snap and row["position"] < len(snap["steps"]) else {}
            out.append({"position": row["position"], "label": row["label"], "app_name": st.get("app_name"), "action_name": st.get("action_name"), "status": row["status"],
                        "status_text": WORDS.get(row["status"], row["status"]), "run_mode": row["run_mode"], "attempts": row["attempts"], "error": row["error"] or None,
                        "result": json.loads(row["result_summary"]) if row["result_summary"] else None, **({"input": json.loads(row["input_json"]) if row["input_json"] else None} if with_input else {})})
        return out

    # ---------- an admin's test of one step (the only real call before going live) ----------
    def test(self, code, step_id, actor_id, typed=None):
        """Really run this step once, with made-up request details, and learn what a good answer looks like. Marks the step tested only if it worked."""
        row = self.e.s.one("SELECT * FROM type_steps WHERE id=? AND type_code=?", (step_id, code))
        if not row:
            raise steps.StepError("No such step.", 404)
        if self.e.live is None:
            raise steps.StepError("This company cannot run steps.", 409)
        view = steps._row_view(row)
        if view["destructive"] and (typed or "").strip().lower() != (row["action_name"] or "").strip().lower():
            raise steps.StepError(f"This action deletes or removes things. Type its name, \"{row['action_name']}\", to confirm the test.")
        inp = steps.render(json.loads(row["input_json"]), steps.sample_context(code))
        res = self.e.live.factory().run_step(row["app_slug"], row["action_version_id"], inp, None)
        learned = learn_expect(res.data) if res.ok else None
        if res.ok and learned:
            outcome, error = "success", ""
            self.e.s.q("UPDATE type_steps SET expect_json=?, tested_hash=?, tested_at=? WHERE id=?",
                       (dumps(learned), steps.step_hash(row["app_slug"], row["action_id"], row["action_version_id"], json.loads(row["input_json"])), self.e.now(), step_id))
        elif res.ok:
            outcome, error = "unrecognisable", "The app's answer has no fields Deskhand can recognise it by, so this step cannot be trusted to report success."
        else:
            outcome, error = res.kind, vs.redact(res.error or "", self._secrets())[:300]
        self.e.audit(None, actor_id, "step_tested", {"code": code, "step": row["label"], "app": row["app_slug"], "outcome": outcome, "reason": res.code or ""})
        return {"outcome": outcome, "error": error, "tested": outcome == "success", "sent": inp, "response": json.dumps(vs.scrub(res.data), default=str)[:1500] if res.ok else None}
