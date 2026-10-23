"""The emails Deskhand sends, as pure functions: no database, no network. The chat text of a message (the outbox `body`) stays as it is for Slack, the in-app list and the log.
An email row also stores a small structured payload when it is written; the HTML is built from it when the email is sent, so a retry sends exactly the same email.

Everything that came from a person (names, the request text, a reason) is HTML-escaped on the way out. The only link in an email is the one the caller passes in (Deskhand's own address for that
request). An AI-written sentence is accepted only if `valid_sentence` says it is plain, short and grounded in the facts it was given; otherwise the template sentence is used (rules 1, 2 and 9)."""
import html
import json
import re

from .viasocket_adapter import _ID_LIKE

RICH = {"approval_request", "reminder", "escalation_1", "escalation_2"}  # these carry the details table and the original message
HEADLINE = {
    "approval_request": "Approval needed", "reminder": "Reminder: still waiting for your decision", "escalation_1": "Escalated to you: it needs a decision",
    "escalation_2": "Second escalation: it needs a decision", "escalated": "Your request was escalated", "approved": "Your request was approved",
    "rejected": "Your request was rejected", "fulfilled": "Your request is done", "task": "A task for you", "parked": "Needs attention",
}
BUTTON = {"approval_request": "Review and decide", "reminder": "Review and decide", "escalation_1": "Review and decide", "escalation_2": "Review and decide", "task": "Open the task"}
SHORT_OPEN = "Open the request"
MAX_MESSAGE = 1200

INK, MUTED, RULE, ACCENT, PAGE, CARD = "#161922", "#566075", "#dfe2e9", "#3a49b8", "#f5f6f9", "#ffffff"
WARN_INK, WARN_BG = "#8b5800", "#fff4de"


def esc(x):
    return html.escape(str(x if x is not None else ""), quote=True)


# ---------------- the stored payload ----------------
def build_payload(kind, *, rid, to_first, type_name, type_code, summary, requester, department, details_rows, facts, message, line, why):
    """Plain data, JSON-safe, bounded. `line` is the kind's own sentence ("Your request #12 was rejected by ... Reason: ..."); `facts` feeds the template sentence and the AI check."""
    def s(v, n):
        return str(v)[:n] if v not in (None, False) else ""
    return {
        "v": 1, "kind": kind, "rid": rid, "to_first": s(to_first, 60), "type_name": s(type_name, 80), "type_code": s(type_code, 40), "summary": s(summary, 200),
        "requester": s(requester, 120), "department": s(department, 80), "details": [[s(a, 60), s(b, 400)] for a, b in details_rows][:30],
        "facts": {k: (v if isinstance(v, (int, float)) and not isinstance(v, bool) else s(v, 120)) for k, v in (facts or {}).items() if v is not None and v != "" and v is not False},
        "message": s(message, MAX_MESSAGE), "line": s(line, 600), "why": s(why, 160),
    }


def dumps(payload):
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def loads(text):
    try:
        p = json.loads(text) if text else None
    except ValueError:
        return None
    return p if isinstance(p, dict) and p.get("v") == 1 and p.get("kind") else None


# ---------------- the sentence ----------------
def _n(x):
    return f"{x:g}" if isinstance(x, float) else str(x)


def _days(n):
    return "half a working day" if n == 0.5 else f"{_n(n)} working day{'' if n == 1 else 's'}"


def template_sentence(p):
    """One plain sentence from the facts, no model. Third person, no pronouns, no advice."""
    f, who = p.get("facts") or {}, p.get("requester") or "Someone"
    who = f"{who} ({p['department']})" if p.get("department") else who
    code = p.get("type_code")
    if code == "leave":
        kind = f"{f['leave_type']} leave" if f.get("leave_type") else "leave"
        n = f.get("days")
        s = f"{who} is asking for " + (f"{_days(n)} of {kind}" if n else kind) + (f" on {f['dates_text']}" if f.get("dates_text") else "") + "."
        if f.get("balance") is not None and f.get("after") is not None:
            s += f" The balance would go from {_n(f['balance'])} to {_n(f['after'])}."
        return s
    if code == "software_seat":
        tool = f.get("tool") or "a software"
        return f"{who} is asking for a {tool} seat" + (f" for the {f['project']} project." if f.get("project") else ".")
    if code in ("equipment", "accessories") and f.get("item"):
        return f"{who} is asking for {f['item']}."
    if code == "it_help" and f.get("issue"):
        return f"{who} needs IT help: {f['issue']}."
    if code == "access" and f.get("resource"):
        return f"{who} is asking for access to {f['resource']}."
    return f"{who} sent a request: {p.get('summary') or p.get('type_name') or 'a request'}."


FACT_KEYS = ("dates_text", "leave_type", "days", "balance", "after", "reason", "item", "tool", "project", "resource", "issue")


def facts_text(p):
    """Everything the AI may use, as one string: the validator checks the sentence against exactly this."""
    f = p.get("facts") or {}
    return " ".join(str(x) for x in [p.get("requester"), p.get("department"), p.get("type_name"), p.get("summary")] + [f.get(k) for k in FACT_KEYS] if x not in (None, ""))


def ai_facts(p):
    """What the AI writer is given: the structured facts only. Never the original message, never an address or an id."""
    f = p.get("facts") or {}
    out = {"requester": p.get("requester"), "department": p.get("department"), "request_type": p.get("type_name"), "summary": p.get("summary"), "dates": f.get("dates_text"),
           "leave_type": f.get("leave_type"), "working_days": f.get("days"), "balance_before": f.get("balance"), "balance_after": f.get("after"), "reason_given": f.get("reason"),
           "tool": f.get("tool"), "project": f.get("project"), "item": f.get("item"), "resource": f.get("resource"), "issue": f.get("issue")}
    return {k: v for k, v in out.items() if v is not None and v != ""}


FUNCTION_WORDS = {"a", "an", "the", "this", "it", "they", "their", "request", "is", "are", "for", "of", "on", "to", "from", "and", "with", "in", "at", "by"}
DENY = ("approv", "reject", "declin", "click", "link", "password", "passcode", "credential", "urgent", "immediate", "must", "should", "please", "recommend", "safe", "ignore",
        "instruction", "verify", "login", "log in", "payment", "bank", "wire ", "gift", "guarantee", "confirm your", "reply")
URLISH = re.compile(r"(?i)(https?|ftp|www\b|://|@|\b[a-z0-9-]+\.(com|org|net|io|co|in|app|dev|ai|me|info|biz|xyz)\b)|[A-Za-z]\.[A-Za-z]")
ALLOWED_PUNCT = set(" .,'’-:()/%")


def valid_sentence(sentence, facts):
    """True only for one plain sentence of 25 to 240 characters that cannot carry markup, a link, an instruction or an invented number or name."""
    if not isinstance(sentence, str):
        return False
    t = sentence.strip()
    if not 25 <= len(t) <= 240 or "\n" in t or "\r" in t or not t.endswith(".") or t.count(".") > 6:
        return False
    if any(not (c.isalpha() or c.isdigit() or c in ALLOWED_PUNCT) for c in t):
        return False
    low = t.lower()
    if URLISH.search(t) or _ID_LIKE.search(t) or any(w in low for w in DENY):
        return False
    known = {w.lower() for w in re.findall(r"[^\W_]+", str(facts))} | {x.rstrip(".,") for x in re.findall(r"\d+(?:[.,]\d+)?", str(facts))}
    if any(x.rstrip(".,") not in known for x in re.findall(r"\d+(?:[.,]\d+)?", t)):
        return False  # a number that is not in the facts
    for w in re.findall(r"[^\W\d_]+", t):
        if w[0].isupper() and w.lower() not in known and w.lower() not in FUNCTION_WORDS:
            return False  # a name or term that is not in the facts
    return True


def summary_sentence(p, ai_sentence=None):
    """(sentence, drafted_by_ai). The AI sentence is used only if it passes the check against the same facts."""
    if ai_sentence and valid_sentence(ai_sentence, facts_text(p)):
        return ai_sentence.strip(), True
    return template_sentence(p), False


# ---------------- the HTML ----------------
def _row(label, value, warn=False):
    bg, ink = (WARN_BG, WARN_INK) if warn else (CARD, INK)
    return (f'<tr><td style="padding:8px 12px;border-top:1px solid {RULE};font-size:13px;color:{MUTED};width:34%;vertical-align:top;background:{bg}">{esc(label)}</td>'
            f'<td style="padding:8px 12px;border-top:1px solid {RULE};font-size:14px;color:{ink};vertical-align:top;background:{bg};word-break:break-word">{esc(value)}</td></tr>')


def render(p, *, link, rehearsal_for=None, ai_sentence=None):
    """The email as HTML: table layout and inline styles, 600px, no images or scripts. `link` is the one address in it."""
    if not str(link).startswith(("https://", "http://")):
        raise ValueError("an email links only to Deskhand's own web address")
    kind, rich = p["kind"], p["kind"] in RICH
    sentence, by_ai = summary_sentence(p, ai_sentence if kind == "approval_request" else None)
    out = [f'<div style="background:{PAGE};padding:24px 12px;font-family:Arial,Helvetica,sans-serif;color:{INK};line-height:1.5">'
           f'<table role="presentation" align="center" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;border-collapse:collapse">']
    if rehearsal_for:
        out.append(f'<tr><td style="padding:10px 14px;margin-bottom:12px;background:{WARN_BG};color:{WARN_INK};font-size:13px;border:1px solid {RULE}">'
                   f'Rehearsal: this is the message {esc(rehearsal_for)} would get once Deskhand is live.</td></tr><tr><td style="height:12px;line-height:12px">&nbsp;</td></tr>')
    out.append(f'<tr><td style="padding:0 0 10px;font-size:13px;color:{MUTED};letter-spacing:.2px">Deskhand</td></tr>')
    out.append(f'<tr><td style="background:{CARD};border:1px solid {RULE};border-radius:12px;padding:24px">')
    out.append(f'<p style="margin:0 0 14px;font-size:15px">Hi {esc(p.get("to_first") or "there")},</p>')
    chip = (f'{p["type_name"]} #{p["rid"]}' if p.get("type_name") else f'Request #{p["rid"]}') if p.get("rid") else ""
    out.append(f'<p style="margin:0 0 16px;font-size:20px;font-weight:700;line-height:1.3">{esc(HEADLINE.get(kind, "A message from Deskhand"))}'
               + (f' <span style="display:inline-block;margin-left:6px;padding:1px 9px;border:1px solid {RULE};border-radius:999px;font-size:12px;font-weight:600;color:{MUTED};vertical-align:middle">{esc(chip)}</span>' if chip else "") + '</p>')
    if rich and kind != "approval_request" and p.get("line"):
        out.append(f'<p style="margin:0 0 16px;font-size:15px">{esc(p["line"])}</p>')
    if rich:
        out.append(f'<div style="margin:0 0 18px;padding:12px 14px;background:{PAGE};border-left:3px solid {ACCENT};border-radius:0 8px 8px 0">'
                   f'<div style="font-size:12px;color:{MUTED};margin-bottom:2px">Summary by Deskhand{" (drafted by AI from the request details)" if by_ai else ""}</div>'
                   f'<div style="font-size:15px">{esc(sentence)}</div></div>')
        if p.get("details"):
            out.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid {RULE};border-radius:8px;margin:0 0 20px">'
                       + "".join(_row(a, b, warn=a in ("Check", "Note")) for a, b in p["details"]) + "</table>")
    else:
        out.append(f'<p style="margin:0 0 20px;font-size:15px">{esc(p.get("line") or sentence)}</p>')
    label = BUTTON.get(kind, SHORT_OPEN)
    out.append(f'<p style="margin:0 0 6px"><a href="{esc(link)}" style="display:inline-block;background:{ACCENT};color:#ffffff;text-decoration:none;font-weight:700;font-size:15px;'
               f'padding:12px 22px;border-radius:8px">{esc(label)}</a></p>'
               f'<p style="margin:0 0 20px;font-size:12px;color:{MUTED};word-break:break-all">If the button does not work, open this address: {esc(link)}</p>')
    if rich and p.get("message"):
        out.append(f'<div style="font-size:12px;color:{MUTED};margin:0 0 4px">Original message</div>'
                   f'<blockquote style="margin:0 0 18px;padding:2px 0 2px 12px;border-left:2px solid {RULE};font-size:14px;color:{INK};white-space:pre-wrap;word-break:break-word">{esc(p["message"])}</blockquote>')
    if p.get("why"):
        out.append(f'<p style="margin:0;padding-top:14px;border-top:1px solid {RULE};font-size:12px;color:{MUTED}">{esc(p["why"])}</p>')
    out.append(f'</td></tr><tr><td style="padding:12px 4px 0;font-size:12px;color:{MUTED}">Sent by Deskhand.</td></tr></table></div>')
    return "".join(out)
