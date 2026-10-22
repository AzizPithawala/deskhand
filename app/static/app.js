/* Deskhand front end. No build step, no dependencies. Presentation only: every call below is an existing API route. */
(() => {
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : 'e' + Date.now() + Math.random().toString(16).slice(2));

let S = { employees: [], types: [] }, EMP = {}, TYPE = {};
let ME = null;  // the signed-in person, their company and the CSRF token. The server decides who they are; nothing here can change it.
let AUTHOPT = { registration_open: false, demo_available: false };
let NEXT = '', NOTICE = '';  // the page they wanted before sign-in; a line to show on the sign-in page
let NOW = Date.now() / 1000;
let seq = 0, current = { page: '', id: null };
let REQS = [], INBOX = [];
const SEEN = {}, PREV = {}, LOG = { chat: [] }, SEENMSG = new Set();
let CLOCKDATA = { banners: [], delivery: { used_today: 0, cap: 150, capped: false } };
const meId = () => ME.user.id;
const isAdmin = () => !!(ME && ME.is_admin);
const isDemo = () => !!(ME && ME.org.is_demo);

/* ---------- api ---------- */
async function api(path, body, retried) {
  // body undefined = GET; null = POST without a body; anything else = POST as JSON. Every POST carries the CSRF token.
  const opt = body === undefined ? undefined
    : { method: 'POST', headers: { 'X-CSRF-Token': ME ? ME.csrf : '', ...(body === null ? {} : { 'Content-Type': 'application/json' }) }, body: body === null ? undefined : JSON.stringify(body) };
  let r;
  try { r = await fetch('/api' + path, opt); } catch { const e = new Error('The server could not be reached.'); e.network = true; throw e; }
  let j = {};
  try { j = await r.json(); } catch { /* not json */ }
  if (r.status === 401 && ME && !path.startsWith('/auth/')) {
    if (!retried) {  // one 401 is not proof the session is gone: ask the server, and only if it agrees sign out. A refused request was never processed, so repeating it is safe.
      const alive = await fetch('/api/auth/me').then(x => (x.ok ? x.json() : null)).catch(() => null);
      if (alive) { ME = alive; return api(path, body, true); }
    }
    signedOut('Your session ended. Sign in again.');
  }
  if (!r.ok) { const e = new Error(typeof j.detail === 'string' ? j.detail : (Array.isArray(j.detail) ? 'Please check what you entered.' : (r.statusText || 'The request failed'))); e.status = r.status; throw e; }
  return j;
}

/* ---------- formatting ---------- */
const nm = id => (id === 'system' ? 'Deskhand' : (EMP[id] ? EMP[id].name : (id || 'someone')));
const first = id => nm(id).split(' ')[0];
function dur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 45) return 'under a minute';
  const m = Math.round(s / 60);
  if (m < 60) return m + ' min';
  const h = Math.floor(m / 60), mm = m % 60;
  if (h < 24) return mm ? `${h}h ${mm}m` : `${h}h`;
  const d = Math.floor(h / 24), hh = h % 24;
  return hh ? `${d}d ${hh}h` : `${d}d`;
}
const stamp = ts => new Date(ts * 1000).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC';
const nice = s => String(s || '').replace(/_/g, ' ').toLowerCase();
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const icon = (id, cls = '') => `<svg class="${cls}" aria-hidden="true" focusable="false"><use href="#${id}"/></svg>`;

/* ---------- status vocabulary ---------- */
function statusOf(r) {
  switch (r.status) {
    case 'PENDING_APPROVAL': return { c: 'pending', i: 's-pending', w: 'Pending' };
    case 'APPROVED': case 'FULFILLING': return { c: 'approved', i: 's-done', w: r.status === 'FULFILLING' ? 'Approved, in progress' : 'Approved' };
    case 'FULFILLED': case 'CLOSED': return { c: 'approved', i: 's-done', w: 'Completed' };
    case 'REJECTED': return { c: 'rejected', i: 's-rejected', w: 'Rejected' };
    case 'PARKED': return { c: 'parked', i: 's-parked', w: 'Parked' };
    case 'CANCELLED': return { c: 'neutral', i: 's-cancelled', w: 'Cancelled' };
    default: return { c: 'neutral', i: 's-upcoming', w: 'Needs detail' };
  }
}
const chip = r => { const s = statusOf(r); return `<span class="chip ${s.c}">${icon(s.i)}${s.w}</span>`; };

/* ---------- the journey ---------- */
function resolveRule(rule, emp) {
  if (!rule || !emp) return null;
  if (rule === 'manager') return EMP[emp.manager_id] || null;
  if (rule.startsWith('role:')) return S.employees.find(e => e.role === rule.slice(5) || (e.roles || []).includes(rule.slice(5))) || null;
  if (rule.startsWith('person:')) return EMP[rule.slice(7)] || null;
  return null;
}
const adminPerson = () => S.employees.find(e => e.role === 'admin') || null;

function stagesFor(r, tl) {
  const t = TYPE[r.type_code] || {};
  const ev = (type) => (tl || []).filter(e => e.type === type);
  const out = [{ k: 'asked', state: 'done', title: 'Asked', who: r.employee_id, time: r.created_at }];
  if (r.status === 'NEEDS_INFO') {
    out.push({ k: 'info', state: 'current', title: 'Needs more detail', note: 'Deskhand asked one question. Answer it in Ask and the request continues.' });
    return out;
  }
  const chain = r.chain || [];
  const approved = ev('approved_stage');
  const decisions = ev('status_change').filter(e => e.data && (e.data.to === 'REJECTED' || e.data.to === 'CANCELLED'));
  if (!chain.length) {
    out.push({ k: 'appr0', state: 'done', title: r.type_code === 'policy_question' ? 'Answered by Deskhand' : 'No approval needed' });
  }
  chain.forEach((id, i) => {
    const label = chain.length > 1 ? (i === 0 ? 'First approval' : 'Second approval') : 'Approval';
    let state = 'upcoming';
    if (r.status === 'PENDING_APPROVAL') state = i < r.chain_index ? 'done' : (i === r.chain_index ? 'current' : 'upcoming');
    else if (r.status === 'REJECTED') state = i < r.chain_index ? 'done' : (i === r.chain_index ? 'rejected' : 'upcoming');
    else if (r.status === 'CANCELLED') state = i < r.chain_index ? 'done' : (i === r.chain_index ? 'cancelled' : 'upcoming');
    else state = 'done';
    const st = { k: 'appr' + i, state, title: label, who: id };
    if (chain.length > 1 && i === 1 && t.cost_threshold) st.because = `because the cost is above ₹${Number(t.cost_threshold).toLocaleString('en-IN')}`;
    if (state === 'done' && approved[i]) { st.who = approved[i].actor; st.time = approved[i].ts; }
    if (state === 'current') {
      const holder = r.escalated_to || id;
      const since = r.escalated_to && r.escalated_at ? r.escalated_at : (r.assigned_at || r.created_at);
      st.who = holder;
      st.wait = `with ${nm(holder)}, waiting ${dur(NOW - since)}`;
      if (r.escalated_to && r.escalated_to !== id) st.note = `Escalated from ${nm(id)}. ${dur(NOW - (r.assigned_at || r.created_at))} in total.`;
      else if (r.last_reminder_at) st.note = `A reminder was sent ${dur(NOW - r.last_reminder_at)} ago.`;
    }
    if (state === 'rejected') {
      const d = decisions.find(e => e.data.to === 'REJECTED');
      if (d) { st.who = d.actor; st.time = d.ts; }
      st.note = r.rejection_reason ? `Reason: ${r.rejection_reason}` : '';
    }
    out.push(st);
  });
  if (['REJECTED', 'CANCELLED'].includes(r.status)) return out;
  const auto = t.fulfilment_kind === 'auto_action';
  const ful = r.fulfiller_id || (resolveRule(t.fulfiller_rule, EMP[r.employee_id]) || {}).id;
  if (r.type_code !== 'policy_question') {
    let state = 'upcoming';
    if (r.status === 'FULFILLING') state = 'current';
    else if (r.status === 'PARKED') state = 'parked';
    else if (['FULFILLED', 'CLOSED'].includes(r.status)) state = 'done';
    const st = { k: 'ful', state, title: auto ? cap(hrApplied()) : `Done by ${ful ? nm(ful) : 'the fulfiller'}` };
    if (state === 'current') st.note = auto ? 'Deskhand is applying it now.' : `${nm(ful)} grants access, then clicks Done.`;
    if (state === 'parked') {
      const f = ev('action_failed').slice(-1)[0];
      st.note = `${isHRDemo() ? 'The HR system did not answer after three tries' : 'It could not be recorded'}${f && f.data && f.data.error ? ` (${f.data.error})` : ''}. Nothing was lost. An admin can replay it.`;
    }
    if (state === 'done') { const f = ev('status_change').find(e => e.data && e.data.to === 'FULFILLED'); if (f) st.time = f.ts; }
    out.push(st);
    out.push({ k: 'done', state: r.status === 'CLOSED' ? 'done' : 'upcoming', title: 'Completed', note: r.status === 'CLOSED' ? '' : `${first(r.employee_id)} is told here.` });
  }
  return out;
}

function nextSentence(r) {
  const t = TYPE[r.type_code] || {};
  if (r.status === 'PENDING_APPROVAL') {
    const holder = r.escalated_to || (r.chain || [])[r.chain_index];
    const age = (NOW - (r.assigned_at || r.created_at)) / 3600;
    const parts = [];
    if (!r.last_reminder_at && age < t.reminder_hours) parts.push(`a reminder goes to ${nm(holder)} in ${dur((t.reminder_hours - age) * 3600)}`);
    if (r.escalation_level === 0) {
      let b = resolveRule(t.backup_rule, EMP[r.employee_id]);
      if (!b || b.id === holder) b = adminPerson();
      if (b) parts.push(age < t.escalate_hours ? `it escalates to ${b.name} in ${dur((t.escalate_hours - age) * 3600)}` : `it escalates to ${b.name} at the next check`);
    } else if (r.escalation_level === 1) {
      const a = adminPerson();
      const left = t.escalate_hours - (NOW - (r.escalated_at || NOW)) / 3600;
      if (a) parts.push(`it goes to ${a.name} in ${dur(Math.max(0, left) * 3600)}`);
    }
    if ((r.chain || []).length > r.chain_index + 1) parts.push(`after ${first(holder)} decides, ${nm(r.chain[r.chain_index + 1])} approves next`);
    return parts.length ? 'If nobody acts: ' + parts.join('; ') + '.' : '';
  }
  if (r.status === 'FULFILLING') return t.fulfilment_kind === 'auto_action' ? '' : 'Next: the fulfiller clicks Done and you are told here.';
  return '';
}

function sentence(r) {
  switch (r.status) {
    case 'PENDING_APPROVAL': {
      const holder = r.escalated_to || (r.chain || [])[r.chain_index];
      return `With ${nm(holder)}, waiting ${dur(NOW - ((r.escalated_to && r.escalated_at) || r.assigned_at || r.created_at))}.`;
    }
    case 'FULFILLING': return TYPE[r.type_code] && TYPE[r.type_code].fulfilment_kind === 'auto_action' ? (isHRDemo() ? 'Applying it in the HR system.' : 'Recording it in the leave record.') : 'Approved. Waiting for the fulfiller to finish.';
    case 'APPROVED': return 'Approved.';
    case 'CLOSED': case 'FULFILLED': return 'Completed.';
    case 'REJECTED': return r.rejection_reason ? `Rejected: ${r.rejection_reason}` : 'Rejected.';
    case 'PARKED': return isHRDemo() ? 'Parked: the HR system is not answering. An admin can replay it.' : 'Parked: it could not be recorded. An admin can replay it.';
    case 'CANCELLED': return 'Cancelled.';
    default: return 'Needs one more detail from the requester.';
  }
}

const NODE = { done: 's-done', current: 's-pending', upcoming: 's-upcoming', rejected: 's-rejected', parked: 's-parked', cancelled: 's-cancelled' };
const WORD = { done: 'Done', current: 'In progress', upcoming: 'Upcoming', rejected: 'Rejected', parked: 'Parked', cancelled: 'Cancelled' };

function journeyHTML(r, tl) {
  const st = stagesFor(r, tl);
  const prev = PREV[r.id];
  const states = st.map(s => s.k + ':' + s.state);
  const html = st.map((s, i) => {
    const changed = prev && prev[i] !== states[i];
    return `<li class="stage ${s.state}" aria-label="${esc(s.title)}: ${WORD[s.state]}">
      <div class="rail">${icon(NODE[s.state], 'node' + (changed ? ' pop' : ''))}<span class="thread${changed ? ' draw' : ''}"></span></div>
      <div><div class="what"><span>${esc(s.title)}</span><span class="meta">${s.because ? esc(s.because) : (s.state === 'current' ? '' : esc(WORD[s.state]))}</span></div>
        ${s.wait ? `<div class="note"><span class="wait">${esc(s.wait)}</span></div>` : ''}
        ${s.who && !s.wait && s.time ? `<div class="meta">${esc(nm(s.who))} · ${stamp(s.time)}</div>` : (s.time ? `<div class="meta">${stamp(s.time)}</div>` : '')}
        ${s.note ? `<div class="note meta">${esc(s.note)}</div>` : ''}</div></li>`;
  }).join('');
  PREV[r.id] = states;
  const nx = nextSentence(r);
  return `<ol class="journey" style="list-style:none;padding:0">${html}</ol>${nx ? `<div class="next fade">${esc(nx)}</div>` : ''}`;
}

function miniHTML(r) {
  const st = stagesFor(r, null);
  return `<div class="mini" aria-hidden="true">${st.map((s, i) => `${i ? `<span class="seg ${st[i - 1].state === 'done' ? 'done' : ''}"></span>` : ''}${icon(NODE[s.state], 'n ' + s.state)}`).join('')}</div>`;
}

/* ---------- small ui pieces ---------- */
function toast(msg, ms = 4000) {
  const t = document.createElement('div');
  t.className = 'toast fade';
  t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), ms);
}
const loading = what => `<div aria-busy="true"><p class="meta">Loading ${esc(what)}</p><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>`;
function errorState(what, err, retry) {
  const code = err.status ? ` (the server answered ${err.status})` : '';
  const id = 'r' + (++seq);
  setTimeout(() => { const b = document.getElementById(id); if (b) b.onclick = retry; }, 0);
  return `<div class="state" role="alert"><b>Could not load ${esc(what)}${code}.</b>${esc(err.network ? 'Check your connection and try again.' : (err.message || 'Try again in a moment.'))}<br><button class="btn" id="${id}">Try again</button></div>`;
}
const emptyState = (title, body) => `<div class="state"><b>${esc(title)}</b>${esc(body)}</div>`;

function dialogPrompt({ title, body = '', label = '', confirm = 'OK', match = null, danger = false }) {
  return new Promise(resolve => {
    const d = $('#dlg');
    $('#dlg-title').textContent = title;
    $('#dlg-body').textContent = body;
    const wrap = $('#dlg-field');
    wrap.hidden = !label;
    $('#dlg-label').textContent = label;
    const input = $('#dlg-input');
    input.value = '';
    $('#dlg-err').textContent = '';
    const ok = $('#dlg-ok');
    ok.textContent = confirm;
    ok.className = 'btn ' + (danger ? 'danger' : 'primary');
    let done = false;
    const finish = v => { if (done) return; done = true; d.onclose = null; resolve(v); };
    d.onclose = () => finish(null);  // Esc or any other way of closing without confirming
    $('#dlgform').onsubmit = e => {
      if (match && input.value.trim().toLowerCase() !== match) { e.preventDefault(); $('#dlg-err').textContent = `Type "${match}" to confirm.`; return; }
      finish(true);  // resolve on submit itself; do not depend on the close event arriving
    };
    $('#dlg-cancel').onclick = () => { finish(null); d.close(); };
    d.returnValue = '';
    d.showModal();
    (label ? input : ok).focus();
  });
}

/* ---------- routing ---------- */
const AUTH_PAGES = ['login', 'register', 'join'];
const APP_PAGES = ['ask', 'mine', 'needs', 'leave', 'mailbox', 'admin', 'setup', 'people', 'integrations', 'health', 'audit'];
const ADMIN_PAGES = ['admin', 'setup', 'people', 'integrations', 'health', 'audit'];
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [p, a] = h.split('/');
  if (p === 'join') return { page: 'join', id: a || null };
  if (AUTH_PAGES.includes(p)) return { page: p, id: null };
  if (p === 'inbox') { history.replaceState(null, '', '#needs'); return { page: 'needs', id: null }; }  // links in notifications: who "you" are comes from the session
  if (p === 'request' && a) return { page: 'mine', id: +a || null };
  if (p === 'setup') return { page: 'setup', id: a || null };  // a section name, not a number
  if (APP_PAGES.includes(p)) return { page: p, id: a ? (+a || null) : null };
  return { page: 'ask', id: null };
}

/* ---------- data ---------- */
async function loadCore() {
  const [st, cl] = await Promise.all([api('/state'), api('/clock')]);
  S = st; EMP = {}; TYPE = {};
  S.employees.forEach(e => { EMP[e.id] = e; });
  S.types.forEach(t => { TYPE[t.code] = t; });
  applyClock(cl);
  if (isDemo()) {
    $('#who').innerHTML = S.employees.map(e => `<option value="${esc(e.id)}">${esc(e.name)} (${esc(e.role.replace(/_/g, ' '))})</option>`).join('');
    $('#who').value = meId();
  }
}
function applyClock(cl) {
  CLOCKDATA = cl; NOW = cl.now;
  const showStatus = isAdmin() && !isDemo();  // delivery health is for the people who can fix it; the demo sends nothing
  const b = showStatus ? (cl.banners || []) : [];
  const notes = showStatus ? (cl.notes || []) : [];
  $('#banner').innerHTML = b.map(t => `<div class="b">${icon('s-alert')}<span>${esc(t)}</span></div>`).join('') + notes.map(t => `<div class="b info">${icon('s-pending')}<span>${esc(t)}</span></div>`).join('');
  const slackOk = !b.some(t => /^(Slack|viaSocket)/.test(t));
  const mailOk = !b.some(t => /^(Gmail|Notification email|viaSocket)/.test(t));
  const d = cl.delivery || { used_today: 0, cap: 150 };
  const dot = ok => `<span class="dot${ok ? '' : ' bad'}"></span>`;
  const inner = `<span class="it">${dot(slackOk)}Slack ${slackOk ? 'connected' : 'not ready'}</span><span class="it">${dot(mailOk)}Gmail ${mailOk ? 'connected' : 'not ready'}</span><span class="it">${cl.mode === 'live' ? 'Live' : 'Rehearsal'}</span><span class="it num">Sent today ${d.used_today} / ${d.cap}</span>`;
  $('#status').innerHTML = inner;
  $('#statuspanel').innerHTML = inner;
  $('#status').hidden = !showStatus; $('#statusbtn').hidden = !showStatus;
  if (!showStatus) $('#statuspanel').hidden = true;
  $('#statusdot').className = 'dot' + (slackOk && mailOk && !d.capped ? '' : ' bad');
  const c = $('#clock'); if (c) c.textContent = stamp(cl.now);
}
async function pollCore() {
  try { applyClock(await api('/clock')); } catch { /* shown by page-level states */ }
}
async function loadLists() {
  [REQS, INBOX] = await Promise.all([api('/requests?employee=' + encodeURIComponent(meId())), api('/inbox')]);
  const b = $('#badge');
  b.hidden = !INBOX.length; b.textContent = INBOX.length;
  b.setAttribute('aria-label', plural(INBOX.length, 'item') + ' waiting for you');
}

/* ---------- motion: remember statuses, flag what changed ---------- */
function diffStatuses(rows) {
  const changed = new Set();
  rows.forEach(r => {
    const sig = [r.status, r.chain_index, r.escalated_to, r.last_reminder_at].join('|');
    if (SEEN[r.id] !== undefined && SEEN[r.id] !== sig) changed.add(r.id);
    SEEN[r.id] = sig;
  });
  return changed;
}
function flash(changed) {
  changed.forEach(id => $$(`[data-req="${id}"]`).forEach(el => {
    el.classList.add('is-changed');
    setTimeout(() => el.classList.add('out'), 1000);
    setTimeout(() => el.classList.remove('is-changed', 'out'), 1400);
  }));
}

/* ---------- pages ---------- */
const SAMPLES = ['I need Friday and Monday off, family function.', 'Need a Claude seat for my work on the research project.', 'I need a second monitor for my desk.',
  'VPN is not connecting since morning.', 'Why is my salary lower this month?', 'New joiner Rohan starts Monday. Needs a laptop and a Slack account.'];

function reqCard(r) {
  return `<div class="reqcard" data-req="${r.id}"><div class="h"><b>${esc(r.summary)}</b>${chip(r)}</div>${miniHTML(r)}<div class="meta">${esc(sentence(r))}</div><div style="margin-top:6px"><a href="#request/${r.id}">Open request ${r.id}</a></div></div>`;
}

async function pageAsk() {
  const m = $('#main');
  m.innerHTML = `<div class="ask"><h1 class="display">What do you need?</h1><p class="lede">Say it in one sentence. Deskhand finds the right person and keeps you posted.</p>
    <form class="composer" id="cform"><label class="sr" for="q">Ask Deskhand</label><input class="field" id="q" placeholder="Ask for something in one sentence" autocomplete="off"><button class="btn primary" type="submit">Send</button></form>
    <div class="chips" aria-label="Examples">${SAMPLES.map(s => `<button type="button" data-s="${esc(s)}">${esc(s)}</button>`).join('')}</div>
    <div class="convo" id="convo" aria-live="polite"></div></div>`;
  $$('.chips button', m).forEach(b => b.onclick = () => { const q = $('#q'); q.value = b.dataset.s; q.focus(); });
  $('#cform').onsubmit = async e => { e.preventDefault(); await sendMsg(); };
  await updateAsk();
}
async function updateAsk() {
  if (current.page !== 'ask') return;
  const rows = (await api('/outbox?limit=200')).reverse();
  rows.filter(r => r.channel === 'chat' && !SEENMSG.has(r.id)).forEach(r => { SEENMSG.add(r.id); LOG.chat.push({ who: 'bot', id: r.id, text: r.body, ts: r.created_at }); });
  const byId = {}; REQS.forEach(r => { byId[r.id] = r; });
  // group each exchange (what you sent, the replies, the request cards) and show the newest exchange first, right under the composer
  const groups = [];
  LOG.chat.forEach(x => { if (x.who === 'me' || !groups.length) groups.push([]); groups[groups.length - 1].push(x); });
  const piece = x => {
    if (x.who === 'cards') return x.ids.map(id => byId[id] ? reqCard(byId[id]) : '').join('');
    return `<div class="msg ${x.who === 'me' ? 'me' : ''}"><div class="who">${x.who === 'me' ? 'You' : 'Deskhand'}</div><div class="tx">${esc(x.text)}</div></div>`;
  };
  const html = groups.length ? groups.reverse().map(g => `<div class="xchg">${g.map(piece).join('')}</div>`).join('')
    : emptyState('Nothing asked yet', 'Type a request above, or pick an example. You will see where it goes and who has it.');
  const convo = $('#convo');
  if (convo && convo.dataset.h !== html) { convo.dataset.h = html; convo.innerHTML = html; }
}
async function sendMsg() {
  const text = $('#q').value.trim();
  if (!text) { toast('Write a sentence first.'); return; }
  LOG.chat.push({ who: 'me', text });
  $('#q').value = '';
  const pending = { who: 'bot', text: 'Reading your request…' };
  LOG.chat.push(pending); updateAsk();
  try {
    const res = await api('/ingest', { text, source_event_id: uuid() });  // the sender is the signed-in person; the server ignores anything else
    await loadLists();
    if (res.request_ids && res.request_ids.length) LOG.chat.push({ who: 'cards', ids: res.request_ids });
    if (res.outcome === 'paused_breaker') toast('Intake is paused after a burst of messages. An admin resets it on Health.', 6000);
  } catch (e) { toast(e.network ? e.message : `Could not send (${e.status || 'error'}): ${e.message}`, 6000); }
  const i = LOG.chat.indexOf(pending); if (i >= 0) LOG.chat.splice(i, 1);
  await updateAsk();
}

function listRow(r, hrefBase, selected, extra = '') {
  return `<a class="item" href="#${hrefBase}/${r.id}" data-req="${r.id}" ${selected ? 'aria-current="true"' : ''}>
    <div class="t"><span class="id num">${r.id}</span><span>${esc(r.summary)}</span></div>
    <div class="s">${chip(r)}</div>${miniHTML(r)}<div class="meta">${esc(extra || sentence(r))}</div></a>`;
}

async function pageMine(id) {
  const m = $('#main');
  m.innerHTML = `<div class="page-head"><h1>My requests</h1><span class="meta">${esc(nm(meId()))}</span></div><div class="split ${id ? 'has-detail' : ''}" id="split"><div class="list" id="lst"></div><div class="detail" id="det" aria-live="polite"></div></div>`;
  paintMine(id, new Set());
  if (id) await paintDetail(id, 'mine'); else if (REQS.length && window.matchMedia('(min-width: 1000px)').matches) { await paintDetail(REQS[0].id, 'mine'); }
}
function paintMine(id, changed) {
  const lst = $('#lst'); if (!lst) return;
  lst.innerHTML = REQS.length ? REQS.map(r => listRow(r, 'mine', r.id === id)).join('') : `<div style="padding-top:16px">${emptyState('No requests yet', 'Ask for something in Ask. Each request you make is listed here with who has it.')}</div>`;
  flash(changed);
}

async function pageNeeds(id) {
  const m = $('#main');
  m.innerHTML = `<div class="page-head"><h1>Needs you</h1><span class="meta">${esc(nm(meId()))}</span></div><div class="split ${id ? 'has-detail' : ''}" id="split"><div class="list" id="lst"></div><div class="detail" id="det" aria-live="polite"></div></div>`;
  paintNeeds(id, new Set());
  const pick = id || (INBOX[0] && window.matchMedia('(min-width: 1000px)').matches ? INBOX[0].id : null);
  if (pick) await paintDetail(pick, 'needs');
  else if ($('#det')) $('#det').innerHTML = '';
}
const KIND = { approval: 'Needs your decision', task: 'Needs you to finish it', parked: 'Parked: needs an admin' };
function paintNeeds(id, changed) {
  const lst = $('#lst'); if (!lst) return;
  lst.innerHTML = INBOX.length ? INBOX.map(r => `<a class="item" href="#needs/${r.id}" data-req="${r.id}" ${r.id === id ? 'aria-current="true"' : ''}>
      <div class="t"><span class="id num">${r.id}</span><span>${esc(r.summary)}</span></div>
      <div class="s meta">${esc(r.employee_name)} · ${esc(KIND[r.inbox_kind] || '')}</div><div class="meta">${esc(sentence(r))}</div></a>`).join('')
    : `<div style="padding-top:16px">${emptyState('Nothing waiting for you', 'New requests assigned to you appear here within a few seconds.')}</div>`;
  flash(changed);
}

function whatWasUnderstood(r) {
  const rows = (r.details_view || []);
  return rows.length ? `<dl class="kv">${rows.map(([a, b]) => `<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join('')}</dl>` : '<p class="meta">Deskhand found no structured details in the message. The original is below.</p>';
}
const REC = {
  request_created: 'Request created', approved_stage: d => `Approved (step ${d.stage} of ${d.of})`, reminder_sent: 'Reminder sent',
  escalated: d => `Escalated to ${nm(d.to)}`, action_succeeded: 'HR action succeeded', action_retry: 'HR action retried', action_failed: 'HR action failed',
  marked_pending: d => `Marked pending${d.note ? ': ' + d.note : ''}`, injection_suspected: 'Instructions inside the message were ignored',
  duplicate_suppressed: 'Duplicate delivery ignored', delivery_succeeded: 'Notification delivered', delivery_failed: 'Notification could not be delivered',
  delivery_retry: 'Notification delivery retried', delivery_replayed: 'Notification retried by an admin', replayed: 'Replayed by an admin',
  unauthorized_action_refused: 'An action by someone not allowed to do it was refused', status_change: d => `${nice(d.from)} to ${nice(d.to)}${d.why ? ': ' + d.why : ''}`,
};
const recLine = e => { const f = REC[e.type]; return f ? (typeof f === 'function' ? f(e.data || {}) : f) : nice(e.type); };

async function paintDetail(id, base) {
  const det = $('#det'); if (!det) return;
  det.innerHTML = loading('request ' + id);
  let d;
  try { d = await api('/requests/' + id); } catch (e) { det.innerHTML = errorState('request ' + id, e, () => paintDetail(id, base)); return; }
  const r = d.request;
  diffStatuses([r]);
  const act = INBOX.find(x => x.id === id);
  det.dataset.id = id;
  det.innerHTML = `<div data-req="${r.id}">
    <a class="back" href="#${base}">Back to the list</a>
    <div class="page-head" style="margin-top:12px"><h1>${esc(r.summary)}</h1>${chip(r)}</div>
    <p class="meta">Request ${r.id} · ${esc(nm(r.employee_id))}${EMP[r.employee_id] ? ', ' + esc(EMP[r.employee_id].department) : ''} · via ${esc(r.channel)} · asked ${stamp(r.created_at)}</p>
    <section aria-label="Journey of this request">${journeyHTML(r, d.timeline)}</section>
    ${act ? decisionHTML(act) : ''}
    <section class="sec"><h3>What Deskhand understood</h3>${whatWasUnderstood(r)}</section>
    <section class="sec"><h3>${r.email_subject ? 'Original email' : 'Original message'}</h3>${r.email_subject ? `<p class="meta">Subject: ${esc(r.email_subject)}</p>` : ''}<div class="quote">${esc(r.message)}</div></section>
    <section class="sec"><h3>Record</h3><p class="meta">Every change is written once and never edited.</p><ul class="record">${d.timeline.map(e => `<li><span class="meta">${new Date(e.ts * 1000).toLocaleTimeString('en-GB', { timeZone: 'UTC' })}</span><span>${esc(recLine(e))} <span class="meta">· ${esc(nm(e.actor))}</span></span></li>`).join('')}</ul></section></div>`;
  wireDecision(r);
}

function decisionHTML(it) {
  if (it.inbox_kind === 'approval') return `<div class="decide" data-id="${it.id}" data-mode="default">
      <div class="reason" hidden><label class="l" for="reason" id="reasonlabel">Reason (sent to ${esc(first(it.employee_id))})</label><textarea class="field" id="reason" rows="3"></textarea><div class="meta" id="reasonerr" role="alert" style="color:var(--rejected)"></div></div>
      <div class="row2"><button class="btn danger" data-do="reject">Reject</button><button class="btn primary" data-do="approve">Approve</button></div>
      <div class="alt"><button class="btn small quiet" data-do="pend">Mark pending</button><button class="btn" data-do="cancel" hidden>Cancel</button><button class="btn" data-do="send" hidden>Send</button></div></div>`;
  if (it.inbox_kind === 'task') return `<div class="decide" data-id="${it.id}"><div class="row2" style="grid-template-columns:1fr"><button class="btn primary" data-do="done">Mark as done</button></div><p class="meta" style="margin:10px 0 0">Never send credentials through chat or email. Grant access in the tool, then mark it done.</p></div>`;
  return `<div class="decide" data-id="${it.id}"><div class="row2" style="grid-template-columns:1fr"><button class="btn primary" data-do="replay">Replay</button></div><p class="meta" style="margin:10px 0 0">Replaying repeats the failed step once. It cannot create a duplicate.</p></div>`;
}
function wireDecision(r) {
  const bar = $('.decide'); if (!bar) return;
  const id = +bar.dataset.id;
  const setMode = (mode) => {
    bar.dataset.mode = mode;
    const reason = $('.reason', bar);
    if (reason) reason.hidden = mode === 'default';
    $$('[data-do]', bar).forEach(b => {
      const k = b.dataset.do;
      if (k === 'send' || k === 'cancel') b.hidden = mode === 'default';
      if (k === 'reject' || k === 'approve' || k === 'pend') b.hidden = mode !== 'default';
    });
    const row = $('.row2', bar); if (row && mode !== 'default') row.hidden = true; else if (row) row.hidden = false;
    if (mode !== 'default') {
      $('#reasonlabel').textContent = mode === 'reject' ? `Reason for rejecting (required; sent to ${first(r.employee_id)})` : 'Note for the requester (optional)';
      $('[data-do=send]', bar).textContent = mode === 'reject' ? 'Send rejection' : 'Send note';
      $('#reason').focus();
    }
  };
  bar.onclick = async e => {
    const b = e.target.closest('[data-do]'); if (!b) return;
    const k = b.dataset.do;
    if (k === 'reject') return setMode('reject');
    if (k === 'pend') return setMode('pend');
    if (k === 'cancel') return setMode('default');
    if (k === 'send') {
      const text = $('#reason').value.trim();
      if (bar.dataset.mode === 'reject' && !text) { $('#reasonerr').textContent = 'A reason is required to reject.'; $('#reason').focus(); return; }
      return decide(id, bar.dataset.mode === 'reject' ? 'reject' : 'pending', bar.dataset.mode === 'reject' ? { reason: text } : { note: text });
    }
    return decide(id, k);
  };
}
async function decide(id, action, extra) {
  $$('.decide button').forEach(b => { b.disabled = true; });
  try {
    await api(`/requests/${id}/${action}`, { ...(extra || {}) });
    toast({ approve: 'Approved. The requester is told.', reject: 'Rejected. The reason is sent to the requester.', pending: 'Marked pending. The requester is told.', done: 'Marked done. The requester is told.', replay: 'Replayed.' }[action] || 'Done.');
  } catch (e) { toast(`${e.message}`, 6000); }
  await refreshAll(false, true);
}

/* ---------- health ---------- */
const LAB = [
  ['duplicate_x4', 'The same message arrives four times', 'Exactly one request is created. The three duplicates are recorded and dropped.'],
  ['own_message', 'Deskhand hears its own reply', 'It is ignored, so it can never answer itself in a loop.'],
  ['burst', 'A flood of messages in one minute', 'The breaker opens, intake pauses and the admin is told.'],
  ['hr_fail_twice', 'The HR system fails twice, then recovers', 'The leave is applied exactly once.'],
  ['hr_timeout_after_success', 'HR applies the leave but the reply is lost', 'The retry finds the leave that already exists. No second leave.'],
  ['hr_down_park', 'The HR system is down', 'The request is parked, not lost, and the admin is told. Then bring HR back and replay: still one leave.'],
  ['ai_budget', 'The AI budget runs out', 'Requests keep working with the built-in rules.'],
];
const CONN = { token: null, info: {}, pending: null };
const STATUSLBL = { connected: 'Connected', not_connected: 'Not connected', expired: 'Expired: reconnect', unknown: 'Connected, status unknown' };

async function pageHealth() {
  const m = $('#main');
  m.innerHTML = `<div class="page-head"><h1>Health</h1><span class="meta">Is everything working, and can it be proven?</span></div>
    <div id="hstrip">${loading('health')}</div>
    <div class="block" id="hdelivery"></div><div class="block" id="hguard"></div><div class="block" id="hlab"></div>`;
  let dash;
  try { dash = await api('/dashboard'); } catch (e) { $('#hstrip').innerHTML = errorState('health', e, pageHealth); return; }
  const g = await api('/digest').catch(() => null);
  let dv = null; try { dv = await api('/delivery'); } catch { /* admin only */ }
  const dd = dash.delivery || { used_today: 0, cap: 150, ready: {} };
  const working = dd.ready && dd.ready.slack && dd.ready.email;
  const cell = (v, l, dim, cls = '') => `<div class="${dim ? 'dim' : ''}"><div class="v ${cls}">${v}</div><div class="meta">${esc(l)}</div></div>`;
  $('#hstrip').innerHTML = `<div class="grid">${isDemo() ? cell(icon('s-upcoming') + 'Off', 'the demo never sends messages', true) : cell(working ? icon('s-done') + 'Working' : icon('s-pending') + 'Not ready', 'delivery to Slack and email', false, working ? 'ok' : 'warn')}${cell(`${dd.used_today} / ${dd.cap}`, 'delivered today (daily cap)')}${cell(dash.total, 'requests')}${cell(dash.avg_decision_minutes == null ? '–' : (dash.avg_decision_minutes < 1 ? 'under 1' : dash.avg_decision_minutes), 'average minutes to a decision')}${cell(dash.hr_effects, 'leaves applied in HR')}</div>`;
  paintDelivery(dv);
  const HEAD = ['duplicate_suppressed', 'own_message_ignored', 'breaker_opened', 'action_retry', 'action_failed', 'blocked_sensitive'];
  const rest = Object.keys(dash.guard).filter(k => !HEAD.includes(k));
  const gc = k => cell(dash.guard[k], nice(k), !dash.guard[k]);
  $('#hguard').innerHTML = `<h2>Reliability counters</h2><p class="meta">What the guard has caught. A count above zero is the guard doing its job.</p>
    <div class="grid" style="margin-top:12px">${HEAD.map(gc).join('')}</div><details><summary>${rest.length} more counters</summary><div class="grid">${rest.map(gc).join('')}</div></details>
    ${dash.breaker_open.length ? `<p><b>Intake is paused on: ${esc(dash.breaker_open.join(', '))}.</b> <button class="btn small" id="brk">Reset the breaker</button></p>` : ''}
    ${g ? `<h3 style="margin-top:20px">Weekly digest preview for ${esc(g.person)}</h3><dl class="kv" style="margin-top:8px"><dt>Approved</dt><dd>${g.as_approver.approved}</dd><dt>Rejected</dt><dd>${g.as_approver.rejected}</dd><dt>Pending</dt><dd>${g.as_approver.pending}</dd><dt>Oldest pending</dt><dd>${g.as_approver.oldest_pending_hours} h</dd></dl>
    ${g.as_employee.length ? `<div class="scroll"><table class="t" style="margin-top:8px"><tr><th>Request</th><th>Type</th><th>Status</th></tr>${g.as_employee.map(x => `<tr><td class="num">${x.id}</td><td>${esc(nice(x.type))}</td><td>${esc(x.status)}</td></tr>`).join('')}</table></div>` : '<p class="meta" style="margin-top:8px">No open requests of their own.</p>'}` : ''}`;
  const brk = $('#brk'); if (brk) brk.onclick = async () => { try { await api('/admin/breaker_reset', {}); toast('Breaker reset.'); } catch (e) { toast(e.message, 6000); } pageHealth(); };
  if (!isDemo()) $('#hlab').hidden = true;
  else $('#hlab').innerHTML = `<h2>Reliability lab</h2><p class="meta">Each button breaks something on purpose and shows whether the guard held. Runs use Neha Gupta's requests and never send to Slack or email.</p>
    ${LAB.map(([k, t, p]) => `<div class="labrow"><div><b>${esc(t)}</b></div><div><p class="meta">${esc(p)}</p><button class="btn small" data-lab="${k}">Run</button><div class="verdict-box" id="v_${k}" aria-live="polite"></div></div></div>`).join('')}`;
  $$('[data-lab]').forEach(b => b.onclick = () => runLab(b.dataset.lab));
}
function paintDelivery(dv) {
  const el = $('#hdelivery'); if (!el) return;
  if (isDemo()) { el.innerHTML = '<h2>Delivery</h2><p class="meta">The demo company never sends real messages. Create your own company to connect Slack and Gmail.</p>'; return; }
  if (!dv) { el.innerHTML = `<h2>Delivery</h2>${emptyState('Delivery details are for admins', 'Only company admins can see them.')}`; return; }
  const cnt = Object.entries(dv.counts).map(([k, v]) => `${k} ${v}`).join(', ') || 'none yet';
  el.innerHTML = `<h2>Delivery</h2><p class="meta">Messages go out by themselves once Slack and Gmail are connected on the <a href="#integrations">Integrations</a> page. Approvals never wait for delivery.</p>
    <dl class="kv" style="margin-top:8px"><dt>Slack</dt><dd>${dv.ready.slack ? 'Ready' : 'Not ready'}</dd><dt>Email</dt><dd>${dv.ready.email ? 'Ready' : 'Not ready'}</dd><dt>Mode</dt><dd>${dv.mode === 'live' ? 'Live: to the real people' : `Rehearsal: only to ${esc((dv.rehearsal_person || {}).name || 'an admin')}`}</dd><dt>Delivered today</dt><dd class="num">${dv.used_today} of ${dv.cap}${dv.capped ? ' (cap reached)' : ''}</dd><dt>Outbox</dt><dd>${esc(cnt)}</dd><dt>Links point to</dt><dd>${esc(dv.base_url)}</dd></dl>
    ${dv.failed.length ? `<h3 style="margin-top:16px">Failed deliveries</h3>${dv.failed.map(f => `<div class="approw"><span style="flex:1">#${f.id} · ${esc(nice(f.kind))} (${esc(f.channel)}) · ${esc(f.delivery_error || '')}</span><button class="btn small" data-retry="${f.id}">Retry</button></div>`).join('')}<p class="meta">Check the channel before retrying: a failed send may already have arrived.</p>` : ''}`;
  $$('[data-retry]', el).forEach(b => b.onclick = async () => { try { await api('/delivery/retry/' + b.dataset.retry, {}); toast('Retried.'); } catch (e) { toast(e.message, 6000); } pageHealth(); });
}
function verdictHTML(r) {
  return `<ul class="verdict"><li class="${r.passed ? 'ok' : 'no'}">${icon(r.passed ? 's-check' : 's-cross')}<b>${r.passed ? 'The guard held' : 'The guard failed'}</b></li>${r.steps.map(s => `<li class="meta" style="margin-left:24px">${esc(s)}</li>`).join('')}${Object.entries(r.checks).map(([c, v]) => `<li class="${v ? 'ok' : 'no'}">${icon(v ? 's-check' : 's-cross')}<span style="color:var(--ink)">${esc(c)}${v ? '' : ' (failed)'}</span></li>`).join('')}</ul>${r.replay_id ? `<p style="margin-top:8px"><button class="btn primary small" data-replay="${r.replay_id}">Bring HR back and replay #${r.replay_id}</button></p>` : ''}`;
}
async function runLab(k) {
  const box = $('#v_' + k); box.innerHTML = '<p class="meta">Running</p>';
  try { const r = await api('/chaos/' + k, {}); box.innerHTML = verdictHTML(r); wireReplay(box); } catch (e) { box.innerHTML = `<p role="alert" style="color:var(--rejected)">Could not run it (${e.status || 'error'}): ${esc(e.message)}</p>`; }
  pollCore();
}
function wireReplay(box) {
  $$('[data-replay]', box).forEach(b => b.onclick = async () => { try { box.innerHTML = verdictHTML(await api('/chaos/replay/' + b.dataset.replay, {})); } catch (e) { toast(e.message, 6000); } });
}

/* connections (admin) */
async function paintConnections() {
  const el = $('#hconn'); if (!el) return;
  if (isDemo()) { el.innerHTML = `<h2>Connections</h2>${emptyState('Connections are switched off in the demo company', 'Create your own company to connect Slack and Gmail and receive real notifications.')}`; return; }
  let c;
  try { c = await api('/connections'); } catch (e) {
    el.innerHTML = `<h2>Connections</h2>${e.status === 403 ? emptyState('Connections are for admins', 'Only company admins can set up Slack and Gmail.') : errorState('connections', e, paintConnections)}`; return;
  }
  if (c.configured) await prepConnect(c);
  el.innerHTML = `<h2>Connections</h2><p class="meta">You sign in on each app's own screen; Deskhand never sees a password or token. Slack messages each person by their own Slack account. Gmail sends email from the company mailbox; anyone can connect their own on Your mailbox so emails about their requests come from them.</p>
    ${!c.configured ? emptyState('viaSocket is not configured on the server', 'Connecting is switched off until the server has its viaSocket values. See the README.') : ''}
    ${c.note ? `<p role="alert" style="color:var(--rejected)">Could not read connection status: ${esc(c.note)}</p>` : ''}
    ${c.apps.map(a => appRow(a, c)).join('')}<div id="aiblock"></div>`;
  paintAI(c.configured);
  for (const a of c.apps) if (a.can_run) for (const st of a.settings) if (st.kind === 'picker') loadPicker(a, st);
  wireConnections(el);
}
async function paintAI(configured) {
  const el = $('#aiblock'); if (!el) return;
  let ai; try { ai = await api('/ai'); } catch { return; }
  el.innerHTML = `<div class="approw" style="display:block"><div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><b style="flex:1">Understanding requests</b>
    <span class="chip ${ai.enabled ? 'approved' : 'neutral'}">${icon(ai.enabled ? 's-done' : 's-upcoming')}${ai.enabled ? 'viaSocket AI is on' : 'Built-in rules'}</span></div>
    <p class="meta" style="margin:6px 0 8px;max-width:640px">By default Deskhand reads each message with its built-in rules. Switch viaSocket AI on and each message is sent to a Claude model to work out the request type and details. It waits at most ${ai.timeout_s} seconds and uses the rules if the AI is slow, fails or runs out of credits. Salary and payroll messages are never sent. The AI only proposes: approvers and approvals always come from your routing map.</p>
    <label style="display:flex;gap:10px;align-items:center;min-height:44px;cursor:pointer"><input type="checkbox" id="aiswitch" ${ai.switch ? 'checked' : ''} ${configured ? '' : 'disabled'} style="width:20px;height:20px"> Use viaSocket AI to understand requests</label></div>`;
  $('#aiswitch').onchange = async e => {
    try { await api('/ai', { enabled: e.target.checked }); toast(e.target.checked ? 'viaSocket AI is on.' : 'Using the built-in rules.'); }
    catch (err) { toast(err.message, 6000); e.target.checked = !e.target.checked; }
    paintAI(configured);
  };
}
function appRow(a, c) {
  const setting = x => `<div class="setrow"><label class="meta" for="s_${a.key}_${x.key}">${esc(x.label)}${x.required ? ' (required)' : ' (optional)'}</label>${x.kind === 'picker'
    ? `<select class="field" id="s_${a.key}_${x.key}" data-pick="${a.key}:${x.key}"><option>Loading</option></select><span></span>`
    : `<input class="field" id="s_${a.key}_${x.key}" value="${esc(x.value)}" placeholder="name@example.com" inputmode="email" autocomplete="off"><button class="btn small" data-save="${a.key}:${x.key}">Save</button>`}</div>`;
  return `<div class="approw" style="display:block"><div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><img src="${esc(a.icon)}" alt="" width="28" height="28"><b style="flex:1">${esc(a.name)}</b>
    <span class="chip ${a.status === 'connected' ? 'approved' : (a.status === 'not_connected' ? 'neutral' : 'pending')}">${icon(a.status === 'connected' ? 's-done' : (a.status === 'not_connected' ? 's-upcoming' : 's-pending'))}${esc(STATUSLBL[a.status] || a.status)}</span>
    <button class="btn small primary" data-connect="${a.key}" ${c.configured ? '' : 'disabled'}>${a.status === 'not_connected' ? 'Connect ' + esc(a.name) : 'Reconnect'}</button>${a.can_run ? `<button class="btn small danger" data-disc="${a.key}">Disconnect</button>` : ''}</div>
    ${a.can_run ? a.settings.map(setting).join('') + `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px">${a.key === 'slack'
      ? `<button class="btn small" data-test="slack_send">Send me a test Slack message</button><input class="field" id="lk" placeholder="email to look up" style="width:220px"><button class="btn small" data-test="slack_lookup">Look up a user</button>`
      : `<button class="btn small" data-test="gmail_send">Send a test email to me</button>`}</div><div id="tres_${a.key}" aria-live="polite"></div>` : ''}</div>`;
}
function wireConnections(el) {
  $$('[data-connect]', el).forEach(b => b.onclick = () => connectApp(b.dataset.connect));
  $$('[data-disc]', el).forEach(b => b.onclick = () => disconnectApp(b.dataset.disc));
  $$('[data-save]', el).forEach(b => b.onclick = () => { const [k, s] = b.dataset.save.split(':'); saveSetting(k, s, $(`#s_${k}_${s}`).value); });
  $$('[data-pick]', el).forEach(sel => sel.onchange = () => { const [k, s] = sel.dataset.pick.split(':'); saveSetting(k, s, sel.value); });
  $$('[data-test]', el).forEach(b => b.onclick = () => testConn(b.dataset.test));
}
async function prepConnect(c) {
  try {
    CONN.token = (await api('/connections/token')).token;
    for (const a of c.apps) CONN.info[a.key] = await api(`/connections/service/${a.key}`);
    await loadConnectScript(c.connect_js);
  } catch (e) { toast(e.message, 6000); }
}
function connectApp(key) {
  const info = CONN.info[key];
  if (!info || !CONN.token || !window.openViasocketConnection) { toast('Connecting is not ready yet. Reload the page and try again.'); return; }
  CONN.pending = key;
  window.openViasocketConnection(CONN.token, info.service_id, { filteredActions: info.action_ids, skipActionSelection: true });
}
window.addEventListener('message', async ev => {
  const d = ev.data; if (!d || typeof d !== 'object') return;
  if (d.type === 'viasocket_connection_success') {
    const key = CONN.pending || Object.keys(CONN.info).find(k => CONN.info[k].service_id === d.serviceId);
    try { await api(key === 'mine' ? '/me/connections/connected' : '/connections/connected', key === 'mine' ? { auth_id: d.data && d.data.id } : { app: key, auth_id: d.data && d.data.id }); toast('Connected.'); } catch (e) { toast(e.message, 6000); }
    if (current.page === 'integrations') pageIntegrations(); else if (current.page === 'mailbox') pageMailbox();
  } else if (d.type === 'viasocket_connection_error') toast((d.error && d.error.message) || 'The connection did not complete.', 6000);
});
async function loadPicker(a, x) {
  const el = document.getElementById(`s_${a.key}_${x.key}`); if (!el) return;
  try {
    const o = await api(`/connections/${a.key}/options?setting=${x.key}`);
    el.innerHTML = (x.required ? '<option value="">Choose one</option>' : '<option value="">Use the connected account</option>') + o.map(v => `<option value="${esc(v.value)}">${esc(v.label)}</option>`).join('');
    el.value = x.value || '';
  } catch (e) { el.innerHTML = `<option value="">${esc(e.message)}</option>`; }
}
async function saveSetting(key, setting, value) {
  try { await api(`/connections/${key}/setting`, { setting, value }); toast('Saved.'); await pollCore(); } catch (e) { toast(e.message, 6000); }
}
async function disconnectApp(key) {
  if (!await dialogPrompt({ title: `Disconnect ${key}?`, body: 'Notifications that need this app stop being delivered until it is connected again.', confirm: 'Disconnect', danger: true })) return;
  try { await api(`/connections/${key}/disconnect`, {}); toast('Disconnected.'); } catch (e) { toast(e.message, 6000); }
  await pollCore(); pageIntegrations();
}
async function testConn(action) {
  const key = action.startsWith('slack') ? 'slack' : 'gmail';
  const box = $('#tres_' + key); box.innerHTML = '<p class="meta">Running</p>';
  try {
    const r = await api('/connections/test', { action, email: action === 'slack_lookup' ? ($('#lk').value || '') : null });
    const ok = r.outcome === 'success';
    box.innerHTML = `<ul class="verdict"><li class="${ok ? 'ok' : 'no'}">${icon(ok ? 's-check' : 's-cross')}<b>${ok ? 'It worked' : esc(nice(r.outcome))}</b></li>${r.error ? `<li class="meta" style="margin-left:24px">${esc(r.error)}</li>` : ''}${r.data ? `<li class="meta" style="margin-left:24px">${esc(Object.entries(r.data).map(([k, v]) => `${k}: ${v}`).join(' · '))}</li>` : ''}</ul>`;
  } catch (e) { box.innerHTML = `<p role="alert" style="color:var(--rejected)">${esc(e.message)}</p>`; }
}

/* ---------- audit ---------- */
async function pageAudit() {
  const m = $('#main');
  m.innerHTML = `<div class="page-head"><h1>Audit</h1><span class="meta">Append-only: every change, who made it, when.</span></div><div style="max-width:420px;margin-bottom:12px"><label class="l" for="af">Filter</label><input class="field" id="af" placeholder="Request number, person or event"></div><div id="at">${loading('the audit record')}</div>`;
  let rows;
  try { rows = await api('/audit?limit=300'); } catch (e) { $('#at').innerHTML = errorState('the audit record', e, pageAudit); return; }
  const draw = () => {
    const q = $('#af').value.trim().toLowerCase();
    const list = rows.filter(e => !q || `${e.request_id ?? ''} ${nm(e.actor)} ${nice(e.type)}`.toLowerCase().includes(q));
    $('#at').innerHTML = list.length ? `<div class="scroll"><table class="t"><tr><th class="num">#</th><th>Request</th><th>Who</th><th>What</th><th>Detail</th></tr>${list.map(e => {
      let det = ''; try { const o = JSON.parse(e.data_json || '{}'); det = Object.entries(o).filter(([, v]) => v !== '' && v !== null && v !== undefined).map(([k, v]) => `${nice(k)}: ${typeof v === 'object' ? JSON.stringify(v) : (EMP[v] ? nm(v) : v)}`).join(' · '); } catch { /* ignore */ }
      return `<tr><td class="num">${e.id}</td><td class="num">${e.request_id ? `<a href="#request/${e.request_id}">${e.request_id}</a>` : ''}</td><td>${esc(nm(e.actor))}</td><td>${esc(nice(e.type))}</td><td class="meta">${esc(det)}</td></tr>`;
    }).join('')}</table></div>` : emptyState('No events match', q ? 'Clear the filter to see every event.' : 'Events appear here as soon as something happens.');
  };
  $('#af').oninput = draw; draw();
}

/* ---------- accounts: sign in, register, join, account menu ---------- */
const ACCESS = { owner: 'Owner', admin: 'Admin', member: 'Member' };
const initials = n => String(n || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('');
function go(h) { if (location.hash === h) return render(false); location.hash = h; }  // a changed hash renders by itself
function resetPerson() {
  REQS = []; INBOX = []; LOG.chat = []; SEENMSG.clear();
  [SEEN, PREV].forEach(o => Object.keys(o).forEach(k => delete o[k]));
}
function signedOut(msg) {
  NEXT = /^#(login|register|join)/.test(location.hash) ? '' : location.hash;
  ME = null; resetPerson(); NOTICE = msg || ''; paintShell(); go('#login');
}
function paintShell() {
  const inn = !!ME && !AUTH_PAGES.includes(current.page);  // sign-in, register and join stand alone, even if someone is signed in
  $('#shell').classList.toggle('solo', !inn);
  $('#nav').hidden = !inn;
  $$('#nav [data-admin]').forEach(a => { a.hidden = !isAdmin(); });
  $$('#nav [data-real]').forEach(a => { a.hidden = isDemo(); });
  $('#demobtn').hidden = !(inn && isDemo());
  const ab = $('#acctbtn'); ab.hidden = !inn;
  if (inn) {
    ab.setAttribute('aria-label', `Account: ${ME.user.name}, ${ME.org.name}`);
    ab.innerHTML = `<span class="av" aria-hidden="true">${esc(initials(ME.user.name))}</span><span class="acct-name">${esc(ME.user.name)}<span class="meta"> · ${esc(ME.org.name)}</span></span>`;
  } else { $('#status').hidden = true; $('#statusbtn').hidden = true; $('#statuspanel').hidden = true; $('#banner').innerHTML = ''; }
}
async function enter(me) {
  ME = me; resetPerson();
  const to = NEXT || '#ask'; NEXT = '';
  try { await loadCore(); } catch (e) { toast(e.message, 6000); }
  paintShell();
  history.replaceState(null, '', to);
  await render(true);
}
function authShell(title, lede, inner) {
  $('#main').innerHTML = `<div class="auth"><h1 class="display">${esc(title)}</h1>${lede ? `<p class="lede">${esc(lede)}</p>` : ''}${inner}</div>`;
}
const fld = (id, label, type = 'text', extra = '', hint = '') => `<div><label class="l" for="${id}">${esc(label)}</label><input class="field" id="${id}" type="${type}" ${extra}>${hint ? `<div class="meta">${esc(hint)}</div>` : ''}</div>`;
function submitForm(form, errId, work) {
  form.onsubmit = async e => {
    e.preventDefault();
    const btn = $('button[type=submit]', form); btn.disabled = true; $(errId).textContent = '';
    try { await work(); } catch (err) { $(errId).textContent = err.message; btn.disabled = false; }
  };
}

async function pageLogin() {
  try { AUTHOPT = await api('/auth/options'); } catch { /* keep what we knew */ }
  const notice = NOTICE; NOTICE = '';
  authShell('Sign in', 'Use the email and password for your company.', `
    ${notice ? `<div class="state" role="status" style="margin-bottom:16px">${esc(notice)}</div>` : ''}
    <form class="stack authform" id="lform" novalidate>${fld('le', 'Email', 'email', 'autocomplete="username"')}${fld('lp', 'Password', 'password', 'autocomplete="current-password"')}
      <div class="err" id="lerr" role="alert"></div><button class="btn primary" type="submit">Sign in</button></form>
    <div class="altlinks">${AUTHOPT.registration_open ? '<a href="#register">Create a company</a>' : ''}${AUTHOPT.demo_available ? '<button class="btn" id="demogo" type="button">Try the demo</button>' : ''}</div>
    <p class="meta">Forgot your password? Ask your company admin for a new link.</p>`);
  submitForm($('#lform'), '#lerr', async () => { await enter(await api('/auth/login', { email: $('#le').value.trim(), password: $('#lp').value })); });
  const d = $('#demogo'); if (d) d.onclick = async () => { d.disabled = true; try { await enter(await api('/demo/enter', null)); } catch (e) { toast(e.message, 6000); d.disabled = false; } };
  $('#le').focus();
}
async function pageRegister() {
  try { AUTHOPT = await api('/auth/options'); } catch { /* keep what we knew */ }
  if (!AUTHOPT.registration_open) { authShell('Registration is closed', 'New companies join by invitation.', '<p>Ask us for an invite code, then come back here.</p><p><a href="#login">Back to sign in</a></p>'); return; }
  authShell('Create your company', 'You become its owner and can invite everyone else.', `
    <form class="stack authform" id="rform" novalidate>${fld('rc', 'Company name', 'text', 'autocomplete="organization"')}${fld('rn', 'Your name', 'text', 'autocomplete="name"')}${fld('re', 'Your work email', 'email', 'autocomplete="username"')}
      ${fld('rp', 'Password', 'password', 'autocomplete="new-password"', 'At least 10 characters.')}${fld('rk', 'Invite code', 'text', 'autocomplete="off"', 'The code we gave you.')}
      <div class="err" id="rerr" role="alert"></div><button class="btn primary" type="submit">Create company</button></form>
    <p class="meta"><a href="#login">Already have an account? Sign in</a></p>`);
  submitForm($('#rform'), '#rerr', async () => {
    const tz = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return null; } })();
    await enter(await api('/auth/register', { company: $('#rc').value, name: $('#rn').value, email: $('#re').value.trim(), password: $('#rp').value, code: $('#rk').value.trim(), timezone: tz }));
  });
  $('#rc').focus();
}
async function pageJoin(token) {
  let info;
  try { info = await api('/auth/invite/' + encodeURIComponent(token || '')); } catch (e) {
    authShell('This link no longer works', '', `<div class="state" role="alert"><b>${esc(e.message)}</b></div><p style="margin-top:16px"><a href="#login">Go to sign in</a></p>`); return;
  }
  const reset = info.kind === 'reset';
  authShell(reset ? 'Choose a new password' : `Join ${info.company}`, reset ? `For ${info.email}.` : `Hi ${info.name}. Choose a password for ${info.email}.`, `
    <form class="stack authform" id="jform" novalidate>${fld('jp', 'Password', 'password', 'autocomplete="new-password"', 'At least 10 characters.')}${fld('jq', 'Password again', 'password', 'autocomplete="new-password"')}
      <div class="err" id="jerr" role="alert"></div><button class="btn primary" type="submit">${reset ? 'Save password and sign in' : 'Join and sign in'}</button></form>`);
  submitForm($('#jform'), '#jerr', async () => {
    if ($('#jp').value !== $('#jq').value) throw new Error('The two passwords do not match.');
    await enter(await api('/auth/join', { token, password: $('#jp').value }));
  });
  $('#jp').focus();
}

function openAccount() {
  const u = ME.user;
  $('#acct-id').innerHTML = `<dt>Name</dt><dd>${esc(u.name)}</dd><dt>Email</dt><dd>${esc(u.email)}</dd><dt>Company</dt><dd>${esc(ME.org.name)}</dd><dt>Access</dt><dd>${esc(ACCESS[u.access_role] || u.access_role)}</dd>`;
  $('#theme').value = store.get('dh_theme') || 'system';
  $('#pwbox').hidden = isDemo(); $('#mbline').hidden = isDemo(); $('#pwbox').open = false; $('#pwform').reset(); $('#pwerr').textContent = '';
  $('#acct').showModal();
}
async function changePassword(e) {
  e.preventDefault();
  const err = $('#pwerr'); err.textContent = '';
  if ($('#pw1').value !== $('#pw2').value) { err.textContent = 'The two new passwords do not match.'; return; }
  try { ME = await api('/auth/password', { current: $('#pw0').value, new: $('#pw1').value }); toast('Password changed. Other devices are signed out.', 5000); $('#acct').close(); }
  catch (x) { err.textContent = x.message; }
}
async function signOut() {
  $('#acct').close();
  try { await api('/auth/logout', null); } catch { /* the cookie is cleared by the server; if it was not reachable the session simply expires */ }
  NEXT = ''; ME = null; resetPerson(); NOTICE = 'You are signed out.'; paintShell(); go('#login');
}

/* ---------- people (admin) ---------- */
let PEOPLE = [];
const PSTATUS = { active: ['approved', 's-done', 'Active'], invited: ['pending', 's-pending', 'Invited, not joined yet'], disabled: ['neutral', 's-cancelled', 'Deactivated'], demo: ['neutral', 's-upcoming', 'Demo person'] };
const pchip = s => { const [c, i, w] = PSTATUS[s] || ['neutral', 's-upcoming', s]; return `<span class="chip ${c}">${icon(i)}${esc(w)}</span>`; };
const pname = id => { const p = PEOPLE.find(x => x.id === id); return p ? p.name : '–'; };

function formDialog({ title, intro = '', fields, confirm = 'Save', submit }) {
  const d = $('#fdlg');
  $('#fdlg-title').textContent = title; $('#fdlg-intro').textContent = intro; $('#fdlg-err').textContent = '';
  $('#fdlg-fields').innerHTML = fields.map((f, i) => `<div><label class="l" for="ff${i}">${esc(f.label)}</label>${f.options
    ? `<select class="field" id="ff${i}" ${f.disabled ? 'disabled' : ''}>${f.options.map(([v, l]) => `<option value="${esc(v)}" ${v === (f.value ?? '') ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`
    : (f.type === 'textarea' ? `<textarea class="field" id="ff${i}" rows="5" ${f.disabled ? 'disabled' : ''}>${esc(f.value ?? '')}</textarea>` : `<input class="field" id="ff${i}" type="${f.type || 'text'}" value="${esc(f.value ?? '')}" autocomplete="off" ${f.disabled ? 'disabled' : ''}>`)}${f.hint ? `<div class="meta">${esc(f.hint)}</div>` : ''}</div>`).join('');
  const ok = $('#fdlg-ok'); ok.textContent = confirm; ok.disabled = false;
  $('#fdlg-cancel').onclick = () => d.close();
  $('#fdlgform').onsubmit = async e => {
    e.preventDefault();
    const vals = {}; fields.forEach((f, i) => { vals[f.key] = $('#ff' + i).value; });
    ok.disabled = true;
    try { await submit(vals); d.close(); } catch (err) { $('#fdlg-err').textContent = err.message; ok.disabled = false; }
  };
  d.showModal();
  const first = $('#ff0'); if (first && !first.disabled) first.focus();
}
function showLink(title, body, token) {
  const d = $('#ldlg'); const link = `${location.origin}/#join/${token}`;
  $('#ldlg-title').textContent = title; $('#ldlg-body').textContent = body; $('#ldlg-link').value = link;
  $('#ldlg-copy').textContent = 'Copy link';
  $('#ldlg-copy').onclick = async () => {
    try { await navigator.clipboard.writeText(link); $('#ldlg-copy').textContent = 'Copied'; }
    catch { $('#ldlg-link').select(); $('#ldlg-copy').textContent = 'Press Ctrl+C or ⌘C to copy'; }
  };
  $('#ldlg-close').onclick = () => { $('#ldlg-link').value = ''; d.close(); };
  d.onclose = () => { $('#ldlg-link').value = ''; };
  d.showModal(); $('#ldlg-link').select();
}
const managerOptions = (selfId) => [['', 'No manager']].concat(PEOPLE.filter(p => p.id !== selfId && ['active', 'invited', 'demo'].includes(p.status)).map(p => [p.id, p.name]));

async function pagePeople() {
  const m = $('#main');
  m.innerHTML = `<div class="page-head"><h1>People</h1><span class="meta">Who works here, who they report to, and what they can do.</span></div>
    <div class="peoplebar">${isDemo() ? '<span class="meta">The demo company\'s people are fixed. Create your own company to invite people.</span>' : '<button class="btn primary" id="invitebtn">Invite a person</button><span class="meta">Each person gets a link to copy and send. Requests go to their manager, or to an admin if they have none.</span>'}</div>
    <div id="plist">${loading('people')}</div>`;
  let rows;
  try { rows = await api('/people'); } catch (e) { $('#plist').innerHTML = errorState('people', e, pagePeople); return; }
  PEOPLE = rows;
  const act = p => isDemo() ? '' : `<button class="btn small" data-edit="${esc(p.id)}">Edit</button>${['active', 'invited'].includes(p.status) ? `<button class="btn small" data-link="${esc(p.id)}">${p.status === 'invited' ? 'New invite link' : 'Password reset link'}</button>` : ''}`;
  $('#plist').innerHTML = `<div class="scroll"><table class="t people"><tr><th>Person</th><th>Access</th><th>Reports to</th><th>Department</th><th>Business roles</th><th>Slack</th><th>Status</th><th></th></tr>${rows.map(p =>
    `<tr><td class="pname"><b>${esc(p.name)}${p.id === meId() ? ' (you)' : ''}</b><span class="meta">${esc(p.email)}</span></td><td data-l="Access">${esc(ACCESS[p.access_role] || p.access_role)}</td><td data-l="Reports to">${esc(pname(p.manager_id))}</td><td data-l="Department">${esc(p.department || '–')}</td><td data-l="Business roles">${esc((p.roles || []).join(', ') || '–')}</td><td data-l="Slack">${p.slack_id ? 'Matched' : 'Not matched'}</td><td>${pchip(p.status)}</td><td class="pact">${act(p)}</td></tr>`).join('')}</table></div>`;
  const inv = $('#invitebtn'); if (inv) inv.onclick = invitePerson;
  $$('[data-edit]').forEach(b => b.onclick = () => editPerson(b.dataset.edit));
  $$('[data-link]').forEach(b => b.onclick = () => personLink(b.dataset.link));
}
function invitePerson() {
  formDialog({
    title: 'Invite a person', intro: 'They get a link to choose a password. Nothing is emailed yet: you copy the link and send it yourself.', confirm: 'Create invite',
    fields: [{ key: 'name', label: 'Name' }, { key: 'email', label: 'Work email', type: 'email' }, { key: 'access_role', label: 'Access', options: [['member', 'Member: asks for things and approves what is assigned to them'], ['admin', 'Admin: sees everything and manages people and apps']], value: 'member' },
      { key: 'department', label: 'Department (optional)' }, { key: 'manager_id', label: 'Reports to', options: managerOptions(null), value: '' }],
    submit: async v => {
      const r = await api('/people/invite', { name: v.name, email: v.email.trim(), access_role: v.access_role, department: v.department || null, manager_id: v.manager_id || null });
      await loadCore(); await pagePeople();
      showLink(`Invite for ${r.user.name}`, `Send this link to ${r.user.name} (${r.user.email}).`, r.join_token);
    },
  });
}
function editPerson(id) {
  const p = PEOPLE.find(x => x.id === id); if (!p) return;
  const owner = p.access_role === 'owner', pending = p.status === 'invited';
  formDialog({
    title: `Edit ${p.name}`, intro: owner ? 'The owner cannot be demoted or deactivated. Keep a second admin in case the owner is locked out.' : (pending ? 'This person has not joined yet.' : ''),
    fields: [{ key: 'name', label: 'Name', value: p.name }, { key: 'department', label: 'Department', value: p.department || '' }, { key: 'slack_id', label: 'Slack user id (optional)', value: p.slack_id || '', hint: 'Starts with U. "Match Slack accounts" on Integrations fills this from email.' },
      { key: 'manager_id', label: 'Reports to', options: managerOptions(p.id), value: p.manager_id || '' },
      { key: 'access_role', label: 'Access', options: owner ? [['owner', 'Owner']] : [['member', 'Member'], ['admin', 'Admin']], value: p.access_role, disabled: owner },
      { key: 'status', label: 'Account', options: pending ? [['invited', 'Invited, not joined yet']] : [['active', 'Active'], ['disabled', 'Deactivated: signed out and cannot sign in']], value: pending ? 'invited' : p.status, disabled: owner || pending }],
    submit: async v => {
      const changed = {};
      ['name', 'department', 'slack_id', 'manager_id', 'access_role', 'status'].forEach(k => { if ((v[k] || '') !== (p[k] || '')) changed[k] = v[k] || null; });
      if (!Object.keys(changed).length) return;
      await api('/people/' + encodeURIComponent(id), changed);
      toast('Saved.'); await loadCore(); await pagePeople();
    },
  });
}
async function personLink(id) {
  const p = PEOPLE.find(x => x.id === id); if (!p) return;
  const ok = await dialogPrompt({ title: p.status === 'invited' ? 'Make a new invite link?' : `Make a password reset link for ${p.name}?`, body: p.status === 'invited' ? 'The earlier invite link stops working.' : `${p.name} is signed out everywhere when the link is used. Earlier links stop working.`, confirm: 'Make the link' });
  if (!ok) return;
  try { const r = await api(`/people/${encodeURIComponent(id)}/link`, null); showLink(p.status === 'invited' ? `Invite for ${p.name}` : `Password reset for ${p.name}`, `Send this link to ${p.name} (${p.email}).`, r.join_token); }
  catch (e) { toast(e.message, 6000); }
}

/* ---------- admin hub, setup and integrations ---------- */
const ADMIN_LINKS = [['setup', 'Setup', 'Roles, request types, policies and a routing test.'], ['people', 'People', 'Invite people, set managers and access.'],
  ['integrations', 'Integrations', 'Connect Slack and Gmail; switch viaSocket AI.'], ['health', 'Health', 'Is everything working, and can it be proven?'], ['audit', 'Audit', 'Every change, who made it, when.']];
const amount = n => `₹${Number(n).toLocaleString('en-IN')}`;
const afterSetup = async () => { try { await loadCore(); } catch { /* the page shows its own errors */ } };  // people, roles and rules feed the journeys and the status bar

async function pageAdmin() {
  let note = '';
  if (!isDemo()) {
    try { const c = await api('/setup/checklist'); if (!c.dismissed && c.done < c.total) note = `<div class="state" style="margin-bottom:16px"><b>Company setup: ${c.done} of ${c.total} steps done</b><a href="#setup">Continue setting up</a></div>`; } catch { /* the hub still works without it */ }
  }
  $('#main').innerHTML = `<div class="page-head"><h1>Admin</h1><span class="meta">${esc(ME.org.name)}</span></div>${note}
    <div class="hub">${ADMIN_LINKS.map(([k, t, d]) => `<a href="#${k}"><b>${esc(t)}</b><span class="meta">${esc(d)}</span></a>`).join('')}</div>`;
}

async function pageIntegrations() {
  $('#main').innerHTML = `<div class="page-head"><h1>Integrations</h1><span class="meta">Slack and Gmail, connected once for the whole company.</span></div><div class="block" id="hmsg" style="border-top:0;margin-top:0;padding-top:0"></div><div class="block" id="hconn"></div>`;
  await paintMessaging();
  await paintConnections();
}

const SETUP_TABS = [['', 'Overview'], ['roles', 'Roles'], ['types', 'Request types'], ['policies', 'Policies'], ['leave', 'Leave'], ['test', 'Routing test']];
let TD = { types: [], people: [], role_options: [] };  // the last request-type listing, also used by the editor's pickers
async function pageSetup(section) {
  const sec = SETUP_TABS.some(t => t[0] === (section || '')) ? (section || '') : '';
  $('#main').innerHTML = `<div class="page-head"><h1>Setup</h1><span class="meta">${isDemo() ? 'The demo company\'s rules are prepared and read-only.' : 'How requests are routed in your company.'}</span></div>
    <nav class="tabs" aria-label="Setup sections">${SETUP_TABS.map(([k, l]) => `<a href="#setup${k ? '/' + k : ''}" ${k === sec ? 'aria-current="page"' : ''}>${esc(l)}</a>`).join('')}</nav><div id="sbody">${loading('setup')}</div>`;
  const fn = { '': setupOverview, roles: setupRoles, types: setupTypes, policies: setupPolicies, leave: setupLeave, test: setupTest }[sec];
  try { await fn(); } catch (e) { $('#sbody').innerHTML = errorState('setup', e, () => pageSetup(section)); }
}

async function setupOverview() {
  const c = await api('/setup/checklist');
  if (c.demo) { $('#sbody').innerHTML = '<div class="state"><b>This is the demo company</b>Its roles and rules are prepared and cannot be changed. Read them in the sections above and try the routing test. Create your own company to set up yours.</div>'; return; }
  $('#sbody').innerHTML = `<h2>Get your company ready <span class="meta num" style="margin-left:8px">${c.done} of ${c.total} done</span></h2>
    <p class="meta" style="max-width:640px">Each step ticks itself when it is true. Nothing here blocks anyone from asking for things.</p>
    <ol class="checklist">${c.steps.map(s => `<li class="${s.done ? 'done' : ''}">${icon(s.done ? 's-done' : 's-upcoming')}<div><a href="${esc(s.link)}"><b>${esc(s.title)}</b></a><div class="meta">${esc(s.detail)}${s.done ? ' · done' : ''}</div></div></li>`).join('')}</ol>
    ${c.done === c.total && !c.dismissed ? '<button class="btn" id="chkdone">Hide the reminder on the Admin page</button>' : ''}`;
  const b = $('#chkdone'); if (b) b.onclick = async () => { try { await api('/setup/checklist/dismiss', {}); toast('Hidden.'); setupOverview(); } catch (e) { toast(e.message, 6000); } };
}

/* roles */
async function setupRoles() {
  const d = await api('/setup/roles'), ro = d.demo;
  const holderCell = r => {
    const opts = d.people.map(p => `<option value="${esc(p.id)}" ${p.id === r.holder_id ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
    const gone = r.holder_state === 'inactive' ? `<option value="${esc(r.holder_id)}" selected>${esc(r.holder_name)} (deactivated)</option>` : '';
    const warn = r.holder_state === 'inactive' ? 'Deactivated: this role is effectively empty.' : (r.holder_state === 'none' ? 'No holder yet.' : '');
    return `<select class="field" data-holder="${esc(r.code)}" aria-label="Who holds ${esc(r.name)}" ${ro ? 'disabled' : ''}><option value="">No one</option>${gone}${opts}</select>${warn ? `<div class="meta warn">${esc(warn)}</div>` : ''}`;
  };
  $('#sbody').innerHTML = `<p class="meta" style="max-width:640px">A business role is a job such as IT admin. Choose who holds it, then request types can say "the IT admin approves". One person holds a role at a time. If nobody does, requests that need it go to the type's backup, then to an admin.</p>
    <div class="scroll"><table class="t roles"><tr><th>Role</th><th>Held by</th><th>Used by</th><th></th></tr>${d.roles.map(r =>
      `<tr><td><b>${esc(r.name)}</b>${r.builtin ? '' : ' <span class="meta">custom</span>'}</td><td>${holderCell(r)}</td><td class="meta">${esc(r.used_by.join(', ') || 'No request type')}</td>
       <td>${!ro && !r.builtin ? `<button class="btn small danger" data-delrole="${esc(r.code)}">Delete</button>` : ''}</td></tr>`).join('')}</table></div>
    ${ro ? '' : '<p style="margin-top:16px"><button class="btn" id="addrole">Add a role</button></p>'}`;
  $$('[data-holder]').forEach(sel => sel.onchange = async () => {
    try { await api('/setup/roles/' + encodeURIComponent(sel.dataset.holder), { holder_id: sel.value || null }); toast('Saved.'); await afterSetup(); } catch (e) { toast(e.message, 6000); }
    setupRoles();
  });
  $$('[data-delrole]').forEach(b => b.onclick = async () => {
    if (!await dialogPrompt({ title: 'Delete this role?', body: 'It is removed. A role that a request type still uses cannot be deleted.', confirm: 'Delete', danger: true })) return;
    try { await api(`/setup/roles/${encodeURIComponent(b.dataset.delrole)}/delete`, {}); toast('Deleted.'); await afterSetup(); } catch (e) { toast(e.message, 6000); }
    setupRoles();
  });
  const add = $('#addrole'); if (add) add.onclick = () => formDialog({ title: 'Add a role', intro: 'For example Facilities lead. Then pick who holds it and use it in a request type.', confirm: 'Add role',
    fields: [{ key: 'name', label: 'Role name' }], submit: async v => { await api('/setup/roles', { name: v.name }); toast('Added.'); await afterSetup(); setupRoles(); } });
}

/* request types */
const whenText = (t, w) => {
  if (!w) return 'always';
  if (w.cost_above != null) return `the cost is above ${/INR/.test(t.currency || '') ? amount(w.cost_above) : Number(w.cost_above).toLocaleString('en-IN')}`;
  const dc = w.detail_contains || {};
  return `the ${nice(dc.field)} mentions ${(dc.words || []).join(', ')}`;
};
function typeLines(t) {
  const mid = x => String(x).replace(/^The requester/, 'the requester').replace(/^An admin/, 'an admin');  // read mid-sentence
  const L = r => esc(mid(((t.labels || {})[r] || {}).label || (r ? r : 'No one')));
  if (t.blocked) return [`Never processed. The employee is told to contact ${L(t.redirect)} privately.`];
  if (t.code === 'policy_question') return ['Answered by Deskhand from your policies. No approval.'];
  const out = [];
  if (!t.requires_approval) out.push('No approval needed.');
  else out.push(`Approved by ${L(t.approver_rule || 'manager')}${t.auto_allow_up_to != null ? ` (only above ${esc(amount(t.auto_allow_up_to))}; below that it needs no approval)` : ''}.`);
  if (!t.requires_approval && t.auto_allow_up_to != null) out[0] = `No approval up to ${esc(amount(t.auto_allow_up_to))}; above that ${L(t.approver_rule || 'manager')} approves.`;
  (t.extra_approvers || []).forEach(x => out.push(`Also ${L(x.rule)} when ${esc(whenText(t, x.when))}.`));
  out.push(`Reminder after ${t.reminder_hours} h. If nobody acts, it escalates after ${t.escalate_hours} h to ${L(t.backup_rule)}${t.backup_rule ? '' : ' (an admin)'}.`);
  out.push(t.fulfilment_kind === 'auto_action' ? `Applied by Deskhand in ${isHRDemo() ? 'the HR system' : 'your leave record'}.` : `Task done by ${L(t.fulfiller_rule)}.`);
  return out;
}
const typeWarnings = t => Object.values(t.labels || {}).filter(l => !l.ok && l.rule !== 'manager').map(l => `${l.label}: ${l.note || 'cannot be found'}. Requests that need it use the backup, then an admin.`);

async function setupTypes() {
  TD = await api('/setup/types');
  const ro = TD.demo;
  $('#sbody').innerHTML = `<p class="meta" style="max-width:640px">The kinds of request Deskhand understands. For each you choose who approves, what happens if nobody acts, and who does the task. Changes apply to new requests; one already with an approver keeps its approvers.</p>
    ${TD.types.map(t => `<div class="typecard" data-type="${esc(t.code)}"><div class="h"><b>${esc(t.name)}</b>
      <span class="chip ${t.enabled ? 'approved' : 'neutral'}">${icon(t.enabled ? 's-done' : 's-cancelled')}${t.enabled ? 'On' : 'Off'}</span>
      ${t.changed.length ? '<span class="meta">edited</span>' : ''}${ro ? '' : `<span class="meta">${t.reviewed_at ? 'reviewed' : 'not reviewed yet'}</span>`}</div>
      ${typeLines(t).map(l => `<p class="sub">${l}</p>`).join('')}${typeWarnings(t).map(w => `<div class="warnrow">${icon('s-alert')}<span>${esc(w)}</span></div>`).join('')}
      ${ro ? '' : `<div class="btns"><button class="btn small" data-edit="${esc(t.code)}">Edit</button>${t.reviewed_at ? '' : `<button class="btn small" data-ok="${esc(t.code)}">Looks right</button>`}</div>`}</div>`).join('')}`;
  $$('[data-edit]').forEach(b => b.onclick = () => editType(b.dataset.edit));
  $$('[data-ok]').forEach(b => b.onclick = async () => { try { await api(`/setup/types/${encodeURIComponent(b.dataset.ok)}/reviewed`, {}); toast('Marked as reviewed.'); } catch (e) { toast(e.message, 6000); } setupTypes(); });
}

function ruleSelect(id, value, o, t) {
  const groups = [['', [...(o.none ? [['', o.none]] : []), ...(o.manager ? [['manager', "The requester's manager"]] : []), ['role:admin', 'An admin (the owner or any admin)']]],
    ['Business roles', TD.role_options.map(r => [`role:${r.code}`, r.name + (r.holder_name ? ` (${r.holder_name})` : ' (no holder)')])], ['People', TD.people.map(p => [`person:${p.id}`, p.name])]];
  const known = new Set(groups.flatMap(g => g[1].map(x => x[0])));
  if (value && !known.has(value)) groups.unshift(['', [[value, (((t.labels || {})[value] || {}).label || value) + ' (not available)']]]);
  const opt = ([v, l]) => `<option value="${esc(v)}" ${v === (value ?? '') ? 'selected' : ''}>${esc(l)}</option>`;
  return `<select class="field" id="${id}">${groups.map(([g, os]) => (g ? `<optgroup label="${esc(g)}">` : '') + os.map(opt).join('') + (g ? '</optgroup>' : '')).join('')}</select>`;
}
function extraRow(i, x, t) {
  const w = x && x.when, dc = (w && w.detail_contains) || {};
  const kind = !x ? 'always' : (!w ? 'always' : (w.cost_above != null ? 'cost' : 'words'));
  const fields = t.condition_fields || [];
  return `<div class="extrarow" data-extra="${i}"><div><label class="l" for="t_x${i}_rule">Extra approver ${i + 1}</label>${ruleSelect(`t_x${i}_rule`, x ? x.rule : '', { none: 'None', manager: true }, t)}</div>
    <div data-when><label class="l" for="t_x${i}_kind">When</label><select class="field" id="t_x${i}_kind"><option value="always" ${kind === 'always' ? 'selected' : ''}>Always</option><option value="cost" ${kind === 'cost' ? 'selected' : ''}>The cost is above</option>${fields.length ? `<option value="words" ${kind === 'words' ? 'selected' : ''}>A detail mentions</option>` : ''}</select></div>
    <div data-show="cost"><label class="l" for="t_x${i}_cost">Amount${t.currency ? ' (' + esc(t.currency) + ')' : ''}</label><input class="field" id="t_x${i}_cost" type="number" min="0" step="any" value="${w && w.cost_above != null ? esc(w.cost_above) : ''}"></div>
    <div data-show="words"><label class="l" for="t_x${i}_field">Detail</label><select class="field" id="t_x${i}_field">${fields.map(f => `<option value="${esc(f)}" ${dc.field === f ? 'selected' : ''}>${esc(nice(f))}</option>`).join('')}</select>
      <label class="l" for="t_x${i}_words" style="margin-top:8px">Words, separated by commas</label><input class="field" id="t_x${i}_words" value="${esc((dc.words || []).join(', '))}" autocomplete="off"></div></div>`;
}
function editType(code) {
  const t = TD.types.find(x => x.code === code), f = new Set(t.fields), d = $('#tdlg');
  const num = (id, label, v, extra = '') => `<div><label class="l" for="${id}">${esc(label)}</label><input class="field" id="${id}" type="number" step="any" min="0" value="${v ?? ''}" ${extra}></div>`;
  const blocks = [];
  if (f.has('enabled') && code !== 'payroll') blocks.push(`<label class="switch"><input type="checkbox" id="t_enabled" ${t.enabled ? 'checked' : ''}> This request type is switched on</label>`);
  if (f.has('requires_approval')) blocks.push(`<label class="switch"><input type="checkbox" id="t_requires_approval" ${t.requires_approval ? 'checked' : ''}> It needs approval before it is carried out</label>`);
  if (f.has('approver_rule')) blocks.push(`<div class="tdgroup"><label class="l" for="t_approver_rule">Who approves</label>${ruleSelect('t_approver_rule', t.approver_rule || 'manager', { manager: true }, t)}</div>`);
  if (f.has('auto_allow_up_to')) blocks.push(`<div class="tdgroup">${num('t_auto_allow_up_to', `Needs no approval up to (${t.currency || 'amount'})`, t.auto_allow_up_to)}<p class="meta">Above this amount the approver above must approve.</p></div>`);
  if (f.has('extra_approvers')) blocks.push(`<div class="tdgroup"><span class="l">Extra approvers (up to 2)</span><p class="meta">Added after the first approver, only when their condition holds.</p>${[0, 1].map(i => extraRow(i, (t.extra_approvers || [])[i], t)).join('')}</div>`);
  if (f.has('reminder_hours')) blocks.push(`<div class="tdgroup"><span class="l">If nobody acts</span><div class="extrarow">${num('t_reminder_hours', 'Send a reminder after (hours)', t.reminder_hours, 'min="1" max="720"')}${num('t_escalate_hours', 'Escalate after (hours)', t.escalate_hours, 'min="1" max="720"')}</div>
    <label class="l" for="t_backup_rule">Escalate to</label>${ruleSelect('t_backup_rule', t.backup_rule || '', { none: 'No one in particular: an admin', manager: true }, t)}</div>`);
  if (f.has('fulfiller_rule')) blocks.push(`<div class="tdgroup"><label class="l" for="t_fulfiller_rule">Who does the task and clicks Done</label>${ruleSelect('t_fulfiller_rule', t.fulfiller_rule, { manager: true }, t)}</div>`);
  if (f.has('redirect')) blocks.push(`<div class="tdgroup"><label class="l" for="t_redirect">Who employees are told to contact privately</label>${ruleSelect('t_redirect', t.redirect, {}, t)}<p class="meta">Salary and payroll messages are never processed, whatever you set here.</p></div>`);
  if (f.has('details_needed')) blocks.push(`<div class="tdgroup"><span class="l">Details Deskhand asks the approver to look at</span><div class="checks">${(t.details_known || []).map(k => `<label><input type="checkbox" data-detail="${esc(k)}" ${(t.details_needed || []).includes(k) ? 'checked' : ''}> ${esc(nice(k))}</label>`).join('')}</div></div>`);
  $('#td-title').textContent = `Edit ${t.name}`; $('#td-intro').textContent = t.notes || ''; $('#td-err').textContent = '';
  $('#td-fields').innerHTML = blocks.join('');
  const sync = () => $$('.extrarow[data-extra]', d).forEach(row => {
    const has = !!$('select[id$=_rule]', row).value, k = $('select[id$=_kind]', row).value;  // no approver chosen: no condition to set
    $('[data-when]', row).hidden = !has;
    $$('[data-show]', row).forEach(x => { x.hidden = !has || x.dataset.show !== k; });
  });
  $$('.extrarow[data-extra] select', d).forEach(s => { s.onchange = sync; }); sync();
  const ok = $('#td-ok'); ok.disabled = false; $('#td-reset').hidden = !t.changed.length;
  $('#td-cancel').onclick = () => d.close();
  $('#td-reset').onclick = async () => {
    if (!await dialogPrompt({ title: `Reset ${t.name} to the defaults?`, body: 'Your edits to this type are removed. Requests already with an approver keep their approvers.', confirm: 'Reset', danger: true })) return;
    try { await api(`/setup/types/${encodeURIComponent(code)}/reset`, {}); d.close(); toast('Reset to the defaults.'); await afterSetup(); setupTypes(); } catch (e) { $('#td-err').textContent = e.message; }
  };
  $('#tdform').onsubmit = async e => {
    e.preventDefault();
    const patch = {}, num2 = id => ($(id).value === '' ? null : Number($(id).value)), set = (k, v) => { if (JSON.stringify(v) !== JSON.stringify(t[k])) patch[k] = v; };
    if (f.has('enabled') && code !== 'payroll') set('enabled', $('#t_enabled').checked);
    if (f.has('requires_approval')) set('requires_approval', $('#t_requires_approval').checked);
    if (f.has('approver_rule') && $('#t_approver_rule').value !== (t.approver_rule || 'manager')) patch.approver_rule = $('#t_approver_rule').value;
    if (f.has('backup_rule') && ($('#t_backup_rule').value || null) !== (t.backup_rule || null)) patch.backup_rule = $('#t_backup_rule').value || null;
    if (f.has('fulfiller_rule')) set('fulfiller_rule', $('#t_fulfiller_rule').value);
    if (f.has('redirect')) set('redirect', $('#t_redirect').value);
    if (f.has('auto_allow_up_to')) set('auto_allow_up_to', num2('#t_auto_allow_up_to'));
    if (f.has('reminder_hours')) { set('reminder_hours', num2('#t_reminder_hours')); set('escalate_hours', num2('#t_escalate_hours')); }
    if (f.has('extra_approvers')) {
      const ex = [];
      for (const i of [0, 1]) {
        const rule = $(`#t_x${i}_rule`).value; if (!rule) continue;
        const kind = $(`#t_x${i}_kind`).value; let when = null;
        if (kind === 'cost') when = { cost_above: num2(`#t_x${i}_cost`) };
        else if (kind === 'words') when = { detail_contains: { field: $(`#t_x${i}_field`).value, words: $(`#t_x${i}_words`).value.split(',').map(s => s.trim()).filter(Boolean) } };
        ex.push({ rule, when });
      }
      set('extra_approvers', ex);
    }
    if (f.has('details_needed')) set('details_needed', $$('[data-detail]', d).filter(c => c.checked).map(c => c.dataset.detail));
    if (!Object.keys(patch).length) { d.close(); toast('Nothing changed.'); return; }
    ok.disabled = true;
    try { await api('/setup/types/' + encodeURIComponent(code), patch); d.close(); toast('Saved. New requests use it.'); await afterSetup(); setupTypes(); }
    catch (err) { $('#td-err').textContent = err.message; ok.disabled = false; }
  };
  d.showModal();
}

/* policies */
async function setupPolicies() {
  const d = await api('/setup/policies'), ro = d.demo;
  $('#sbody').innerHTML = `<p class="meta" style="max-width:640px">When someone asks a policy question, Deskhand answers with the text you write here, word for word. It never makes policy up: with no matching policy it points them to HR.</p>
    ${d.policies.length ? d.policies.map(p => `<div class="typecard"><div class="h"><b>${esc(p.title)}</b></div><p class="sub" style="white-space:pre-wrap">${esc(p.text)}</p>
      ${ro ? '' : `<div class="btns"><button class="btn small" data-editpol="${esc(p.key)}">Edit</button><button class="btn small danger" data-delpol="${esc(p.key)}">Delete</button></div>`}</div>`).join('')
      : emptyState('No policies yet', ro ? 'The demo company has prepared policies that Deskhand quotes.' : 'Add the rules people ask about, such as work from home or sick leave.')}
    ${ro ? '' : '<p style="margin-top:16px"><button class="btn" id="addpol">Add a policy</button></p>'}`;
  const save = (title, locked) => formDialog({ title: locked ? `Edit ${title}` : 'Add a policy', confirm: 'Save policy',
    fields: [{ key: 'title', label: 'Name (for example Work from home)', value: title || '', disabled: !!locked }, { key: 'text', label: 'What Deskhand should say', type: 'textarea', value: (d.policies.find(p => p.title === title) || {}).text || '' }],
    submit: async v => { await api('/setup/policies', { title: v.title, text: v.text }); toast('Saved.'); setupPolicies(); } });
  const add = $('#addpol'); if (add) add.onclick = () => save('', false);
  $$('[data-editpol]').forEach(b => b.onclick = () => save(d.policies.find(p => p.key === b.dataset.editpol).title, true));
  $$('[data-delpol]').forEach(b => b.onclick = async () => {
    if (!await dialogPrompt({ title: 'Delete this policy?', body: 'Deskhand will stop answering questions about it.', confirm: 'Delete', danger: true })) return;
    try { await api(`/setup/policies/${encodeURIComponent(b.dataset.delpol)}/delete`, {}); toast('Deleted.'); } catch (e) { toast(e.message, 6000); }
    setupPolicies();
  });
}

/* routing test */
async function setupTest() {
  TD = await api('/setup/types');
  const so = (xs, f, sel) => xs.map(x => `<option value="${esc(f(x)[0])}" ${f(x)[0] === sel ? 'selected' : ''}>${esc(f(x)[1])}</option>`).join('');
  $('#sbody').innerHTML = `<p class="meta" style="max-width:640px">Pick a person and a kind of request to see what would happen, before anyone asks. It uses the same rules as a real request. Nothing is created or sent.</p>
    <form class="simform" id="simform" novalidate><div><label class="l" for="sim_person">Who is asking</label><select class="field" id="sim_person">${so(TD.people, p => [p.id, p.name], ME.user.id)}</select></div>
      <div><label class="l" for="sim_type">What they ask for</label><select class="field" id="sim_type">${so(TD.types, t => [t.code, t.name], '')}</select></div>
      <div><label class="l" for="sim_cost">Estimated cost (optional)</label><input class="field" id="sim_cost" type="number" min="0" step="any" placeholder="For example 25000"></div>
      <div><label class="l" for="sim_res">Resource, for access requests (optional)</label><input class="field" id="sim_res" placeholder="For example shared finance folder" autocomplete="off"></div>
      <div class="err" id="sim_err" role="alert"></div><div><button class="btn primary" type="submit">Test it</button></div></form><div id="simres" aria-live="polite"></div>`;
  $('#simform').onsubmit = async e => {
    e.preventDefault(); $('#sim_err').textContent = '';
    try {
      const r = await api('/setup/simulate', { person_id: $('#sim_person').value, type_code: $('#sim_type').value, estimated_cost: $('#sim_cost').value === '' ? null : Number($('#sim_cost').value), resource: $('#sim_res').value.trim() || null });
      $('#simres').innerHTML = `<div class="simres"><p><b>${esc(r.text)}</b></p>
        ${r.chain.length ? `<ol class="simchain">${r.chain.map(c => `<li><b>${esc(c.name)}</b> <span class="meta">${esc(c.reason)}</span></li>`).join('')}</ol>` : ''}
        <dl class="kv">${r.outcome === 'approval' || r.outcome === 'no_approval' ? `<dt>Reminder</dt><dd>after ${r.reminder_hours} h</dd><dt>Escalates</dt><dd>after ${r.escalate_hours} h${r.escalates_to ? ` to ${esc(r.escalates_to.name)}` : ''}</dd>` : ''}
        ${r.fulfiller ? `<dt>Task done by</dt><dd>${esc(r.fulfiller.name)}</dd>` : ''}</dl>
        ${r.warnings.map(w => `<div class="warnrow">${icon('s-alert')}<span>${esc(w)}</span></div>`).join('')}</div>`;
    } catch (err) { $('#sim_err').textContent = err.message; $('#simres').innerHTML = ''; }
  };
}

/* ---------- leave: balances, history, allowances and corrections ---------- */
const cap = s => String(s).charAt(0).toUpperCase() + String(s).slice(1);
const hrLabel = () => S.hr_label || 'the HR system';
const hrApplied = () => S.hr_applied || 'applied in the HR system';
const isHRDemo = () => hrLabel() === 'the HR system';
const dayName = iso => new Date(iso + 'T00:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
const daysText = n => `${n} day${Math.abs(n) === 1 ? '' : 's'}`;
const signed = n => (n > 0 ? '+' : '') + n;
function infoDialog(title, html) { $('#idlg-title').textContent = title; $('#idlg-body').innerHTML = html; $('#idlg-close').onclick = () => $('#idlg').close(); $('#idlg').showModal(); }
function entryText(e) {
  if (e.kind === 'grant') return 'Yearly allowance';
  if (e.kind === 'use') return `Leave taken: ${esc(e.dates.map(dayName).join(', '))}${e.request_id ? ` (<a href="#request/${e.request_id}">request ${e.request_id}</a>)` : ''}`;
  return `Adjusted by ${esc(e.by)}: ${esc(e.note)}`;
}
const entryRows = rows => rows.length ? `<div class="scroll"><table class="t"><tr><th>When</th><th>What</th><th class="num">Days</th></tr>${rows.map(e =>
  `<tr><td class="num">${stamp(e.at)}</td><td>${e.type_name ? esc(e.type_name) + ': ' : esc(cap(e.type)) + ': '}${entryText(e)}</td><td class="num ${e.days < 0 ? 'neg' : ''}">${signed(e.days)}</td></tr>`).join('')}</table></div>` : emptyState('Nothing yet', 'Leave you take and any corrections appear here.');

async function pageLeave() {
  const d = await api('/leave/me');
  $('#main').innerHTML = `<div class="page-head"><h1>My leave</h1><span class="meta">${d.year || ''}</span></div>
    <div class="grid">${d.types.map(t => `<div><div class="v ${t.balance < 0 ? 'neg' : ''}">${t.balance}</div><div class="meta">${esc(t.name)} left${t.yearly_days != null ? `, of ${t.yearly_days} a year` : ''}</div></div>`).join('')}</div>
    <p style="margin:16px 0"><a href="#ask">Ask for leave</a> in plain words, for example "I need Friday and Monday off". Only Monday to Friday count; a half day counts 0.5.</p>
    <h2 style="margin-top:24px">History</h2><div id="lhist">${entryRows(d.entries)}</div>`;
}

async function setupLeave() {
  const d = await api('/setup/leave');
  if (d.demo) { $('#sbody').innerHTML = '<div class="state"><b>The demo company uses prepared figures</b>Real companies keep leave in their own record. Create your own company to try allowances and corrections.</div>'; return; }
  $('#sbody').innerHTML = `<p class="meta" style="max-width:640px">Each person gets the yearly allowance the first time leave is looked at in a calendar year. A new allowance applies to people who have not had this year's yet; for anyone else use Adjust. Every change is a line in the leave record with your name and the reason, and cannot be edited later.</p>
    <h2>Yearly allowance</h2><div class="scroll"><table class="t"><tr><th>Leave type</th><th>Days a year</th><th></th></tr>${d.types.map(t =>
      `<tr><td><b>${esc(t.name)}</b></td><td><input class="field" id="al_${esc(t.code)}" type="number" min="0" max="366" step="0.5" value="${t.yearly_days}" style="max-width:140px" aria-label="Days a year for ${esc(t.name)}"></td><td><button class="btn small" data-allow="${esc(t.code)}">Save</button></td></tr>`).join('')}</table></div>
    <h2 style="margin-top:28px">Balances, ${d.year}</h2><div class="scroll"><table class="t"><tr><th>Person</th>${d.types.map(t => `<th class="num">${esc(t.name)}</th>`).join('')}<th></th></tr>${d.people.map(p =>
      `<tr><td><b>${esc(p.name)}</b></td>${d.types.map(t => `<td class="num ${p.balances[t.code] < 0 ? 'neg' : ''}">${p.balances[t.code]}</td>`).join('')}<td><button class="btn small" data-adjust="${esc(p.id)}">Adjust</button> <button class="btn small" data-hist="${esc(p.id)}">History</button></td></tr>`).join('')}</table></div>`;
  $$('[data-allow]').forEach(b => b.onclick = async () => {
    try { await api(`/setup/leave/${encodeURIComponent(b.dataset.allow)}`, { yearly_days: Number($('#al_' + b.dataset.allow).value) }); toast('Saved.'); } catch (e) { toast(e.message, 6000); }
    setupLeave();
  });
  $$('[data-adjust]').forEach(b => b.onclick = () => {
    const p = d.people.find(x => x.id === b.dataset.adjust);
    formDialog({ title: `Adjust ${p.name}'s leave`, intro: 'Add or take away days in the current year. The reason is kept in the leave record with your name.', confirm: 'Record adjustment',
      fields: [{ key: 'leave_type', label: 'Leave type', options: d.types.map(t => [t.code, `${t.name} (now ${p.balances[t.code]})`]), value: d.types[0].code },
        { key: 'days', label: 'Days (use a minus sign to take days away)', type: 'number', hint: 'Whole or half days, for example 3 or -0.5.' }, { key: 'note', label: 'Reason', hint: 'For example: joined mid-year, extra days agreed.' }],
      submit: async v => { await api('/leave/adjust', { person_id: p.id, leave_type: v.leave_type, days: Number(v.days), note: v.note }); toast('Recorded.'); setupLeave(); } });
  });
  $$('[data-hist]').forEach(b => b.onclick = async () => {
    try { const h = await api('/leave/people/' + encodeURIComponent(b.dataset.hist)); infoDialog(`Leave record: ${h.person}`, entryRows(h.entries.map(e => ({ ...e, type_name: '' })))); } catch (e) { toast(e.message, 6000); }
  });
}

/* ---------- messaging mode, Slack matching and your own mailbox ---------- */
async function paintMessaging() {
  const el = $('#hmsg'); if (!el) return;
  let d;
  try { d = await api('/delivery'); } catch (e) { el.innerHTML = errorState('messaging', e, paintMessaging); return; }
  const live = d.mode === 'live', admins = S.employees.filter(e => e.role === 'admin' && e.active !== false);
  const unmatched = d.people_slack.filter(p => !p.slack);
  el.innerHTML = `<h2>Messaging <span class="chip ${live ? 'approved' : 'pending'}" style="margin-left:8px">${icon(live ? 's-done' : 's-pending')}${live ? 'Live' : 'Rehearsal'}</span></h2>
    <p class="meta" style="max-width:680px">${live
      ? 'Slack messages and emails go to the real people. Approvers get a Slack message and an email; requesters hear back the same way.'
      : 'Nothing reaches your colleagues yet. Every Slack message and email goes only to the admin below, labelled with the person it is for, so you can see exactly what each person would get.'}</p>
    ${live ? '' : `<div style="max-width:360px;margin-top:8px"><label class="l" for="rehearsal_person">Rehearsal messages go to</label><select class="field" id="rehearsal_person">${admins.map(a => `<option value="${esc(a.id)}" ${a.id === (d.rehearsal_person || {}).id ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}</select></div>`}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">${live ? '<button class="btn" id="torehearsal">Back to rehearsal</button>' : '<button class="btn primary" id="golive">Go live</button>'}
      <button class="btn" id="matchslack" ${d.ready.slack ? '' : 'disabled'}>Match Slack accounts</button></div>
    <p class="meta">Slack accounts matched by email: <b>${d.slack_matched} of ${d.people}</b>${unmatched.length ? `. Not matched yet: ${esc(unmatched.slice(0, 8).map(p => p.name).join(', '))}${unmatched.length > 8 ? '…' : ''}. They get email only until matched.` : '.'}</p>
    <div id="matchres" aria-live="polite"></div>`;
  const post = async body => { try { await api('/delivery/mode', body); toast(body.mode === 'live' ? 'Live: real people now get Slack messages and emails.' : 'Back in rehearsal.'); await pollCore(); } catch (e) { toast(e.message, 6000); } paintMessaging(); };
  const g = $('#golive'); if (g) g.onclick = async () => {
    if (!await dialogPrompt({ title: 'Go live?', body: 'Your colleagues will start receiving Slack messages and emails from Deskhand. Messages written before now are not sent. You can go back to rehearsal at any time.', confirm: 'Go live' })) return;
    post({ mode: 'live', confirm: true, rehearsal_person: $('#rehearsal_person') ? $('#rehearsal_person').value : null });
  };
  const r = $('#torehearsal'); if (r) r.onclick = () => post({ mode: 'rehearsal', confirm: false });
  const rp = $('#rehearsal_person'); if (rp) rp.onchange = () => post({ mode: 'rehearsal', rehearsal_person: rp.value });
  $('#matchslack').onclick = async e => {
    e.target.disabled = true;
    try {
      const m = await api('/integrations/slack/match', {});
      await afterSetup();
      $('#matchres').innerHTML = `<p class="meta">Matched ${m.matched.length}${m.not_found.length ? `; no Slack account for ${esc(m.not_found.join(', '))}` : ''}${m.problems.length ? `; problem: ${esc(m.problems[0])}` : ''}${m.unmatched_left && !m.problems.length ? `; ${m.unmatched_left} left, press again for the next batch of ${m.batch_limit}` : ''}.</p>`;
      const mm = await api('/delivery'); $('#hmsg .meta b').textContent = `${mm.slack_matched} of ${mm.people}`;
    } catch (err) { toast(err.message, 6000); }
    e.target.disabled = false;
  };
}

function loadConnectScript(src) {
  if (document.getElementById('viasocket-connect-script')) return Promise.resolve();
  return new Promise((ok, no) => { const sc = document.createElement('script'); sc.id = 'viasocket-connect-script'; sc.src = src; sc.onload = ok; sc.onerror = () => no(new Error('Could not load the viaSocket connect script. Check the network and reload.')); document.head.appendChild(sc); });
}
async function pageMailbox() {
  const d = await api('/me/connections');
  const m = $('#main');
  if (d.demo) { m.innerHTML = '<div class="page-head"><h1>Your mailbox</h1></div><div class="state"><b>Not in the demo company</b>Create your own company to connect a mailbox.</div>'; return; }
  m.innerHTML = `<div class="page-head"><h1>Your mailbox</h1><span class="meta">${esc(d.email)}</span></div>
    <p class="meta" style="max-width:680px">When you connect your Gmail, the emails about <b>your own requests</b> come from your address: the email asking your approver to decide, and, when you approve or reject someone's request, the email telling them. Deskhand writes those emails; it never reads your mail and never sends anything else from it. Only people in your company are emailed. If you do not connect, the company mailbox sends them instead and nothing is lost.</p>
    <div class="approw" style="display:block;max-width:680px"><div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><img src="${esc(d.app.icon)}" alt="" width="28" height="28"><b style="flex:1">Gmail</b>
      <span class="chip ${d.app.connected ? 'approved' : 'neutral'}">${icon(d.app.connected ? 's-done' : 's-upcoming')}${d.app.connected ? 'Connected' : 'Not connected'}</span>
      <button class="btn small primary" id="mbconnect" ${d.configured ? '' : 'disabled'}>${d.app.connected ? 'Reconnect' : 'Connect Gmail'}</button>${d.app.connected ? '<button class="btn small" id="mbtest">Send a test email to me</button><button class="btn small danger" id="mbdisc">Disconnect</button>' : ''}</div>
      ${d.configured ? '' : '<p class="meta" style="margin-top:8px">Connecting is switched off until the server has its viaSocket values.</p>'}<div id="mbres" aria-live="polite"></div></div>`;
  $('#mbconnect').onclick = async () => {
    try {
      const t = await api('/me/connections/token'); await loadConnectScript(d.connect_js);
      CONN.token = t.token; CONN.info.mine = { service_id: t.service_id, action_ids: t.action_ids }; CONN.pending = 'mine';
      window.openViasocketConnection(CONN.token, t.service_id, { filteredActions: t.action_ids, skipActionSelection: true });
    } catch (e) { toast(e.message, 6000); }
  };
  const tst = $('#mbtest'); if (tst) tst.onclick = async () => {
    try { const r = await api('/me/connections/test', {}); $('#mbres').innerHTML = `<p class="${r.outcome === 'success' ? 'meta' : 'err'}">${r.outcome === 'success' ? 'Sent. Check your inbox.' : esc(r.error || nice(r.outcome))}</p>`; } catch (e) { toast(e.message, 6000); }
  };
  const dis = $('#mbdisc'); if (dis) dis.onclick = async () => {
    if (!await dialogPrompt({ title: 'Disconnect your mailbox?', body: 'Emails about your requests will come from the company mailbox instead.', confirm: 'Disconnect', danger: true })) return;
    try { await api('/me/connections/gmail/disconnect', {}); toast('Disconnected.'); } catch (e) { toast(e.message, 6000); }
    pageMailbox();
  };
}

/* ---------- render loop ---------- */
const TITLES = { login: 'Sign in', register: 'Create a company', join: 'Join', ask: 'Ask', mine: 'My requests', needs: 'Needs you', leave: 'My leave', mailbox: 'Your mailbox', admin: 'Admin', setup: 'Setup', people: 'People', integrations: 'Integrations', health: 'Health', audit: 'Audit' };
const LOADING = { leave: 'your leave', health: 'health', people: 'people', setup: 'setup', integrations: 'integrations', admin: 'the admin page', audit: 'the audit record' };
async function render(userNav) {
  const my = ++seq;
  const route = parseHash();
  const authPage = AUTH_PAGES.includes(route.page);
  if (!ME && !authPage) { NEXT = location.hash; return go('#login'); }
  if (ME && (route.page === 'login' || route.page === 'register')) return go('#ask');
  if (ME && isDemo() && ['leave', 'mailbox'].includes(route.page)) return go('#ask');  // the demo's figures are prepared, not a record
  if (ME && ADMIN_PAGES.includes(route.page) && !isAdmin()) return go('#ask');
  const changedPage = route.page !== current.page || route.id !== current.id;
  current = route;
  if (userNav && changedPage) window.scrollTo(0, 0);
  paintShell();
  $$('#nav a').forEach(a => { if (a.dataset.page === route.page || (a.hasAttribute('data-hub') && ADMIN_PAGES.includes(route.page))) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  document.title = TITLES[route.page] + ' · Deskhand';
  const m = $('#main');
  try {
    if (authPage) {
      if (route.page === 'login') await pageLogin(); else if (route.page === 'register') await pageRegister(); else await pageJoin(route.id);
      if (userNav) m.focus({ preventScroll: true });
      return;
    }
    if (changedPage) m.innerHTML = loading(LOADING[route.page] || 'your requests');
    if (['ask', 'mine', 'needs'].includes(route.page)) { await loadLists(); if (!SEEN.__init) { diffStatuses(REQS); SEEN.__init = 1; } }
    if (my !== seq) return;
    if (route.page === 'ask') await pageAsk();
    else if (route.page === 'mine') await pageMine(route.id);
    else if (route.page === 'needs') await pageNeeds(route.id);
    else if (route.page === 'leave') await pageLeave();
    else if (route.page === 'mailbox') await pageMailbox();
    else if (route.page === 'admin') await pageAdmin();
    else if (route.page === 'setup') await pageSetup(route.id);
    else if (route.page === 'integrations') await pageIntegrations();
    else if (route.page === 'people') await pagePeople();
    else if (route.page === 'health') await pageHealth();
    else await pageAudit();
  } catch (e) { if (my === seq && ME) m.innerHTML = errorState('this page', e, () => render(true)); }
  if (userNav && changedPage) m.focus({ preventScroll: true });
}

async function refreshAll(force, afterDecision) {
  if (!ME || AUTH_PAGES.includes(current.page)) return;
  if (!['ask', 'mine', 'needs'].includes(current.page)) { await pollCore(); if (force || afterDecision) await render(false); return; }
  const interacting = document.activeElement && document.activeElement.closest && document.activeElement.closest('.decide, .composer');
  const typed = $('#reason') && $('#reason').value;
  try {
    await Promise.all([pollCore(), loadLists()]);
    const rows = current.page === 'needs' ? INBOX : REQS;
    const changed = diffStatuses([...REQS, ...INBOX]);
    if (force || afterDecision) return render(false).then(() => flash(changed));
    if (current.page === 'ask') { await updateAsk(); flash(changed); return; }
    const sig = JSON.stringify(rows.map(r => [r.id, r.status, r.chain_index, r.escalated_to, r.last_reminder_at])) + Math.floor(NOW / 60);
    const lst = $('#lst');
    if (lst && lst.dataset.sig !== sig) {
      lst.dataset.sig = sig;
      (current.page === 'needs' ? paintNeeds : paintMine)(current.id || (document.querySelector('#det')?.dataset.id ? +$('#det').dataset.id : null), changed);
    }
    const detId = $('#det') && +$('#det').dataset.id;
    if (detId && changed.has(detId) && !interacting && !typed) await paintDetail(detId, current.page);
    else if (detId && !interacting && !typed) {
      if (current.page === 'needs' && !INBOX.some(x => x.id === detId)) await paintDetail(detId, 'needs');
    }
  } catch { /* the page keeps what it has; the header shows reachability */ }
}

/* ---------- shell wiring ---------- */
function wireShell() {
  window.addEventListener('hashchange', () => render(true));
  $('#demobtn').onclick = () => { $('#clock').textContent = stamp(NOW); $('#drawer').showModal(); };
  $('#drawer-close').onclick = () => $('#drawer').close();
  $('#who').onchange = async e => {
    try { ME = await api('/demo/act-as', { person_id: e.target.value }); } catch (err) { toast(err.message, 6000); $('#who').value = meId(); return; }
    resetPerson(); $('#drawer').close();
    try { await loadCore(); } catch (err) { toast(err.message, 6000); }
    paintShell();
    if (ADMIN_PAGES.includes(current.page) && !isAdmin()) go('#ask'); else render(false);
  };
  const applyTheme = t => { if (t === 'system') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', t); };
  applyTheme(store.get('dh_theme') || 'system');
  $('#theme').onchange = e => { store.set('dh_theme', e.target.value); applyTheme(e.target.value); };
  $('#acctbtn').onclick = openAccount;
  $('#acct-close').onclick = () => $('#acct').close();
  $('#mblink').onclick = () => $('#acct').close();
  $('#signout').onclick = signOut;
  $('#pwform').onsubmit = changePassword;
  $$('[data-adv]').forEach(b => b.onclick = async () => {
    try { const r = await api('/clock/advance', { hours: +b.dataset.adv }); toast(`Clock moved ${b.dataset.adv} h. Reminders sent: ${r.sweep.reminders}. Escalations: ${r.sweep.escalations}.`, 5000); } catch (e) { toast(e.message, 6000); }
    await pollCore(); refreshAll(true);
  });
  $('#resetbtn').onclick = async () => {
    $('#drawer').close();
    if (!await dialogPrompt({ title: 'Reset all demo data?', body: 'This deletes every request, message and audit event and restarts the demo clock. Connections and settings stay.', label: 'Type reset to confirm', confirm: 'Reset demo data', match: 'reset', danger: true })) return;
    try { await api('/reset', {}); resetPerson(); toast('Demo data reset.'); } catch (e) { toast(e.message, 6000); }
    await pollCore(); render(false);
  };
  const sb = $('#statusbtn');
  sb.onclick = () => { const open = $('#statuspanel').hidden; $('#statuspanel').hidden = !open; sb.setAttribute('aria-expanded', String(open)); };
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#statuspanel').hidden) { $('#statuspanel').hidden = true; sb.setAttribute('aria-expanded', 'false'); sb.focus(); } });
}

async function boot() {
  wireShell();
  try { ME = await api('/auth/me'); } catch (e) {
    if (e.status !== 401) { paintShell(); $('#main').innerHTML = errorState('Deskhand', e, () => location.reload()); return; }
    ME = null;
  }
  if (ME) { try { await loadCore(); } catch (e) { paintShell(); $('#main').innerHTML = errorState('Deskhand', e, () => location.reload()); return; } }
  paintShell();
  await render(false);
  setInterval(() => { if (ME && !document.hidden && !document.querySelector('dialog[open]')) refreshAll(false); }, 4000);
}
boot();
})();
