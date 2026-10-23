"""The browser code and the API must agree, and the browser must never say who is acting. No network."""
import os
import re
import struct
import xml.etree.ElementTree as ET

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


def _line(prefix):
    return next(l for l in JS.splitlines() if l.startswith(prefix))


def test_the_landing_page_is_a_signed_out_page_that_makes_only_the_two_calls_the_sign_in_page_makes():
    assert "'welcome'" in _line("const AUTH_PAGES") and "'welcome'" not in _line("const ADMIN_PAGES") and "'welcome'" not in _line("const APP_PAGES")
    block = JS[JS.index("async function pageWelcome()"):JS.index("async function pageLogin()")]
    assert set(re.findall(r"api\('([^']*)'", block)) == {"/auth/options", "/demo/enter"} and "fetch(" not in block and "localStorage" not in block
    demo_call = "enter(await api('/demo/enter', null))"  # the very call the sign-in page's demo button makes
    assert demo_call in block and demo_call in JS[JS.index("async function pageLogin()"):JS.index("async function pageRegister()")]


def test_a_bare_address_shows_the_overview_but_a_deep_link_still_goes_to_sign_in_and_comes_back():
    line = next(l for l in JS.splitlines() if "if (!ME && !authPage)" in l)
    assert "NEXT = location.hash" in line and "go(location.hash.replace('#', '') ? '#login' : '#welcome')" in line
    assert "route.page === 'welcome'" in next(l for l in JS.splitlines() if "if (ME && (route.page === 'login'" in l)  # a signed-in person never sees it
    assert "signedOut" in JS and "go('#login')" in JS.split("function signedOut")[1].split("}")[0]  # an expired session still lands on sign-in with its notice


def test_the_landing_page_makes_no_claim_the_product_cannot_back_up():
    block = JS[JS.index("async function pageWelcome()"):JS.index("async function pageLogin()")].lower()
    for claim in ("trusted by", "customers", "% ", "free forever", "soc 2", "iso 27001", "guarantee", "99."):
        assert claim not in block, claim


def test_the_font_is_self_hosted_checked_and_referenced_and_the_old_one_is_gone():
    css = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.css")).read()
    fonts = os.path.join(os.path.dirname(__file__), "..", "app", "static", "fonts")
    files = sorted(os.listdir(fonts))
    assert {"AtkinsonHyperlegibleNext-latin.woff2", "AtkinsonHyperlegibleNext-latin-ext.woff2", "OFL.txt"} <= set(files) and not any("Geist" in f for f in files)
    for f in files:
        if f.endswith(".woff2"):
            assert f in css and open(os.path.join(fonts, f), "rb").read(4) == b"wOF2"  # a real WOFF2 file, and the stylesheet loads it
    assert "SIL OPEN FONT LICENSE" in open(os.path.join(fonts, "OFL.txt")).read().upper()
    assert "Geist" not in css + JS + HTML and "/static/fonts/AtkinsonHyperlegibleNext-latin.woff2" in HTML  # preloaded once, from our own server (no font CDN at run time)
    assert "fonts.googleapis" not in css + HTML and "jsdelivr" not in css + JS + HTML


def test_every_line_colour_the_script_picks_is_defined_for_both_themes_and_styled():
    css = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.css")).read()
    lines = set(re.findall(r"'(\w+)'", re.search(r"const LINE = \{(.*?)\};", JS).group(1))) - {"leave", "software_seat", "access", "equipment", "accessories", "it_help", "policy_question"}
    assert lines == {"time", "tools", "gear", "help"}
    for name in lines:
        assert css.count(f"--line-{name}:") >= 3 and f".line-{name} {{ --line: var(--line-{name}); }}" in css  # day, night by system, night by choice; and the class that uses it


STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
SHAPE = re.compile(r"<(rect|circle|path)\b([^>]*?)/?>")
GEOMETRY = ("x", "y", "width", "height", "rx", "cx", "cy", "r", "d")


def _png_header(name):
    b = open(os.path.join(STATIC, name), "rb").read()
    assert b[:8] == b"\x89PNG\r\n\x1a\n" and b[12:16] == b"IHDR"  # a real PNG
    return struct.unpack(">II", b[16:24]), len(b)


def test_the_page_declares_its_icons_and_each_one_is_a_local_file():
    declared = re.findall(r'<link rel="(icon|apple-touch-icon)"([^>]*)>', HTML)
    hrefs = sorted(h for _, attrs in declared for h in re.findall(r'href="([^"]+)"', attrs))
    assert hrefs == ["/static/apple-touch-icon.png", "/static/icon-32.png", "/static/icon.svg"]
    for h in hrefs:
        assert h.startswith("/static/") and os.path.isfile(os.path.join(STATIC, h[len("/static/"):]))  # our own server, no address elsewhere
    assert '<link rel="icon" href="/static/icon.svg" type="image/svg+xml">' in HTML
    assert '<link rel="icon" href="/static/icon-32.png" sizes="32x32" type="image/png">' in HTML
    assert HTML.count('name="theme-color"') == 2 and 'media="(prefers-color-scheme: dark)"' in HTML and 'media="(prefers-color-scheme: light)"' in HTML


def test_the_svg_icon_is_plain_shapes_with_no_script_text_font_or_outside_reference():
    root = ET.parse(os.path.join(STATIC, "icon.svg")).getroot()
    assert root.get("viewBox") == "0 0 64 64"
    assert {el.tag.split("}")[1] for el in root.iter()} <= {"svg", "title", "rect", "circle", "path"}  # no script, style, text, image, use or foreignObject
    for el in root.iter():
        for key, value in el.attrib.items():
            assert key in {"viewBox", "width", "height", "rx", "cx", "cy", "r", "x", "y", "d", "fill", "stroke", "stroke-width"} and "http" not in value and "url(" not in value, (el.tag, key)  # no event handler, link or style


def test_the_png_icons_are_real_pngs_of_the_declared_size_and_small():
    for name, size in (("icon-32.png", (32, 32)), ("apple-touch-icon.png", (180, 180))):
        dims, length = _png_header(name)
        assert dims == size and length < 20_000, (name, dims, length)


def test_the_logo_is_one_drawing_in_the_icon_and_the_page_and_it_follows_the_theme():
    css = open(os.path.join(STATIC, "app.css")).read()
    symbol = re.search(r'<symbol id="logo" viewBox="0 0 64 64">(.*?)</symbol>', HTML, re.S).group(1)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", symbol) and "var(--accent)" in symbol and "var(--on-accent)" in symbol  # tile and mark take the theme's colours

    def geometry(shapes):
        return [(tag, {k: v for k, v in attrs.items() if k in GEOMETRY}) for tag, attrs in shapes]
    svg = open(os.path.join(STATIC, "icon.svg")).read().split("</title>", 1)[1]
    drawn = lambda text: [(t, dict(re.findall(r'([\w-]+)="([^"]*)"', a))) for t, a in SHAPE.findall(text)]  # noqa: E731
    assert len(drawn(svg)) == 5 and geometry(drawn(svg)) == geometry(drawn(symbol))  # the tab icon and the in-app mark are the same shapes
    mark = '<svg class="mark" aria-hidden="true"><use href="#logo"/></svg>'
    assert f'<a class="brand" href="#ask">{mark}Deskhand<span>request desk</span></a>' in HTML  # the app header
    assert f'<a class="brand" href="#welcome">{mark}Deskhand</a>' in JS  # the landing header
    assert ".brand .mark {" in css and ".brand::before" not in css  # the CSS-drawn stand-in is gone


def test_the_steps_screens_only_draw_what_the_server_says_and_never_run_text_from_an_app():
    block = JS[JS.index("/* ---------- after-approval steps (admin)"):JS.index("/* ---------- leave: balances")]
    assert "eval(" not in JS and "new Function" not in JS and "document.write" not in JS  # a form definition from the catalog is data: conditions are evaluated by the server
    assert "actor" not in block and "sender" not in block and "script_id" not in block and "auth_id" not in block.replace("auth_id: d.data", "")  # no identity and no connection id in the page
    assert all(f'id="{i}"' in HTML for i in ("sdlg", "sd-title", "sd-intro", "sd-body", "sd-err", "sd-actions"))
    assert "CONN.pending = 'steps:'" in block and "startsWith('steps:')" in JS  # the one Connect popup handler tells a step's connection from the Integrations page's
    prompt = JS[JS.index("function dialogPrompt"):JS.index("/* ---------- routing")]
    assert "$('#dlg-title').textContent = title" in prompt and "$('#dlg-body').textContent = body" in prompt and "$('#dlg-label').textContent = label" in prompt  # a confirmation shows its words as text, never as HTML
    for catalog_text in ("n.label", "n.help", "a.name", "a.description", "it.label", "o.label", "t.label", "s.label", "s.app_name", "s.action_name"):
        for m in re.finditer(r"\$\{([^}]*" + re.escape(catalog_text) + r"[^}]*)\}", block):
            line = block[block.rfind("\n", 0, m.start()) + 1:block.find("\n", m.end())]
            assert "esc(" in m.group(1) or "dialogPrompt(" in line or "label: s.destructive" in line, m.group(0)  # whatever an app or the catalog calls things is escaped before it is drawn as HTML

