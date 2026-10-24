"""After-approval steps: what an admin tells Deskhand to do in another app once a request type is approved (CLAUDE.md rule 21).

A step is validated, bounded setup data: an app and an action from viaSocket's catalog, and values for the action's form. A value is either something the admin typed or picked,
or a text with request fields in braces such as {requester.name}. Only the fields in TOKENS (plus a type's own details) may be used: never the message text, never anything that
names who or what to act on (rule 2). Nothing here makes a network call or reads the AI; the catalog and the running of steps are separate (viasocket_catalog.py, engine)."""
import datetime
import hashlib
import json
import math
import re
import time

from . import viasocket_catalog as vcat
from .setup import SetupError, _clean

MAX_STEPS, MAX_TEXT, MAX_RENDERED, MAX_TOKEN_VALUE = 3, 1000, 2000, 500
MAX_FIELDS, MAX_DEPTH, MAX_OPTIONS, MAX_LIST, MAX_GROUP_KEYS = 150, 4, 200, 50, 60
NEVER = {"leave", "payroll", "policy_question"}  # leave is the ledger's (rules 4 and 19), payroll is blocked, a policy question has nothing to fulfil
RUN_MODES = ("auto", "confirm")
TOKEN_RE = re.compile(r"\{([a-z][a-z0-9_.]{0,40})\}")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# The request fields a step may use. (label, kind). Everything is from the request row or the directory; nothing is the raw message text.
TOKENS = {
    "request.id": ("Request number", "number"), "request.ref": ("Request reference, to find it again in the app", "text"), "request.summary": ("Summary of the request", "text"),
    "request.type": ("Request type", "text"), "request.created_date": ("Date the request was made", "date"), "request.approved_date": ("Date of the final approval", "date"),
    "requester.name": ("Requester's name", "text"), "requester.email": ("Requester's email", "text"), "requester.department": ("Requester's department", "text"),
    "requester.manager_name": ("Requester's manager", "text"), "approvers.names": ("Everyone who approved", "text"),
}
DETAILS = {"software_seat": ["tool", "project", "justification", "estimated_cost"], "equipment": ["item", "reason", "estimated_cost"], "accessories": ["item", "reason", "estimated_cost"],
           "it_help": ["issue"], "access": ["resource", "tool", "access_level", "estimated_cost"]}  # the details the readers extract (classifier._details)
DETAIL_LABELS = {"tool": "Tool", "project": "Project", "justification": "Reason given", "estimated_cost": "Estimated cost", "item": "Item", "reason": "Reason", "issue": "Issue",
                 "resource": "Resource", "access_level": "Access level"}
SAMPLE = {"request.id": 12, "request.ref": "deskhand-12", "request.summary": "Claude seat for the research project", "request.type": "Software seat", "request.created_date": "2026-10-22",
          "request.approved_date": "2026-10-22", "requester.name": "Priya Sharma", "requester.email": "priya.sharma@example.com", "requester.department": "Research",
          "requester.manager_name": "Rahul Mehta", "approvers.names": "Imran Khan",
          "detail.tool": "Claude", "detail.project": "Research", "detail.justification": "Needed for the research project", "detail.estimated_cost": 250, "detail.item": "Second monitor",
          "detail.reason": "Current screen is too small", "detail.issue": "VPN is not connecting", "detail.resource": "Finance reports", "detail.access_level": "Read only"}


class StepError(SetupError):
    pass


def tokens_for(code):
    out = dict(TOKENS)
    for k in DETAILS.get(code, []):
        out[f"detail.{k}"] = (f"Detail: {DETAIL_LABELS[k].lower()}", "number" if k == "estimated_cost" else "text")
    return out


def token_list(code):
    return [{"token": t, "label": lab, "kind": kind} for t, (lab, kind) in tokens_for(code).items()]


def sample_context(code):
    return {t: SAMPLE[t] for t in tokens_for(code) if t in SAMPLE}


def eligible(d, code):
    """Whether a request type may have steps, and if not, why."""
    t = d.types.get(code)
    if not t:
        return False, "No such request type."
    if code in NEVER or t.get("blocked") or t.get("fulfilment_kind") != "manual_task":
        return False, "This request type is handled by Deskhand itself and cannot have steps in other apps."
    return True, ""


# ---------------- reading an action's form (input_schema) ----------------
def _truthy_flag(v):
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _options(blk):
    raw = blk.get("options") if isinstance(blk.get("options"), list) else blk.get("children")
    if not isinstance(raw, list):
        return None
    return [{"label": str(o.get("label", o.get("value")))[:120], "value": o["value"]} for o in raw[:MAX_OPTIONS]
            if isinstance(o, dict) and "value" in o and isinstance(o["value"], (str, int, float, bool))]


def _default(blk):
    v = blk.get("defaultValue")
    return v["value"] if isinstance(v, dict) and "value" in v else v


def normalize_schema(raw):
    """A catalog `input_schema` {steps, blocks} as an ordered tree of fields Deskhand can draw and check.

    Blocks that cannot be reached from steps.root are leftovers and ignored. A field of a kind Deskhand cannot draw yet is dropped if it is optional; if it is required the
    whole action is `supported: False` with the reason. Types: text, number, date, boolean, dropdown, multiselect, group (fixed children), fetched_group (one text per option the
    app lists: Google Sheets column values), dictionary."""
    steps = raw.get("steps") if isinstance(raw, dict) and isinstance(raw.get("steps"), dict) else {}
    blocks = raw.get("blocks") if isinstance(raw, dict) and isinstance(raw.get("blocks"), dict) else {}
    out = {"fields": [], "supported": True, "reasons": [], "dropped": []}
    count = [0]

    def unsupported(blk, key, why):
        label = str(blk.get("label") or key)[:60]
        if _truthy_flag(blk.get("required")):
            out["supported"] = False
            out["reasons"].append(f"{label}: {why}")
        else:
            out["dropped"].append(label)

    def build(key, depth):
        blk = blocks.get(key)
        if not isinstance(blk, dict):
            return None
        count[0] += 1
        t = blk.get("type")
        node = {"key": key, "label": str(blk.get("label") or key)[:80], "help": str(blk.get("help") or "")[:300], "placeholder": str(blk.get("placeholder") or "")[:120],
                "required": _truthy_flag(blk.get("required")), "options": _options(blk), "dynamic": False, "search": bool(blk.get("enableSearchApi")),
                "depends_on": [d for d in blk.get("dependsOn") or [] if isinstance(d, str)][:10], "visible_if": blk.get("visibilityCondition") if isinstance(blk.get("visibilityCondition"), str) else None,
                "default": _default(blk), "is_list": bool(blk.get("list")), "hidden": bool(blk.get("isHidden"))}
        if node["visible_if"] and len(node["visible_if"]) > 400:
            node["visible_if"] = None
        if t in ("string", "aifield", "html", "markdown"):
            node["type"] = "text"
        elif t in ("number", "date", "boolean"):
            node["type"] = t
        elif t in ("dropdown", "multiselect"):
            node["type"] = t
            node["dynamic"] = node["options"] is None or bool(blk.get("willDynamicFetchOptions"))
        elif t == "dictionary":
            node["type"] = "dictionary"
        elif t == "help":
            node["type"] = "help"
        elif t == "input groups":
            kids = steps.get(key) or []
            if kids and depth < MAX_DEPTH:
                node["type"] = "group"
                node["children"] = [c for c in (build(k, depth + 1) for k in kids if isinstance(k, str)) if c]
            else:
                node["type"] = "fetched_group"
        else:
            unsupported(blk, key, f"a field of type {t or 'unknown'} is not supported yet")
            return None
        return node

    for key in (steps.get("root") or [])[:MAX_FIELDS]:
        n = build(key, 1) if isinstance(key, str) else None
        if n:
            out["fields"].append(n)
    if count[0] > MAX_FIELDS:
        out["supported"], out["reasons"] = False, out["reasons"] + ["This action has too many fields."]
    return out


def describe_action(raw):
    """What the admin sees about one catalog action, plus the form already read."""
    schema = normalize_schema(raw.get("input_schema"))
    name = str(raw.get("name") or "").strip()[:80]
    return {"action_id": raw["action_id"], "action_version_id": raw["action_version_id"], "version": str(raw.get("versionid") or "")[:20], "name": name,
            "description": str(raw.get("description") or "")[:300], "destructive": vcat.is_destructive(name), "supported": schema["supported"], "reasons": schema["reasons"][:3],
            "schema": schema}


def find_action(catalog, slug, action_id):
    """(app, described action) or raises a StepError the admin can read."""
    if not vcat.valid_slug(slug):
        raise StepError("That is not an app name Deskhand can use.")
    try:
        app = catalog.app(slug)
    except vcat.CatalogError as e:
        raise StepError(str(e), e.status)
    if not app:
        raise StepError("That app is not in the catalog.", 404)
    raw = next((a for a in app["actions"] if a.get("action_id") == action_id), None)
    if not raw:
        raise StepError("That action is not in the catalog for this app.", 404)
    return app, describe_action(raw)


# ---------------- conditions: a tiny evaluator for the visibility expressions apps publish (no eval) ----------------
class _Unreadable(Exception):
    pass


_TOK = re.compile(r"""\s*(?:(?P<op>\?\.|===|!==|==|!=|&&|\|\||[!().\[\]])|(?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(?P<num>-?\d+(?:\.\d+)?)|(?P<id>[A-Za-z_$][\w$]*))""")


def _tokenize(expr):
    pos, out = 0, []
    expr = expr.rstrip()
    while pos < len(expr):
        m = _TOK.match(expr, pos)
        if not m:
            raise _Unreadable()
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


def _unquote(text):
    return re.sub(r"\\(.)", r"\1", text[1:-1])


def _js_truthy(v):
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0 and not (isinstance(v, float) and math.isnan(v))
    return v != ""


def _js_same(a, b):
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) == type(b) and a == b


def get_path(values, dotted):
    cur = values
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def eval_condition(expr, values):
    """(visible, readable). An expression this evaluator does not understand is not guessed at: the field stays visible and `readable` is False."""
    try:
        toks, i = _tokenize(expr), [0]

        def peek():
            return toks[i[0]] if i[0] < len(toks) else (None, None)

        def take():
            t = peek()
            i[0] += 1
            return t

        def primary():
            kind, text = take()
            if kind == "op" and text == "(":
                v = parse_or()
                if take() != ("op", ")"):
                    raise _Unreadable()
                return v
            if kind == "str":
                return _unquote(text)
            if kind == "num":
                return float(text) if "." in text else int(text)
            if kind == "id":
                lit = {"true": True, "false": False, "null": None, "undefined": None}
                if text in lit and peek()[1] not in (".", "?.", "["):
                    return lit[text]
                path = [text]
                while peek() in (("op", "."), ("op", "?."), ("op", "[")):
                    _, op = take()
                    if op == "?." and peek() == ("op", "["):
                        _, op = take()
                    if op == "[":  # context.inputData["a b"]
                        k, quoted = take()
                        if k != "str" or take() != ("op", "]"):
                            raise _Unreadable()
                        path.append(_unquote(quoted))
                    else:
                        k, name = take()
                        if k != "id":
                            raise _Unreadable()
                        path.append(name)
                if path[:2] != ["context", "inputData"]:
                    raise _Unreadable()
                cur = values
                for part in path[2:]:
                    if part == "length" and isinstance(cur, (list, str)):
                        cur = len(cur)
                    elif isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        return None
                return cur
            raise _Unreadable()

        def cmp():
            a = primary()
            k, op = peek()
            if k == "op" and op in ("===", "!==", "==", "!="):
                take()
                b = primary()
                eq = _js_same(a, b) or (op in ("==", "!=") and str(a) == str(b) and a is not None and b is not None)
                return eq if op in ("===", "==") else not eq
            return a

        def not_():
            if peek() == ("op", "!"):
                take()
                return not _js_truthy(not_())
            return cmp()

        def and_():
            v = not_()
            while peek() == ("op", "&&"):
                take()
                r = not_()
                v = r if _js_truthy(v) else v
            return v

        def parse_or():
            v = and_()
            while peek() == ("op", "||"):
                take()
                r = and_()
                v = v if _js_truthy(v) else r
            return v
        result = parse_or()
        if i[0] != len(toks):
            raise _Unreadable()
        return _js_truthy(result), True
    except (_Unreadable, ValueError, UnicodeDecodeError, RecursionError):
        return True, False


# ---------------- values ----------------
def _has(v):
    return not (v is None or v == "" or v == [] or v == {})


def _set(cleaned, dotted, value):
    cur, parts = cleaned, dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _visible(node, values):
    """Visible = not hidden, every field it depends on has a value, and its condition (if any) holds. Returns (visible, readable)."""
    if node["hidden"] or node["type"] == "help":
        return False, True
    if any(not _has(get_path(values, d)) for d in node["depends_on"]):
        return False, True
    if node["visible_if"]:
        return eval_condition(node["visible_if"], values)
    return True, True


def _token_kind_ok(text, kind, allowed):
    names = TOKEN_RE.findall(text)
    return len(names) == 1 and text.strip() == "{" + names[0] + "}" and names[0] in allowed and allowed[names[0]][1] == kind


def clean_text(value, label, allowed):
    if not isinstance(value, str):
        raise StepError(f"{label} must be text.")
    s = CONTROL.sub("", value)
    if len(s) > MAX_TEXT:
        raise StepError(f"{label} is longer than {MAX_TEXT} characters.")
    for name in TOKEN_RE.findall(s):
        if name not in allowed:
            raise StepError(f"{label} uses {{{name}}}, which is not a request field Deskhand offers.")
    return s


def _scalar(value, label):
    if isinstance(value, bool) or (isinstance(value, (int, float)) and math.isfinite(value)) or (isinstance(value, str) and len(value) <= 300 and not CONTROL.search(value)):
        return value
    raise StepError(f"{label} has a choice Deskhand cannot use.")


def _check_one(node, value, allowed):
    t, label = node["type"], node["label"]
    if t == "text":
        return clean_text(value, label, allowed)
    if t == "number":
        if isinstance(value, str) and _token_kind_ok(value, "number", allowed):
            return value.strip()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise StepError(f"{label} must be a number.")
        return value
    if t == "date":
        if isinstance(value, str) and _token_kind_ok(value, "date", allowed):
            return value.strip()
        try:
            datetime.date.fromisoformat(value)
        except (TypeError, ValueError):
            raise StepError(f"{label} must be a date like 2026-10-22.")
        if len(value) != 10:
            raise StepError(f"{label} must be a date like 2026-10-22.")
        return value
    if t == "boolean":
        if not isinstance(value, bool):
            raise StepError(f"{label} must be yes or no.")
        return value
    if t == "dropdown":
        v = _scalar(value, label)
        if isinstance(v, str) and TOKEN_RE.search(v):
            raise StepError(f"{label} is a choice from the app and cannot use request fields.")
        if node["options"] is not None and v not in [o["value"] for o in node["options"]]:
            raise StepError(f"{label} is not one of the choices the app offers.")
        return v
    raise StepError(f"{label} cannot be set this way.")


def _check_field(node, value, allowed):
    t, label = node["type"], node["label"]
    if node["is_list"] and t not in ("multiselect", "group", "fetched_group"):
        if not isinstance(value, list) or len(value) > MAX_LIST:
            raise StepError(f"{label} must be a list of up to {MAX_LIST} items.")
        return [_check_one(node, v, allowed) for v in value]
    if t == "multiselect":
        if not isinstance(value, list) or len(value) > MAX_LIST:
            raise StepError(f"{label} must be a list of up to {MAX_LIST} choices.")
        return [_check_one({**node, "type": "dropdown"}, v, allowed) for v in value]
    if t == "dictionary":
        if not isinstance(value, dict) or len(value) > 30:
            raise StepError(f"{label} must be up to 30 name and value pairs.")
        return {clean_text(k, label, {})[:80]: clean_text(v, label, allowed) for k, v in value.items()}
    if t == "fetched_group":
        if not isinstance(value, dict) or len(value) > MAX_GROUP_KEYS:
            raise StepError(f"{label} must be up to {MAX_GROUP_KEYS} values.")
        out = {}
        for k, v in value.items():
            if not isinstance(k, str) or not k or len(k) > 120 or CONTROL.search(k):
                raise StepError(f"{label} has a column Deskhand cannot use.")
            out[k] = clean_text(v, label, allowed) if isinstance(v, str) else _scalar(v, label)
        return out
    return _check_one(node, value, allowed)


def _fill_defaults(schema, values):
    """A copy of the values where a visible field with no value takes the form's own default, in field order, so the fields that depend on it can appear (as the app's page does)."""
    vals = json.loads(json.dumps(values, default=str)) if isinstance(values, dict) else {}

    def walk(nodes):
        for n in nodes:
            if not _visible(n, vals)[0]:
                continue
            if n["type"] == "group":
                walk(n["children"])
            elif not _has(get_path(vals, n["key"])) and n["default"] is not None and n["type"] in ("text", "number", "date", "boolean", "dropdown", "multiselect"):
                _set(vals, n["key"], n["default"])
    walk(schema["fields"])
    return vals


def validate_values(schema, values, allowed):
    """Check an admin's values against an action's form. Returns (cleaned, errors, dropped, unreadable): `cleaned` has only visible, valid, non-empty values in the shape the
    action expects; `errors` is {key: message}; `dropped` lists keys that were given but are hidden by the form's own conditions; `unreadable` lists conditions not understood."""
    errors, dropped, unreadable, cleaned = {}, [], [], {}
    if not isinstance(values, dict) or len(json.dumps(values, default=str)) > 30000:
        return {}, {"": "The values are not in the right shape or are too large."}, [], []
    top = set()
    values = _fill_defaults(schema, values)

    def walk(nodes, vals):
        for n in nodes:
            top.add(n["key"].split(".")[0])
            vis, readable = _visible(n, vals)
            if not readable:
                unreadable.append(n["key"])
            raw = get_path(vals, n["key"])
            if not vis:
                if _has(raw):
                    dropped.append(n["key"])
                continue
            if n["type"] == "group":
                walk(n["children"], vals)
                continue
            if not _has(raw):
                if n["required"]:
                    errors[n["key"]] = f"{n['label']} is required."
                continue
            try:
                _set(cleaned, n["key"], _check_field(n, raw, allowed))
            except StepError as e:
                errors[n["key"]] = str(e)
    walk(schema["fields"], values)
    for k in values:
        if k not in top:
            errors[str(k)[:60]] = "This field is not part of the action's form."
    return cleaned, errors, dropped, unreadable


def field_keys(schema):
    """Every field key of a form, including those inside groups."""
    out = set()

    def walk(nodes):
        for n in nodes:
            out.add(n["key"])
            walk(n.get("children") or [])
    walk(schema["fields"])
    return out


def visible_keys(schema, values):
    """The keys the form shows right now for these values (what the admin's page should draw)."""
    out, values = [], _fill_defaults(schema, values)

    def walk(nodes):
        for n in nodes:
            if _visible(n, values)[0]:
                out.append(n["key"])
                walk(n.get("children") or [])
    walk(schema["fields"])
    return out


# ---------------- using the values for a real request (the engine does this at run time) ----------------
def neutralise(s):
    """Text that came from a request must not act as a command in the app it lands in: a leading = + - @ makes a spreadsheet cell a formula, and <! or <@ mentions people in chat apps."""
    s = CONTROL.sub("", s).replace("<!", "‹!").replace("<@", "‹@")
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def render(value, ctx):
    """Fill {tokens} in every text of a validated value. A text that is exactly one number or date token becomes that number or date. Missing fields become empty text."""
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if not isinstance(value, str) or not TOKEN_RE.search(value):
        return value
    only = TOKEN_RE.fullmatch(value.strip())
    if only and isinstance(ctx.get(only.group(1)), (int, float)) and not isinstance(ctx.get(only.group(1)), bool):
        return ctx[only.group(1)]
    out = TOKEN_RE.sub(lambda m: str(ctx.get(m.group(1)) if ctx.get(m.group(1)) is not None else "")[:MAX_TOKEN_VALUE], value)
    return neutralise(out)[:MAX_RENDERED]


def step_hash(app, action_id, version_id, cleaned):
    """What a test run vouches for. Any change to the app, action, version or values makes the step untested again."""
    return hashlib.sha256(json.dumps([app, action_id, version_id, cleaned], sort_keys=True, default=str).encode()).hexdigest()


# ---------------- stored steps ----------------
def _row_view(r):
    cleaned = json.loads(r["input_json"] or "{}")
    current = step_hash(r["app_slug"], r["action_id"], r["action_version_id"], cleaned)
    return {"id": r["id"], "position": r["position"], "app": r["app_slug"], "app_name": r["app_name"], "action_id": r["action_id"], "action_name": r["action_name"], "label": r["label"],
            "run_mode": r["run_mode"], "input": cleaned, "enabled": bool(r["enabled"]), "tested": bool(r["tested_hash"]) and r["tested_hash"] == current, "tested_at": r["tested_at"],
            "destructive": vcat.is_destructive(r["action_name"] or "")}


def list_steps(store, code):
    return [_row_view(r) for r in store.all("SELECT * FROM type_steps WHERE type_code=? ORDER BY position", (code,))]


def close_when_done(store, code):
    return store.get_setting(f"steps_close:{code}", "0") == "1"


def seed_demo(store, d):
    """The demo company shows one example step on software seats. It is read-only and never runs: the demo does not call other apps (the run engine records it as not sent)."""
    if store.one("SELECT 1 x FROM type_steps LIMIT 1"):
        return
    cat = vcat.FakeCatalog()
    aid = next(a["action_id"] for a in cat.app("google-sheets")["actions"] if a["name"] == "Add New Row to Sheet")
    row = {"spreadsheet_Id": "demo-seats-sheet", "grid_Id": "0", "column_key": True, "column_selected": ["Name", "Email", "Tool"],
           "column_name": {"Name": "{requester.name}", "Email": "{requester.email}", "Tool": "{detail.tool}"}}
    save_steps(store, d, "software_seat", [{"app": "google-sheets", "action_id": aid, "run_mode": "auto", "label": "Add a row to the seats sheet", "input": row}], cat, "demo")
    r = store.one("SELECT * FROM type_steps")
    store.q("UPDATE type_steps SET tested_hash=?, tested_at=?, enabled=1, expect_json=? WHERE id=?",
            (step_hash(r["app_slug"], r["action_id"], r["action_version_id"], json.loads(r["input_json"])), time.time(), json.dumps({"kind": "object", "keys": ["Name", "Email", "Tool"]}), r["id"]))


def view(d, store, code):
    ok, why = eligible(d, code)
    return {"code": code, "eligible": ok, "reason": why, "max_steps": MAX_STEPS, "close_when_done": close_when_done(store, code), "steps": list_steps(store, code),
            "tokens": token_list(code) if ok else []}


def validate_steps(d, code, steps, catalog):
    """Check a whole list of steps for one request type. Returns the rows to store (without ids and test results)."""
    ok, why = eligible(d, code)
    if not ok:
        raise StepError(why, 404 if why.startswith("No such") else 422)
    if steps and d.types[code].get("custom") and not d.types[code].get("requires_approval", True):
        raise StepError("Switch approval on for this request type before adding steps: an app action must not run with no one deciding.")
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise StepError(f"A request type can have up to {MAX_STEPS} steps.")
    allowed, rows = tokens_for(code), []
    for i, s in enumerate(steps, 1):
        where = f"Step {i}: "
        try:
            app, action = find_action(catalog, str(s.get("app") or ""), str(s.get("action_id") or ""))
            if not app["requires_auth"]:
                raise StepError("Apps that need no sign-in are not supported yet.")
            if not action["supported"]:
                raise StepError("This action cannot be set up in Deskhand yet (" + "; ".join(action["reasons"]) + ").")
            if s.get("run_mode") not in RUN_MODES:
                raise StepError("Choose whether the step runs by itself or when the approver presses Run.")
            if action["destructive"] and s["run_mode"] != "confirm":
                raise StepError("This action deletes or removes things, so it can only run when the approver presses Run.")
            cleaned, errors, _dropped, _unreadable = validate_values(action["schema"], s.get("input") or {}, allowed)
            if errors:
                raise StepError("; ".join(list(errors.values())[:3]))
            label = _clean(s.get("label") or action["name"], 1, 60, "The step name")
        except SetupError as e:
            raise StepError(where + str(e), 422 if e.status == 404 else e.status)
        rows.append({"id": s.get("id") if isinstance(s.get("id"), int) else None, "app_slug": app["slug"], "app_name": app["name"], "action_id": action["action_id"],
                     "action_name": action["name"], "action_version_id": action["action_version_id"], "label": label, "run_mode": s["run_mode"], "input": cleaned})
    return rows


def save_steps(store, d, code, steps, catalog, user, close=None):
    """Replace a type's steps with a validated list. A step keeps its test result only if it is exactly what was tested and is still the same row. Returns what changed."""
    rows = validate_steps(d, code, steps, catalog)
    before = list_steps(store, code)
    old = {r["id"]: r for r in store.all("SELECT * FROM type_steps WHERE type_code=?", (code,))}
    now = time.time()
    with store.lock:
        store.conn.execute("DELETE FROM type_steps WHERE type_code=?", (code,))
        for pos, r in enumerate(rows):
            prev = old.get(r["id"]) if r["id"] else None
            h = step_hash(r["app_slug"], r["action_id"], r["action_version_id"], r["input"])
            same = bool(prev and prev["tested_hash"] == h)
            store.conn.execute(
                "INSERT INTO type_steps(type_code,position,app_slug,app_name,action_id,action_name,action_version_id,label,run_mode,input_json,expect_json,tested_hash,tested_at,enabled,updated_at,updated_by)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, pos, r["app_slug"], r["app_name"], r["action_id"], r["action_name"], r["action_version_id"], r["label"], r["run_mode"], json.dumps(r["input"], ensure_ascii=False),
                 prev["expect_json"] if same else None, prev["tested_hash"] if same else None, prev["tested_at"] if same else None, int(bool(same and prev["enabled"])), now, user))
        store.conn.commit()
    after = list_steps(store, code)
    changes = {"steps": [_brief(before), _brief(after)]} if _brief(before) != _brief(after) else {}
    if close is not None and bool(close) != close_when_done(store, code):
        changes["close_when_done"] = bool(close)
    if close is not None:
        store.set_setting(f"steps_close:{code}", "1" if close else "0")
    return changes


def _brief(steps):
    return [f"{s['app_name']}: {s['action_name']} ({s['run_mode']})" for s in steps]


def set_enabled(store, code, step_id, on):
    row = store.one("SELECT * FROM type_steps WHERE id=? AND type_code=?", (step_id, code))
    if not row:
        raise StepError("No such step.", 404)
    if on:
        v = _row_view(row)
        if not v["tested"]:
            raise StepError("Run a test of this exact step first. A step that has not been tested, or was changed since, cannot be switched on.")
    store.q("UPDATE type_steps SET enabled=? WHERE id=?", (int(bool(on)), step_id))
    return {"enabled": [not on, bool(on)], "step": row["label"]}
