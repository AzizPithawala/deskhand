"""The browser code and the API must agree, and the browser must never say who is acting. No network."""
import os
import re

import pytest

from tests import apiutil

JS = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.js")).read()
HTML = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "index.html")).read()


ACTIONS = ("approve", "reject", "pending", "done", "cancel", "replay")  # what decide() puts in ${action}


def _api_paths():
    out = set()
    for m in re.finditer(r"api\((?:'([^']*)'|`([^`]*)`)(\s*\+)?", JS):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        for a in (ACTIONS if "${action}" in raw else (None,)):
            r = raw.replace("${action}", a) if a else raw
            path = re.sub(r"\$\{[^}]*\}", "1", r.split("?")[0]) + ("1" if m.group(3) and "?" not in r else "")
            out.add(path)
    return out


def test_every_path_the_browser_calls_is_a_real_route(tmp_path, monkeypatch):
    main = apiutil.load_app(monkeypatch, tmp_path)
    routes = [r for r in main.app.routes if hasattr(r, "path_regex")]
    paths = _api_paths()
    assert len(paths) > 25  # the scan found the calls
    missing = sorted(p for p in paths if not any(r.path_regex.match("/api" + p) for r in routes))
    assert not missing, missing


def test_the_browser_never_sends_who_is_acting():
    assert "actor_id" not in JS and "sender" not in JS.replace("the sender is the signed-in person", "")
    assert "dh_who" not in JS and "?employee=' + who" not in JS  # no remembered identity: the session decides


def test_every_post_carries_the_csrf_token_and_no_secret_is_stored_in_the_browser():
    assert "'X-CSRF-Token'" in JS
    assert "localStorage" in JS and re.findall(r"store\.(?:get|set)\('([a-z_]+)'", JS) and set(re.findall(r"store\.(?:get|set)\('([a-z_]+)'", JS)) <= {"dh_theme"}
    assert "sessionStorage" not in JS and "document.cookie" not in JS


def test_every_element_the_script_looks_up_exists_in_the_page():
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    wanted = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", JS))
    dynamic = {m for m in re.findall(r'id="([^"$]+)"', JS)} | {"clock", "reason", "reasonlabel", "reasonerr", "q", "cform", "convo", "lst", "det", "split", "af", "at", "brk", "lk", "aiblock",
                                                                  "hstrip", "hdelivery", "hconn", "hguard", "hlab", "plist", "invitebtn", "demogo", "lform", "le", "lp", "lerr", "rform", "rc", "rn",
                                                                  "re", "rp", "rk", "rerr", "jform", "jp", "jq", "jerr", "aiswitch", "ff0", "t_approver_rule", "t_backup_rule", "t_fulfiller_rule", "t_redirect"}
    unknown = sorted(w for w in wanted if w not in ids and w not in dynamic and not w.startswith(("v_", "s_", "tres_", "ff")))
    assert not unknown, unknown


def test_the_delivery_log_page_never_reads_what_a_message_said():
    block = JS[JS.index("/* ---------- delivery log (admin)"):JS.index("/* ---------- render loop")]
    assert "dlRowHTML" in block and not re.search(r"\.(text|body|subject)\b", block)  # the API sends none of it, and the page must not look for it


def test_notifications_are_in_the_menu_for_everyone_including_the_demo_and_the_log_is_admin_only():
    link = re.search(r'<a href="#notifications"[^>]*>', HTML).group(0)
    assert "data-admin" not in link and "data-real" not in link and 'id="nbadge"' in HTML
    assert "'notifications'" in JS.split("const APP_PAGES")[1].split("\n")[0] and "'notifications'" not in JS.split("const ADMIN_PAGES")[1].split("\n")[0]
    assert "'deliverylog'" in JS.split("const ADMIN_PAGES")[1].split("\n")[0]
