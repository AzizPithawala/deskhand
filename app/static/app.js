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
let who = store.get('dh_who') || 'e01';
let NOW = Date.now() / 1000;
let seq = 0, current = { page: '', id: null };
let REQS = [], INBOX = [];
const SEEN = {}, PREV = {}, LOG = { chat: {}, email: {} }, SEENMSG = {};
let CLOCKDATA = { banners: [], delivery: { used_today: 0, cap: 150, capped: false } };

/* ---------- api ---------- */
async function api(path, body) {
  const opt = body === undefined ? undefined : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  let r;
  try { r = await fetch('/api' + path, opt); } catch { const e = new Error('The server could not be reached.'); e.network = true; throw e; }
  let j = {};
  try { j = await r.json(); } catch { /* not json */ }
  if (!r.ok) { const e = new Error(typeof j.detail === 'string' ? j.detail : (r.statusText || 'The request failed')); e.status = r.status; throw e; }
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
  if (rule.startsWith('role:')) return S.employees.find(e => e.role === rule.slice(5)) || null;
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
    const st = { k: 'ful', state, title: auto ? 'Applied in the HR system' : `Done by ${ful ? nm(ful) : 'the fulfiller'}` };
    if (state === 'current') st.note = auto ? 'Deskhand is applying it now.' : `${nm(ful)} grants access, then clicks Done.`;
    if (state === 'parked') {
      const f = ev('action_failed').slice(-1)[0];
      st.note = `The HR system did not answer after three tries${f && f.data && f.data.error ? ` (${f.data.error})` : ''}. Nothing was lost. An admin can replay it.`;
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
    case 'FULFILLING': return TYPE[r.type_code] && TYPE[r.type_code].fulfilment_kind === 'auto_action' ? 'Applying it in the HR system.' : 'Approved. Waiting for the fulfiller to finish.';
    case 'APPROVED': return 'Approved.';
    case 'CLOSED': case 'FULFILLED': return 'Completed.';
    case 'REJECTED': return r.rejection_reason ? `Rejected: ${r.rejection_reason}` : 'Rejected.';
    case 'PARKED': return 'Parked: the HR system is not answering. An admin can replay it.';
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
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [p, a] = h.split('/');
  if (p === 'inbox' && a && EMP[a]) { setWho(a, true); history.replaceState(null, '', '#needs'); return { page: 'needs', id: null }; }
  if (p === 'request' && a) return { page: 'mine', id: +a || null };
  if (['ask', 'mine', 'needs', 'health', 'audit'].includes(p)) return { page: p, id: a ? (+a || null) : null };
  return { page: 'ask', id: null };
}
function setWho(id, silent) {
  who = id; store.set('dh_who', id);
  const sel = $('#who'); if (sel) sel.value = id;
  if (!silent) refreshAll(true);
}

/* ---------- data ---------- */
async function loadCore() {
  const [st, cl] = await Promise.all([api('/state'), api('/clock')]);
  S = st; EMP = {}; TYPE = {};
  S.employees.forEach(e => { EMP[e.id] = e; });
  S.types.forEach(t => { TYPE[t.code] = t; });
  applyClock(cl);
  if (!EMP[who]) who = S.employees[0].id;
  $('#who').innerHTML = S.employees.map(e => `<option value="${esc(e.id)}">${esc(e.name)} (${esc(e.role.replace(/_/g, ' '))})</option>`).join('');
  $('#who').value = who;
}
function applyClock(cl) {
  CLOCKDATA = cl; NOW = cl.now;
  const b = cl.banners || [];
  $('#banner').innerHTML = b.map(t => `<div class="b">${icon('s-alert')}<span>${esc(t)}</span></div>`).join('');
  const slackOk = !b.some(t => /^(Slack|viaSocket)/.test(t));
  const mailOk = !b.some(t => /^(Gmail|Notification email|viaSocket)/.test(t));
  const d = cl.delivery || { used_today: 0, cap: 150 };
  const dot = ok => `<span class="dot${ok ? '' : ' bad'}"></span>`;
  const inner = `<span class="it">${dot(slackOk)}Slack ${slackOk ? 'connected' : 'not ready'}</span><span class="it">${dot(mailOk)}Gmail ${mailOk ? 'connected' : 'not ready'}</span><span class="it num">Sent today ${d.used_today} / ${d.cap}</span>`;
  $('#status').innerHTML = inner;
  $('#statuspanel').innerHTML = inner;
  $('#statusdot').className = 'dot' + (slackOk && mailOk && !d.capped ? '' : ' bad');
  const c = $('#clock'); if (c) c.textContent = stamp(cl.now);
}
async function pollCore() {
  try { applyClock(await api('/clock')); } catch { /* shown by page-level states */ }
}
async function loadLists() {
  [REQS, INBOX] = await Promise.all([api('/requests?employee=' + who), api('/inbox/' + who)]);
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

function mailCard(x, person) {
  const mine = x.who === 'me';
  return `<div class="mailitem"><div class="meta">From: ${mine ? esc(nm(person)) : 'Deskhand &lt;desk@acme-demo.example&gt;'}<br>To: ${mine ? 'Deskhand &lt;desk@acme-demo.example&gt;' : esc(nm(person))}${x.ts ? '<br>' + stamp(x.ts) : ''}</div><b>${esc(x.subject)}</b><pre>${esc(x.text)}</pre></div>`;
}
function reqCard(r) {
  return `<div class="reqcard" data-req="${r.id}"><div class="h"><b>${esc(r.summary)}</b>${chip(r)}</div>${miniHTML(r)}<div class="meta">${esc(sentence(r))}</div><div style="margin-top:6px"><a href="#request/${r.id}">Open request ${r.id}</a></div></div>`;
}

async function pageAsk() {
  const mode = store.get('dh_mode') === 'email' ? 'email' : 'chat';
  const m = $('#main');
  m.innerHTML = `<div class="ask"><h1 class="display">What do you need?</h1><p class="lede">Say it in one sentence. Deskhand finds the right person and keeps you posted.</p>
    <div class="seg2" role="group" aria-label="Channel"><button data-mode="chat" aria-pressed="${mode === 'chat'}">Message</button><button data-mode="email" aria-pressed="${mode === 'email'}">Email</button></div>
    ${mode === 'chat'
      ? `<form class="composer" id="cform"><label class="sr" for="q">Ask Deskhand</label><input class="field" id="q" placeholder="Ask for something in one sentence" autocomplete="off"><button class="btn primary" type="submit">Send</button></form>
         <div class="chips" aria-label="Examples">${SAMPLES.map(s => `<button type="button" data-s="${esc(s)}">${esc(s)}</button>`).join('')}</div>`
      : `<form class="composer mailc" id="cform"><label class="l" for="subj">Subject</label><input class="field" id="subj" autocomplete="off"><label class="l" for="body">Message</label><textarea class="field" id="body" rows="4" placeholder="Write to desk@acme-demo.example"></textarea><button class="btn primary" type="submit">Send email</button></form><div style="height:24px"></div>`}
    <div class="convo" id="convo" aria-live="polite"></div></div>`;
  $$('.seg2 button', m).forEach(b => b.onclick = () => { store.set('dh_mode', b.dataset.mode); pageAsk(); });
  $$('.chips button', m).forEach(b => b.onclick = () => { const q = $('#q'); q.value = b.dataset.s; q.focus(); });
  $('#cform').onsubmit = async e => { e.preventDefault(); await sendMsg(mode); };
  await updateAsk();
}
async function updateAsk() {
  if (current.page !== 'ask') return;
  const mode = store.get('dh_mode') === 'email' ? 'email' : 'chat';
  const rows = (await api('/outbox?to=' + who + '&limit=200')).reverse();
  const L = (LOG[mode][who] ??= []);
  const k = mode + who;
  const seen = (SEENMSG[k] ??= new Set());
  rows.filter(r => r.channel === mode && !seen.has(r.id)).forEach(r => { seen.add(r.id); L.push({ who: 'bot', id: r.id, subject: r.subject, text: r.body, ts: r.created_at }); });
  const byId = {}; REQS.forEach(r => { byId[r.id] = r; });
  // group each exchange (what you sent, the replies, the request cards) and show the newest exchange first, right under the composer
  const groups = [];
  L.forEach(x => { if (x.who === 'me' || !groups.length) groups.push([]); groups[groups.length - 1].push(x); });
  const piece = x => {
    if (x.who === 'cards') return x.ids.map(id => byId[id] ? reqCard(byId[id]) : '').join('');
    if (mode === 'email') return mailCard(x, who);
    return `<div class="msg ${x.who === 'me' ? 'me' : ''}"><div class="who">${x.who === 'me' ? 'You' : 'Deskhand'}</div><div class="tx">${esc(x.text)}</div></div>`;
  };
  const html = groups.length ? groups.reverse().map(g => `<div class="xchg">${g.map(piece).join('')}</div>`).join('')
    : emptyState('Nothing asked yet', mode === 'chat' ? 'Type a request above, or pick an example. You will see where it goes and who has it.' : 'Write an email to the desk. Replies from Deskhand appear here.');
  const convo = $('#convo');
  if (convo && convo.dataset.h !== html) { convo.dataset.h = html; convo.innerHTML = html; }
}
async function sendMsg(mode) {
  let text;
  if (mode === 'chat') {
    text = $('#q').value.trim();
    if (!text) { toast('Write a sentence first.'); return; }
    (LOG.chat[who] ??= []).push({ who: 'me', text });
    $('#q').value = '';
  } else {
    const s = $('#subj').value.trim(), b = $('#body').value.trim();
    if (!b) { toast('Write a message first.'); return; }
    text = (s ? 'Subject: ' + s + '\n' : '') + b;
    (LOG.email[who] ??= []).push({ who: 'me', subject: s || '(no subject)', text: b, ts: NOW });
    $('#subj').value = ''; $('#body').value = '';
  }
  const pending = { who: 'bot', text: 'Reading your request…' };
  if (mode === 'chat') { (LOG.chat[who] ??= []).push(pending); updateAsk(); }
  try {
    const res = await api('/ingest', { channel: mode, sender: who, text, source_event_id: uuid() });
    await loadLists();
    if (res.request_ids && res.request_ids.length) (LOG[mode][who] ??= []).push({ who: 'cards', ids: res.request_ids });
    if (res.outcome === 'paused_breaker') toast('Intake is paused after a burst of messages. An admin resets it on Health.', 6000);
  } catch (e) { toast(e.network ? e.message : `Could not send (${e.status || 'error'}): ${e.message}`, 6000); }
  const L = LOG.chat[who]; if (L) { const i = L.indexOf(pending); if (i >= 0) L.splice(i, 1); }
  await updateAsk();
}

function listRow(r, hrefBase, selected, extra = '') {
  return `<a class="item" href="#${hrefBase}/${r.id}" data-req="${r.id}" ${selected ? 'aria-current="true"' : ''}>
    <div class="t"><span class="id num">${r.id}</span><span>${esc(r.summary)}</span></div>
    <div class="s">${chip(r)}</div>${miniHTML(r)}<div class="meta">${esc(extra || sentence(r))}</div></a>`;
}

async function pageMine(id) {
  const m = $('#main');
  m.innerHTML = `<div class="page-head"><h1>My requests</h1><span class="meta">${esc(nm(who))}</span></div><div class="split ${id ? 'has-detail' : ''}" id="split"><div class="list" id="lst"></div><div class="detail" id="det" aria-live="polite"></div></div>`;
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
  m.innerHTML = `<div class="page-head"><h1>Needs you</h1><span class="meta">${esc(nm(who))}</span></div><div class="split ${id ? 'has-detail' : ''}" id="split"><div class="list" id="lst"></div><div class="detail" id="det" aria-live="polite"></div></div>`;
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
    await api(`/requests/${id}/${action}`, { actor_id: who, ...(extra || {}) });
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
    <div class="block" id="hdelivery"></div><div class="block" id="hconn"></div><div class="block" id="hguard"></div><div class="block" id="hlab"></div>`;
  let dash;
  try { dash = await api('/dashboard'); } catch (e) { $('#hstrip').innerHTML = errorState('health', e, pageHealth); return; }
  const g = await api('/digest/' + who).catch(() => null);
  let dv = null; try { dv = await api('/delivery?actor_id=' + who); } catch { /* admin only */ }
  const dd = dash.delivery || { used_today: 0, cap: 150, ready: {} };
  const working = dd.ready && dd.ready.slack && dd.ready.email;
  const cell = (v, l, dim, cls = '') => `<div class="${dim ? 'dim' : ''}"><div class="v ${cls}">${v}</div><div class="meta">${esc(l)}</div></div>`;
  $('#hstrip').innerHTML = `<div class="grid">${cell(working ? icon('s-done') + 'Working' : icon('s-pending') + 'Not ready', 'delivery to Slack and email', false, working ? 'ok' : 'warn')}${cell(`${dd.used_today} / ${dd.cap}`, 'delivered today (daily cap)')}${cell(dash.total, 'requests')}${cell(dash.avg_decision_minutes == null ? '–' : (dash.avg_decision_minutes < 1 ? 'under 1' : dash.avg_decision_minutes), 'average minutes to a decision')}${cell(dash.hr_effects, 'leaves applied in HR')}</div>`;
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
  $('#hlab').innerHTML = `<h2>Reliability lab</h2><p class="meta">Each button breaks something on purpose and shows whether the guard held. Runs use Neha Gupta's requests and never send to Slack or email.</p>
    ${LAB.map(([k, t, p]) => `<div class="labrow"><div><b>${esc(t)}</b></div><div><p class="meta">${esc(p)}</p><button class="btn small" data-lab="${k}">Run</button><div class="verdict-box" id="v_${k}" aria-live="polite"></div></div></div>`).join('')}`;
  $$('[data-lab]').forEach(b => b.onclick = () => runLab(b.dataset.lab));
  await paintConnections();
}
function paintDelivery(dv) {
  const el = $('#hdelivery'); if (!el) return;
  if (!dv) { el.innerHTML = `<h2>Delivery</h2>${emptyState('Delivery details are for admins', 'Open Demo controls and act as Arjun Patel to see them.')}`; return; }
  const cnt = Object.entries(dv.counts).map(([k, v]) => `${k} ${v}`).join(', ') || 'none yet';
  el.innerHTML = `<h2>Delivery</h2><p class="meta">Messages go out by themselves once Slack and Gmail are set up below. Approvals never wait for delivery.</p>
    <dl class="kv" style="margin-top:8px"><dt>Slack</dt><dd>${dv.ready.slack ? 'Ready' : 'Not ready'}</dd><dt>Email</dt><dd>${dv.ready.email ? `Ready, goes only to ${esc(dv.email_to)}` : 'Not ready'}</dd><dt>Delivered today</dt><dd class="num">${dv.used_today} of ${dv.cap}${dv.capped ? ' (cap reached)' : ''}</dd><dt>Outbox</dt><dd>${esc(cnt)}</dd><dt>Links point to</dt><dd>${esc(dv.base_url)}</dd></dl>
    ${dv.failed.length ? `<h3 style="margin-top:16px">Failed deliveries</h3>${dv.failed.map(f => `<div class="approw"><span style="flex:1">#${f.id} · ${esc(nice(f.kind))} (${esc(f.channel)}) · ${esc(f.delivery_error || '')}</span><button class="btn small" data-retry="${f.id}">Retry</button></div>`).join('')}<p class="meta">Check the channel before retrying: a failed send may already have arrived.</p>` : ''}`;
  $$('[data-retry]', el).forEach(b => b.onclick = async () => { try { await api('/delivery/retry/' + b.dataset.retry, { actor_id: who }); toast('Retried.'); } catch (e) { toast(e.message, 6000); } pageHealth(); });
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
  let c;
  try { c = await api('/connections?actor_id=' + who); } catch (e) {
    el.innerHTML = `<h2>Connections</h2>${e.status === 403 ? emptyState('Connections are for admins', 'Open Demo controls and act as Arjun Patel to set up Slack and Gmail.') : errorState('connections', e, paintConnections)}`; return;
  }
  if (c.configured) await prepConnect(c);
  el.innerHTML = `<h2>Connections</h2><p class="meta">The only setup. You sign in on each app's own screen; Deskhand never sees a password or token. Once Slack and its channel are set, chat notifications are delivered there. Once Gmail and the notification email are set, email notifications go to that one address.</p>
    ${!c.configured ? emptyState('viaSocket is not configured on the server', 'Connecting is switched off until the server has its viaSocket values. See the README.') : ''}
    ${c.note ? `<p role="alert" style="color:var(--rejected)">Could not read connection status: ${esc(c.note)}</p>` : ''}
    ${c.apps.map(a => appRow(a, c)).join('')}<div id="aiblock"></div>`;
  paintAI(c.configured);
  for (const a of c.apps) if (a.can_run) for (const st of a.settings) if (st.kind === 'picker') loadPicker(a, st);
  wireConnections(el);
}
async function paintAI(configured) {
  const el = $('#aiblock'); if (!el) return;
  let ai; try { ai = await api('/ai?actor_id=' + who); } catch { return; }
  el.innerHTML = `<div class="approw" style="display:block"><div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><b style="flex:1">Understanding requests</b>
    <span class="chip ${ai.enabled ? 'approved' : 'neutral'}">${icon(ai.enabled ? 's-done' : 's-upcoming')}${ai.enabled ? 'viaSocket AI is on' : 'Built-in rules'}</span></div>
    <p class="meta" style="margin:6px 0 8px;max-width:640px">By default Deskhand reads each message with its built-in rules. Switch viaSocket AI on and each message is sent to a Claude model to work out the request type and details. It waits at most ${ai.timeout_s} seconds and uses the rules if the AI is slow, fails or runs out of credits. Salary and payroll messages are never sent. The AI only proposes: approvers and approvals always come from your routing map.</p>
    <label style="display:flex;gap:10px;align-items:center;min-height:44px;cursor:pointer"><input type="checkbox" id="aiswitch" ${ai.switch ? 'checked' : ''} ${configured ? '' : 'disabled'} style="width:20px;height:20px"> Use viaSocket AI to understand requests</label></div>`;
  $('#aiswitch').onchange = async e => {
    try { await api('/ai', { actor_id: who, enabled: e.target.checked }); toast(e.target.checked ? 'viaSocket AI is on.' : 'Using the built-in rules.'); }
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
      ? `<button class="btn small" data-test="slack_send">Send a test message</button><input class="field" id="lk" placeholder="email to look up" style="width:220px"><button class="btn small" data-test="slack_lookup">Look up a user</button>`
      : `<button class="btn small" data-test="gmail_send">Send a test email to the notification address</button>`}</div><div id="tres_${a.key}" aria-live="polite"></div>` : ''}</div>`;
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
    CONN.token = (await api('/connections/token?actor_id=' + who)).token;
    for (const a of c.apps) CONN.info[a.key] = await api(`/connections/service/${a.key}?actor_id=${who}`);
    if (!document.getElementById('viasocket-connect-script')) {
      await new Promise((ok, no) => { const sc = document.createElement('script'); sc.id = 'viasocket-connect-script'; sc.src = c.connect_js; sc.onload = ok; sc.onerror = () => no(new Error('Could not load the viaSocket connect script. Check the network and reload.')); document.head.appendChild(sc); });
    }
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
    try { await api('/connections/connected', { actor_id: who, app: key, auth_id: d.data && d.data.id }); toast('Connected.'); } catch (e) { toast(e.message, 6000); }
    if (current.page === 'health') pageHealth();
  } else if (d.type === 'viasocket_connection_error') toast((d.error && d.error.message) || 'The connection did not complete.', 6000);
});
async function loadPicker(a, x) {
  const el = document.getElementById(`s_${a.key}_${x.key}`); if (!el) return;
  try {
    const o = await api(`/connections/${a.key}/options?actor_id=${who}&setting=${x.key}`);
    el.innerHTML = (x.required ? '<option value="">Choose one</option>' : '<option value="">Use the connected account</option>') + o.map(v => `<option value="${esc(v.value)}">${esc(v.label)}</option>`).join('');
    el.value = x.value || '';
  } catch (e) { el.innerHTML = `<option value="">${esc(e.message)}</option>`; }
}
async function saveSetting(key, setting, value) {
  try { await api(`/connections/${key}/setting`, { actor_id: who, setting, value }); toast('Saved.'); await pollCore(); } catch (e) { toast(e.message, 6000); }
}
async function disconnectApp(key) {
  if (!await dialogPrompt({ title: `Disconnect ${key}?`, body: 'Notifications that need this app stop being delivered until it is connected again.', confirm: 'Disconnect', danger: true })) return;
  try { await api(`/connections/${key}/disconnect`, { actor_id: who }); toast('Disconnected.'); } catch (e) { toast(e.message, 6000); }
  await pollCore(); pageHealth();
}
async function testConn(action) {
  const key = action.startsWith('slack') ? 'slack' : 'gmail';
  const box = $('#tres_' + key); box.innerHTML = '<p class="meta">Running</p>';
  try {
    const r = await api('/connections/test', { actor_id: who, action, email: action === 'slack_lookup' ? ($('#lk').value || '') : null });
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

/* ---------- render loop ---------- */
async function render(userNav) {
  const my = ++seq;
  const route = parseHash();
  const changedPage = route.page !== current.page || route.id !== current.id;
  current = route;
  $$('#nav a').forEach(a => { if (a.dataset.page === route.page) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  document.title = { ask: 'Ask', mine: 'My requests', needs: 'Needs you', health: 'Health', audit: 'Audit' }[route.page] + ' · Deskhand';
  const m = $('#main');
  if (changedPage) m.innerHTML = loading(route.page === 'health' ? 'health' : 'your requests');
  try {
    if (['ask', 'mine', 'needs'].includes(route.page)) { await loadLists(); if (!SEEN.__init) { diffStatuses(REQS); SEEN.__init = 1; } }
    if (my !== seq) return;
    if (route.page === 'ask') await pageAsk();
    else if (route.page === 'mine') await pageMine(route.id);
    else if (route.page === 'needs') await pageNeeds(route.id);
    else if (route.page === 'health') await pageHealth();
    else await pageAudit();
  } catch (e) { if (my === seq) m.innerHTML = errorState('this page', e, () => render(true)); }
  if (userNav && changedPage) m.focus({ preventScroll: true });
}

async function refreshAll(force, afterDecision) {
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
      const r = rows.find(x => x.id === detId);
      if (current.page === 'needs' && !INBOX.some(x => x.id === detId)) await paintDetail(detId, 'needs');
    }
  } catch { /* the page keeps what it has; the header shows reachability */ }
}

/* ---------- shell wiring ---------- */
function wireShell() {
  window.addEventListener('hashchange', () => render(true));
  $('#demobtn').onclick = () => { $('#clock').textContent = stamp(NOW); $('#drawer').showModal(); };
  $('#drawer-close').onclick = () => $('#drawer').close();
  $('#who').onchange = e => { setWho(e.target.value); };
  const theme = store.get('dh_theme') || 'system';
  $('#theme').value = theme;
  const applyTheme = t => { if (t === 'system') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', t); };
  applyTheme(theme);
  $('#theme').onchange = e => { store.set('dh_theme', e.target.value); applyTheme(e.target.value); };
  $$('[data-adv]').forEach(b => b.onclick = async () => {
    try { const r = await api('/clock/advance', { hours: +b.dataset.adv }); toast(`Clock moved ${b.dataset.adv} h. Reminders sent: ${r.sweep.reminders}. Escalations: ${r.sweep.escalations}.`, 5000); } catch (e) { toast(e.message, 6000); }
    await pollCore(); refreshAll(true);
  });
  $('#resetbtn').onclick = async () => {
    $('#drawer').close();
    if (!await dialogPrompt({ title: 'Reset all demo data?', body: 'This deletes every request, message and audit event and restarts the demo clock. Connections and settings stay.', label: 'Type reset to confirm', confirm: 'Reset demo data', match: 'reset', danger: true })) return;
    try { await api('/reset', {}); LOG.chat = {}; LOG.email = {}; Object.keys(SEENMSG).forEach(k => delete SEENMSG[k]); Object.keys(SEEN).forEach(k => delete SEEN[k]); Object.keys(PREV).forEach(k => delete PREV[k]); toast('Demo data reset.'); } catch (e) { toast(e.message, 6000); }
    await pollCore(); render(false);
  };
  const sb = $('#statusbtn');
  sb.onclick = () => { const open = $('#statuspanel').hidden; $('#statuspanel').hidden = !open; sb.setAttribute('aria-expanded', String(open)); };
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#statuspanel').hidden) { $('#statuspanel').hidden = true; sb.setAttribute('aria-expanded', 'false'); sb.focus(); } });
}

async function boot() {
  wireShell();
  try { await loadCore(); } catch (e) { $('#main').innerHTML = errorState('Deskhand', e, () => location.reload()); return; }
  await render(false);
  setInterval(() => { if (!document.hidden && !document.querySelector('dialog[open]')) refreshAll(false); }, 4000);
}
boot();
})();
