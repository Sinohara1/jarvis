'use strict';
window.addEventListener('error', (e) => { try { window.pywebview && window.pywebview.api && window.pywebview.api.log_js(`${e.message} @${e.filename}:${e.lineno}`); } catch (_) {} });
// Джарвис UI — talks to Python via window.pywebview.api (see jarvis_app/webui.py).
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let API = null;
const NM = () => (S.settings && String(S.settings.assistant_name || '').trim()) || 'Джарвис';
const isDefaultName = (n) => /^(джарвис|jarvis)$/i.test(String(n).trim());
const VOICE_RU = { 'ru-RU-DmitryNeural': 'Дмитрий', 'ru-RU-SvetlanaNeural': 'Светлана' };
const vname = (v) => VOICE_RU[v] || pvLabel(v) || String(v).replace(/^[a-z]{2}-[A-Z]{2}-/, '').replace(/Neural$/, '');
const EDGE2PIPER = { 'ru-RU-DmitryNeural': 'ru_RU-dmitri-medium', 'ru-RU-SvetlanaNeural': 'ru_RU-irina-medium' };
function pvLabel(key) { const v = ((S.tts || {}).voices || []).find((x) => x.key === key); return v ? v.label : ''; }
const mb = (b) => (b / 1048576).toFixed(0);
const S = { settings: {}, providers: {}, voices: [], chat: [], focus: {}, stats: {}, state: 'idle', tab: 'home',
            chatMode: false, attach: false, wake: { enabled: false, mode: 'off' }, echo: [], hotkey: 'Ctrl+Alt+J',
            live: { enabled: false, status: '' }, wakeDl: null, tts: { voices: [] }, ttsDl: null, liteModel: '',
            follow: { active: false }, pcHearing: {} };
const FOLLOW_MODES = [['off', 'Выкл'], ['30s', '30 с'], ['2m', '2 мин'], ['10m', '10 мин'], ['always', 'Всегда']];
const TALK = [['rare', 'Редко'], ['some', 'Иногда'], ['often', 'Часто']];
const WAKE_MODES = [['name', 'По имени'], ['hey_jarvis', 'Hey Jarvis'], ['off', 'Выкл']];

const TABS = [
  ['home', 'home', 'Главная'], ['commands', 'terminal', 'Команды'], ['model', 'box', 'Модель'],
  ['voice', 'mic', 'Голос'], ['ai', 'cpu', 'ИИ'], ['focus', 'target', 'Фокус'], ['settings', 'settings', 'Настройки'],
];
const STATE_TXT = { idle: '', listening: 'Слушаю…', thinking: 'Думаю…', speaking: 'Говорю…' };
const WATCH = [[90, 'Автоматически', 'каждые 90 с во время фокуса'], [30, 'Часто', 'каждые 30 с'], [300, 'Редко', 'раз в 5 минут'], [0, 'Выкл', 'только заголовки окон']];
const QUICK = [
  ['target', 'Начать фокус на 25 минут', 'Начни фокус-сессию на 25 минут'],
  ['monitor', 'Что у меня на экране?', 'Что у меня на экране?'],
  ['bell', 'Таймер на 10 минут', 'Поставь таймер на 10 минут'],
  ['clock', 'Сколько времени?', 'Сколько сейчас времени?'],
  ['chart', 'Сколько на счетах?', 'Сколько денег на счетах?'],
  ['stop', 'Остановить фокус', 'Останови фокус-сессию'],
];

// ───────────────────────── boot ─────────────────────────
function boot() {
  $('#winctl [data-win=min]').innerHTML = icon('min', 14);
  $('#winctl [data-win=max]').innerHTML = icon('max', 12);
  $('#winctl [data-win=close]').innerHTML = icon('x', 14);
  $('#burger').innerHTML = icon('menu', 20);
  $('#rail-home').innerHTML = icon('spark', 18);
  $('#rail-new').innerHTML = icon('edit', 17);
  $('#grid-btn').innerHTML = icon('grid', 18);
  $('#wordmark .spark').innerHTML = icon('spark', 24);
  $('#plus').innerHTML = icon('plus', 18);
  $('#slash').innerHTML = icon('slash', 15);
  $('#clip').innerHTML = icon('clip', 17);
  $('#send').innerHTML = icon('up', 18);
  $('#mic').innerHTML = icon('mic', 17);
  $('[data-q=last]').innerHTML = icon('arrow', 14) + 'Последний чат';
  $('#tabs').innerHTML = TABS.map(([k, ic, t]) => `<button class="tab${k === 'home' ? ' active' : ''}" data-tab="${k}">${icon(ic, 16)}<span>${t}</span></button>`).join('');
  bindStatic();
}

function connect() {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.init) { API = window.pywebview.api; start(); return; }
  if (/[?&]mock/.test(location.search)) { const s = document.createElement('script'); s.src = 'mock.js'; s.onload = () => { API = window.MOCK_API; start(); }; document.body.appendChild(s); return; }
  window.addEventListener('pywebviewready', () => { API = window.pywebview.api; start(); }, { once: true });
}

async function start() {
  const d = await API.init();
  S.local = d.local || {};
  Object.assign(S, { settings: d.settings, providers: d.providers, voices: d.voices, abilities: d.abilities, tools: d.tools,
                     chat: d.chat || [], focus: d.focus, stats: d.stats, version: d.version, hotkey: d.hotkey,
                     autostart: d.autostart, wake: d.wake, hasKey: d.has_key, dataDir: d.data_dir, presets: d.presets || [],
                     live: d.live || { enabled: false, status: '' }, tts: d.tts || { voices: [] }, liteModel: d.lite_model || '',
                     memory: d.memory || { facts: [] }, follow: d.follow || { active: false }, pcHearing: d.pc_hearing || {} });
  applyName();
  setState(d.state || 'idle');
  setMax(d.maximized);
  renderComposer();
  renderGreeting();
  renderFollow();
  buildPages();
  if (!d.has_key) toast('Нет API-ключа — добавь его во вкладке «Модель».', 'err', 7000);
}

// ───────────────────────── events from Python ─────────────────────────
window.J = {
  autotest(cmd) {   // set only via JARVIS_AUTOTEST for automated screenshots
    if (cmd.startsWith('tab:')) { selectTab(cmd.slice(4)); return; }
    if (cmd.startsWith('type:')) {   // type:sel=value — set an input and fire change (test hook)
      const i = cmd.indexOf('='); const el = document.querySelector(cmd.slice(5, i));
      if (el) { el.value = cmd.slice(i + 1); el.dispatchEvent(new Event('change', { bubbles: true })); }
      API.log_js('autotest type ' + cmd.slice(5, i) + (el ? '' : ' — not found')); return;
    }
    if (cmd.startsWith('click:')) {   // click:sel1|sel2|… — clicks in order, waiting up to 15 s for each element
      const sels = cmd.slice(6).split('|');
      (async () => { for (const sel of sels) { let el = null; for (let i = 0; i < 150 && !el; i++) { el = document.querySelector(sel); if (!el) await new Promise((r) => setTimeout(r, 100)); }
        API.log_js('autotest click ' + sel + (el ? '' : ' — not found')); if (!el) return; el.scrollIntoView({ block: 'center' }); el.click(); await new Promise((r) => setTimeout(r, 800)); } })();
      return;
    }
    if (cmd === 'chat') { selectTab('home'); enterChat(true); return; }
    if (cmd.startsWith('scroll:')) { const el = document.querySelector(cmd.slice(7)); if (el) el.scrollIntoView({ block: 'start' }); return; }
    $('#input').value = cmd; send();
  },
  onEvents(list) { for (const { e, d } of list) { try { onEvent(e, d || {}); } catch (err) { console.error(err); } } },
};
function onEvent(e, d) {
  switch (e) {
    case 'state': setState(d.state, d.detail); break;
    case 'level': document.documentElement.style.setProperty('--lvl', Math.min(1, (d.level || 0) * 1.6).toFixed(3)); break;
    case 'chat': if (d.role === 'jarvis') dropPartial(); addChat(d); break;
    case 'chat_partial': renderPartial(d); break;
    case 'tts': { const was = S.tts && S.tts.downloading; S.tts = d; if (!d.downloading) S.ttsDl = null; renderTtsStatus();
      if (was && !d.downloading) { if (d.error) toast(d.error, 'err', 7000); else { toast(`Голос «${pvLabel(was)}» скачан — теперь говорю офлайн`, '', 2600); if (S.tab === 'voice') buildVoice(); } }
      break; }
    case 'tts_dl': S.ttsDl = d; renderTtsStatus(); break;
    case 'stt_dl': S.sttDl = d; renderTtsStatus(); break;
    case 'whisper': { const w = (S.tts || {}).whisper || {}; const was = w.downloading; S.tts = Object.assign(S.tts || {}, { whisper: d });
      if (!d.downloading) S.whisperDl = null;
      if (was && !d.downloading) { if (d.error) toast('Whisper: ' + d.error, 'err', 7000); else toast('Модель Whisper скачана', '', 2600); if (S.tab === 'voice') buildVoice(); else renderWhisperStatus(); }
      else renderWhisperStatus();
      break; }
    case 'whisper_dl': S.whisperDl = d; renderWhisperStatus(); break;
    case 'voice_timing': S.tts.timing = d; renderTtsStatus(); break;
    case 'tool': addMsg({ role: 'tool', text: d.label }); break;
    case 'focus': case 'tick': if (d.focus) { S.focus = d.focus; renderFocusLive(); } break;
    case 'notify': if (S.tab !== 'home' || !S.chatMode) toast(d.text); break;
    case 'error': toast(d.text, 'err', 6000); break;
    case 'wake': S.wake = d; if (!d.downloading) S.wakeDl = null; renderComposer(); renderWakeStatus(); if (d.error) toast('Слово-активатор недоступно: ' + d.error, 'err', 7000); break;
    case 'wake_dl': S.wakeDl = d; renderComposer(); renderWakeStatus(); renderTtsStatus(); break;
    case 'live': S.live = { enabled: !!d.enabled, status: d.status || '' }; S.settings.live_mode = !!d.enabled; renderComposer(); renderLiveStatus(); break;
    case 'visible': window.BG && BG.setPaused(!d.visible); break;
    case 'window': setMax(d.maximized); break;
    case 'update': toast(`Доступна новая версия ${d.tag}`, '', 0, [['Посмотреть', openVersions]]); break;
    case 'upd_progress': renderUpdProgress(d); break;
    case 'upd_ready': toast(`Устанавливаю ${d.tag} — ${NM()} перезапустится…`, '', 0); closeModal(); break;
    case 'upd_error': S.installing = null; toast('Не удалось установить: ' + d.error, 'err', 8000); renderVersions(); break;
    case 'local': S.local = d; renderLocalStatus(); renderComposer(); break;
    case 'local_pull': S.localPull = d; if (d.status === 'success') { toast(`Модель ${d.model} скачана`, '', 2600); S.localPull = null; API.local_info(true).then((l) => { S.local = l; if (S.tab === 'model') buildModel(); }); } else if (d.status === 'error') { toast('Не удалось скачать модель: ' + (d.error || ''), 'err', 7000); S.localPull = null; } renderLocalStatus(); break;
    case 'reminders': break;
    case 'memory': if (typeof onMemoryEvent === 'function') onMemoryEvent(d); break;
    case 'follow': S.follow = d; renderFollow(); break;
    case 'pc_hearing': S.pcHearing = Object.assign(S.pcHearing || {}, d); renderPcStatus(); break;
  }
}

function setState(st, detail) {
  S.state = st || 'idle';
  document.body.dataset.state = S.state;
  if (window.BG) { BG.energy = S.state === 'idle' ? 0 : S.state === 'thinking' ? 0.7 : 0.45; if (BG.energy) BG.kick(); else BG.blurredAt = performance.now(); }
  const inp = $('#input');
  inp.placeholder = S.state === 'listening' ? 'Слушаю… говори' : S.state === 'thinking' ? (detail || 'Думаю…') : 'Спросите о чём угодно...';
  const txt = STATE_TXT[S.state] || '';
  const stEl = $('#status');
  const show = txt || (detail && S.state === 'idle' ? detail : '');
  stEl.querySelector('.txt').textContent = S.state === 'thinking' && detail ? detail : (txt || detail || '');
  stEl.classList.toggle('show', !!show);
  if (S.state === 'idle' && detail) setTimeout(() => { if (S.state === 'idle') stEl.classList.remove('show'); }, 2500);
  renderTyping();
}

// ───────────────────────── chat ─────────────────────────
function enterChat(on = true) {
  closePop();
  S.chatMode = on;
  document.body.classList.toggle('chatmode', on);
  if (on) { renderChat(); }
}
function renderChat() {
  const box = $('#msgs');
  box.innerHTML = '';
  for (const m of S.chat) box.appendChild(msgEl(m));
  renderTyping();
  scrollChat(true);
}
function msgEl(m) {
  const el = document.createElement('div');
  const role = m.role === 'jarvis' ? 'jarvis' : m.role === 'user' ? 'user' : m.role === 'tool' ? 'tool' : 'system';
  el.className = `msg ${role}${m.kind === 'nudge' ? ' nudge' : ''}${m.kind === 'live' ? ' live' : ''}`;
  if (role === 'jarvis') el.innerHTML = `<div class="av">${icon(m.kind === 'nudge' ? 'target' : m.kind === 'live' ? 'eye' : 'spark', 14)}</div><div class="col"><div class="who">${esc(NM())}</div><div class="bubble">${esc(m.text)}</div></div>`;
  else if (role === 'user') el.innerHTML = `<div class="bubble">${esc(m.text)}</div>`;
  else if (role === 'tool') el.innerHTML = `<span class="toolchip">${icon('zap', 12)}${esc(m.text)}</span>`;
  else el.innerHTML = `<div class="bubble">${esc(m.text)}</div>`;
  if (m.ts) el.title = m.ts;
  return el;
}
function addMsg(m) {
  m.ts = m.ts || new Date().toTimeString().slice(0, 5);
  S.chat.push(m);
  if (S.chat.length > 300) S.chat.shift();
  if (S.chatMode) {
    const t = $('#msgs .typing'); if (t) t.remove();
    $('#msgs').appendChild(msgEl(m));
    renderTyping();
    scrollChat();
  }
}
function addChat(d) {
  if (d.role === 'user') {
    const i = S.echo.indexOf(d.text);
    if (i >= 0) { S.echo.splice(i, 1); return; }      // already shown optimistically
  }
  addMsg({ role: d.role, text: d.text, kind: d.kind || '' });
  if ((d.role === 'user' || d.role === 'jarvis') && S.tab === 'home' && !S.chatMode) enterChat(true);
}
function renderPartial(d) {   // streamed reply: grows while the model writes (the final 'chat' replaces it)
  if (!S.chatMode) return;
  let el = $('#msgs .msg.partial');
  if (d.done || !d.text) { if (el) el.remove(); return; }
  if (!el) {
    const t = $('#msgs .typing'); if (t) t.remove();
    el = msgEl({ role: 'jarvis', text: '', ts: new Date().toTimeString().slice(0, 5) }); el.classList.add('partial'); $('#msgs').appendChild(el);
  }
  const b = el.querySelector('.bubble') || el; const txt = b.querySelector('.txt') || b;
  txt.textContent = d.text; scrollChat(true);
}
function dropPartial() { const el = $('#msgs .msg.partial'); if (el) el.remove(); }
function renderTyping() {
  const box = $('#msgs');
  const t = $('#msgs .typing');
  if (S.state === 'thinking' && S.chatMode && !$('#msgs .msg.partial')) {
    if (!t) { const el = document.createElement('div'); el.className = 'msg jarvis typing'; el.innerHTML = `<div class="av">${icon('spark', 14)}</div><div class="bubble"><i></i><i></i><i></i></div>`; box.appendChild(el); scrollChat(); }
  } else if (t) t.remove();
}
function scrollChat(instant) { const c = $('#chat'); c.scrollTo({ top: c.scrollHeight, behavior: instant ? 'auto' : 'smooth' }); }

async function send(text, opts = {}) {
  text = (text ?? $('#input').value).trim();
  if (!text) return;
  const cc = (S.settings.custom_commands || []).find((c) => '/' + c.name.toLowerCase() === text.toLowerCase());
  if (cc) text = cc.prompt;
  $('#input').value = '';
  const attach = S.attach || !!opts.attach;
  S.attach = false; $('#clip').classList.remove('on');
  if (S.tab !== 'home') selectTab('home');
  if (!S.chatMode) enterChat(true);
  S.echo.push(attach ? '(Посмотри на мой экран через look_at_screen и учти, что там.) ' + text : text);
  addMsg({ role: 'user', text: attach ? '🖥 ' + text : text });
  setState('thinking');
  try { await API.send(text, attach); } catch (e) { toast('Не удалось отправить: ' + e, 'err'); }
}

// ───────────────────────── composer ─────────────────────────
function modelLabel() {
  const s = S.settings; const p = s.provider; const m = (s.models && s.models[p] && s.models[p].chat) || '';
  const L = S.local || {};
  if (s.ai_route !== 'cloud' && L.model_ok && !(L.game && L.gpu_free)) return localModel() + ' · локально';
  return m || p;
}
function watchLabel() {
  const v = S.settings.screen_check_sec;
  const w = WATCH.find((x) => x[0] === v);
  return w ? w[1] : `Каждые ${v} с`;
}
function renderComposer() {
  const s = S.settings;
  $('#confirm-tgl').classList.toggle('on', !!s.confirm_actions);
  $('#live-tgl').classList.toggle('on', !!S.live.enabled);
  $('#live-wrap').title = 'Живой режим: ассистент сам поглядывает на экран и говорит, только когда это уместно' + (S.live.enabled && S.live.status ? '\nСейчас: ' + S.live.status : '');
  const wb = $('#wake-btn');
  const dl = S.wake.downloading || S.wakeDl;
  const pct = S.wakeDl && S.wakeDl.total ? Math.round(S.wakeDl.done / S.wakeDl.total * 100) : 0;
  wb.innerHTML = icon('mic', 15) + `<span>${S.wake.enabled ? `«${esc(wakePhrase())}»` : dl ? `Модель ${pct}%` : 'Запустить'}</span>`;
  wb.title = S.wake.enabled ? `Слушаю «${wakePhrase()}» — нажми, чтобы выключить` : dl ? 'Скачиваю модель распознавания имени (~45 МБ)…' : 'Звать голосом по имени';
  wb.classList.toggle('on', !!S.wake.enabled);
  const vb = $('#voice-btn');
  vb.innerHTML = icon(s.speak_replies ? 'vol' : 'volx', 17);
  vb.classList.toggle('on', false);
  vb.title = s.speak_replies ? 'Ответы озвучиваются — выключить' : 'Голос выключен — включить';
  $('#model-btn').innerHTML = `<span class="txt">${esc(modelLabel())}</span>${icon('down', 14)}`;
  $('#watch-btn').innerHTML = `${icon(s.screen_check_sec ? 'eye' : 'eyeoff', 14)}<span class="txt">${esc(watchLabel())}</span>${icon('down', 13)}`;
}
function renderGreeting() {
  const h = new Date().getHours();
  const name = String(S.settings.user_name || '').trim();
  const g = h >= 1 && h < 5 ? (name ? `Не спится, ${name}?` : 'Не спится?') : (name ? `Пора работать, ${name}?` : 'Пора работать?');
  $('#greet').textContent = S.focus && S.focus.active && S.focus.task ? `Фокус: ${S.focus.task}` : g;
}

async function save(patch, quiet) {
  const r = await API.save(patch);
  if (!r || !r.ok) { toast((r && r.error) || 'Не удалось сохранить', 'err'); return false; }
  S.settings = r.settings; S.hotkey = r.hotkey; S.hasKey = r.has_key;
  renderComposer();
  if (!quiet) toast('Сохранено', '', 1400);
  return true;
}

// ───────────────────────── popovers ─────────────────────────
let popAnchor = null;
function closePop() { $('#pop').classList.add('hidden'); popAnchor = null; }
function openPop(anchor, html, place = 'below', onClick) {
  const pop = $('#pop');
  if (popAnchor === anchor) { closePop(); return; }
  pop.innerHTML = html; pop.classList.remove('hidden'); popAnchor = anchor;
  const r = anchor.getBoundingClientRect(); const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let x = r.left, y = place === 'above' ? r.top - ph - 8 : r.bottom + 8;
  if (place === 'above' && y < 70) y = 70;
  if (place === 'below' && y + ph > innerHeight - 10) y = Math.max(10, r.top - ph - 8);
  x = Math.min(Math.max(10, x), innerWidth - pw - 10);
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
  pop.onclick = (ev) => { const it = ev.target.closest('[data-v]'); if (it) { closePop(); onClick(it.dataset.v, it); } };
}
const pi = (v, ic, label, sub = '', cls = '') => `<button class="pi ${cls}" data-v="${esc(v)}">${ic ? icon(ic, 15) : ''}<span>${esc(label)}</span>${sub ? `<span class="sub">${esc(sub)}</span>` : ''}</button>`;

function popCommands(anchor) {
  let h = '<div class="ph-h">Быстрые команды</div>' + QUICK.map((q, i) => pi('q' + i, q[0], q[1])).join('');
  const cc = S.settings.custom_commands || [];
  h += '<div class="sep"></div><div class="ph-h">Свои команды</div>';
  h += cc.length ? cc.map((c, i) => pi('c' + i, 'terminal', '/' + c.name, '', '')).join('') : pi('new', 'plus', 'Создать команду…', '', 'dim');
  openPop(anchor, h, S.chatMode ? 'above' : 'below', (v) => {
    if (v === 'new') { selectTab('commands'); setTimeout(() => $('#cc-name') && $('#cc-name').focus(), 250); return; }
    if (v[0] === 'q') send(QUICK[+v.slice(1)][2]);
    else send(cc[+v.slice(1)].prompt);
  });
}
function popPlus(anchor) {
  const h = pi('new', 'edit', 'Новый чат') + pi('screen', 'monitor', 'Спросить про экран') + pi('focus', 'target', 'Фокус-сессия…') +
            pi('timer', 'bell', 'Поставить таймер…') + pi('listen', 'mic', 'Сказать голосом', S.hotkey);
  openPop(anchor, h, S.chatMode ? 'above' : 'below', (v) => {
    if (v === 'new') newChat();
    else if (v === 'screen') { S.attach = true; $('#clip').classList.add('on'); $('#input').focus(); $('#input').placeholder = 'Что спросить про экран?'; }
    else if (v === 'focus') selectTab('focus');
    else if (v === 'timer') { $('#input').value = 'Поставь таймер на 10 минут'; $('#input').focus(); }
    else if (v === 'listen') API.listen();
  });
}
function popModel(anchor) {
  let h = '<div class="ph-h">На этом компьютере</div>';
  const L = S.local || {};
  for (const m of localModels().filter((x) => x.installed)) {
    const sel = S.settings.ai_route !== 'cloud' && m.name === localModel();
    h += `<button class="pi ${sel ? 'sel' : ''}" data-v="${esc('ollama|' + m.name)}"><span>${esc(m.name)}</span><span class="sub">${esc(m.sub)}</span>${icon('check', 14, 'chk')}</button>`;
  }
  if (!localModels().some((x) => x.installed)) h += pi('cfg', 'download', L.installed ? 'Скачать локальную модель…' : 'Установить локальную модель…', '', 'dim');
  for (const [p, info] of Object.entries(S.providers).filter(([, v]) => !v.local)) {
    if (p !== S.settings.provider && !(S.settings.api_keys || {})[p] && !info.chat_models.length) continue;
    const cur = S.settings.models[p].chat;
    const list = Array.from(new Set([cur, ...info.chat_models])).filter(Boolean).slice(0, 6);
    if (!list.length) continue;
    h += `<div class="ph-h">${esc(info.label)}</div>`;
    for (const m of list) {
      const sel = S.settings.ai_route === 'cloud' && S.settings.provider === p && cur === m;
      h += `<button class="pi ${sel ? 'sel' : ''}" data-v="${esc(p + '|' + m)}"><span>${esc(m)}</span>${icon('check', 14, 'chk')}</button>`;
    }
  }
  h += '<div class="sep"></div>' + pi('cfg', 'settings', 'Ключи и модели…');
  openPop(anchor, h, S.chatMode ? 'above' : 'below', async (v) => {
    if (v === 'cfg') { selectTab('model'); return; }
    const [p, m] = v.split('|');
    if (p === 'ollama') { await save({ ai_route: S.settings.ai_route === 'cloud' ? 'local_first' : S.settings.ai_route, models: { ollama: { chat: m, lite: m } } }, true); toast(`Локальная модель: ${m}`, '', 1600); buildPages(); return; }
    await save({ provider: p, models: { [p]: { chat: m } }, ai_route: 'cloud' }, true);
    toast(`Модель: ${p}/${m} (только облако)`, '', 1600);
    buildPages();
  });
}
function popWatch(anchor) {
  const cur = S.settings.screen_check_sec;
  const h = '<div class="ph-h">Проверка экрана в фокусе</div>' + WATCH.map(([v, l, sub]) =>
    `<button class="pi ${cur === v ? 'sel' : ''}" data-v="${v}">${icon(v ? 'eye' : 'eyeoff', 15)}<span>${l}</span><span class="sub">${sub}</span></button>`).join('');
  openPop(anchor, h, S.chatMode ? 'above' : 'below', async (v) => { await save({ screen_check_sec: +v }, true); toast(`Экран: ${watchLabel()}`, '', 1500); buildPages(); });
}
function popBurger(anchor) {
  const top = !!S.settings.always_on_top;
  const h = pi('top', 'pin', top ? 'Не поверх окон' : 'Поверх всех окон') + pi('tray', 'tray', 'Свернуть в трей') + pi('new', 'edit', 'Новый чат') +
            pi('data', 'folder', 'Папка данных') + '<div class="sep"></div>' + pi('quit', 'power', 'Выйти');
  openPop(anchor, h, 'below', async (v) => {
    if (v === 'top') { await save({ always_on_top: !top }, true); buildPages(); }
    else if (v === 'tray') API.hide();
    else if (v === 'new') newChat();
    else if (v === 'data') API.open_data_folder();
    else if (v === 'quit') API.quit();
  });
}
function popGrid(anchor) {
  const f = S.focus || {};
  const h = '<div class="ph-h">Панели</div>' + pi('focus', 'target', 'Фокус и статистика', f.active ? fmt(f.remaining) : '') +
            pi('cmds', 'terminal', `Что умеет ${esc(NM())}`) + pi('cam', 'camera', 'Камера', 'скоро', 'dim') + pi('help', 'keyboard', 'Горячие клавиши', S.hotkey);
  openPop(anchor, h, 'below', (v) => {
    if (v === 'focus') selectTab('focus');
    else if (v === 'cmds') selectTab('commands');
    else if (v === 'cam') toast(`Камера появится в следующих версиях — ${NM()} сможет замечать, что ты отвлёкся на телефон.`, '', 4000);
    else if (v === 'help') toast(`${S.hotkey}: удерживай — говоришь; короткое нажатие — слушаю до паузы; повторное — стоп.`, '', 6000);
  });
}

// ───────────────────────── tabs / pages ─────────────────────────
function selectTab(k) {
  S.tab = k;
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === k));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'v-' + k));
  document.body.classList.toggle('view-page', k !== 'home');
  $('#rail-home').classList.toggle('active', k === 'home' && !S.chatMode);
  closePop();
  if (k === 'focus') refreshStats();
}
function newChat() {
  API.new_chat(); S.chat = []; $('#msgs').innerHTML = '';
  selectTab('home'); enterChat(false); renderComposer(); toast('Новый чат', '', 1200);
}

function fmt(sec) { sec = Math.max(0, Math.round(sec || 0)); const m = Math.floor(sec / 60), s = sec % 60; return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`; }

function buildPages() {
  buildCommands(); buildModel(); buildVoice(); buildAI(); buildFocus(); buildSettings();
}
const tgl = (id, on) => `<span id="${id}" class="toggle${on ? ' on' : ''}"></span>`;
const head = (t, p) => `<div class="ph"><div><h2>${t}</h2>${p ? `<p>${p}</p>` : ''}</div></div>`;

function buildCommands() {
  const ab = S.abilities || [];
  const cc = S.settings.custom_commands || [];
  $('#p-commands').innerHTML = head('Команды', `Скажи или напиши своими словами — ${esc(NM())} сам выберет действие. Нажми на карточку, чтобы попробовать.`) +
    `<div class="grid2" style="margin-bottom:14px">${ab.map((a, i) => `<div class="card abil" data-try="${i}"><div class="top"><div class="ico">${icon(a.icon, 16)}</div><div class="ttl">${esc(a.title)}</div></div>
      <div class="dsc">${esc(a.desc)}</div><div class="ex">«${esc(a.example)}»</div></div>`).join('')}</div>
    <div class="card sect"><h3>${icon('terminal', 15)}Свои команды<span class="badge">в меню «/»</span></h3>
      <div id="cc-list">${cc.length ? cc.map((c, i) => `<div class="row cmd-row"><div class="grow"><div class="name">/${esc(c.name)}</div><div class="prompt">${esc(c.prompt)}</div></div>
        <button class="btn sm ghost" data-run="${i}">${icon('play', 13)}Запустить</button><button class="icon-btn" data-del="${i}" title="Удалить">${icon('trash', 15)}</button></div>`).join('') : '<div class="note" style="margin:0 0 10px">Пока пусто. Например: «утро» → «Открой почту, календарь и скажи, сколько времени».</div>'}</div>
      <div class="grid2" style="grid-template-columns: 1fr 2fr auto; align-items:end; margin-top:10px">
        <div class="field"><label>Название</label><input id="cc-name" class="inp" placeholder="утро"></div>
        <div class="field"><label>Что сказать ассистенту</label><input id="cc-prompt" class="inp" placeholder="Открой почту и скажи, сколько времени"></div>
        <button id="cc-add" class="btn primary">${icon('plus', 15)}Добавить</button>
      </div></div>
    <div class="card sect"><h3>${icon('shield', 15)}Безопасность</h3><div class="note" style="margin:0">${esc(NM())} не удаляет файлы, не закрывает программы и не отправляет сообщения. Скрипты и установщики он не запускает. Включи «Подтверждать опасные действия», чтобы он спрашивал перед открытием программ и сайтов.</div></div>`;
  $$('#p-commands [data-try]').forEach((el) => el.onclick = () => { const a = ab[+el.dataset.try]; selectTab('home'); $('#input').value = a.example; $('#input').focus(); });
  $$('#p-commands [data-run]').forEach((el) => el.onclick = () => send(cc[+el.dataset.run].prompt));
  $$('#p-commands [data-del]').forEach((el) => el.onclick = async () => { const list = cc.filter((_, i) => i !== +el.dataset.del); await save({ custom_commands: list }); buildCommands(); });
  $('#cc-add').onclick = async () => {
    const name = $('#cc-name').value.trim().replace(/^\//, ''), prompt = $('#cc-prompt').value.trim();
    if (!name || !prompt) { toast('Заполни название и текст команды', 'err'); return; }
    await save({ custom_commands: [...cc, { name, prompt }] }); buildCommands();
  };
}

// ── v1.5: local model (Ollama) + cloud provider ──
function cloudName() { const p = S.settings.provider; return ({ gemini: 'Gemini', hubris: 'Hubris', custom: 'свой API' })[p] || ((S.providers[p] || {}).label || p); }
function modelOpts(p, kind) {
  const info = S.providers[p] || {}; const fetched = (S.modelLists || {})[p];
  const base = kind === 'lite' ? (info.lite_models || []) : (info.chat_models || []);
  if (!fetched) return base.map((x) => `<option value="${esc(x)}">`).join('');
  const seen = new Set(); let h = '';
  for (const x of base) { seen.add(x); const f = fetched.find((m) => m.id === x); h += `<option value="${esc(x)}" label="${esc('★ ' + ((f && f.label) || 'рекомендую'))}">`; }
  for (const m of fetched) { if (seen.has(m.id)) continue; if (kind === 'lite' && m.vision === false) continue; h += `<option value="${esc(m.id)}" label="${esc(m.label || '')}">`; }
  return h;
}
function localModel() { return ((S.settings.models || {}).ollama || {}).chat || 'gemma4:12b'; }
function routeOpts() { const c = cloudName(); return [['local_first', `Локально, ${c} запасной`], ['local', 'Только локально'], ['cloud', `Только ${c}`]]; }
function gb(x) { return (+x || 0).toFixed(1).replace('.', ','); }
function localModels() {   // installed first, then recommended ones not yet downloaded
  const L = S.local || {}; const inst = L.models || []; const rec = L.recommended || [];
  const has = (n) => inst.some((m) => m.name === n || m.name === n + ':latest');
  const out = inst.map((m) => ({ name: m.name, label: m.name, sub: `${gb(m.gb)} ГБ${m.params ? ' · ' + m.params : ''}`, installed: true, note: (rec.find((r) => r.name === m.name) || {}).note || '' }));
  rec.filter((r) => !has(r.name)).forEach((r) => out.push({ name: r.name, label: r.name, sub: `скачать ~${gb(r.gb)} ГБ`, installed: false, note: r.note }));
  if (!out.some((m) => m.name === localModel())) out.unshift({ name: localModel(), label: localModel(), sub: 'не скачана', installed: false });
  return out;
}
function localStatusHtml() {
  const L = S.local || {}; const m = localModel(); const c = cloudName(); const pl = S.localPull;
  if (pl && pl.status !== 'success' && pl.status !== 'error') {
    const pct = pl.total ? Math.min(100, Math.round(pl.done / pl.total * 100)) : 0;
    return `<b>Скачиваю ${esc(pl.model)}</b> — ${pl.total ? `${pct}% · ${gb(pl.done / 1e9)} из ${gb(pl.total / 1e9)} ГБ` : esc(pl.status || 'подключаюсь…')}`;
  }
  if (!L.installed) return `Ollama не установлена — без неё локальная модель не работает, отвечает ${esc(c)}.`;
  if (!L.running) return `Ollama установлена, но не запущена. ${esc(NM())} запустит её сам (без окна в трее).`;
  if (!L.model_ok) return `Модель <b>${esc(m)}</b> не скачана. Скачай её здесь — один раз, потом всё работает без интернета.`;
  if (L.game && L.gpu_free) return `Сейчас <b>${esc(L.game)}</b> — видеопамять отдана игре, модель выгружена, отвечает ${esc(c)}. Вернётся сама после игры.`;
  if (L.warming) return `Загружаю <b>${esc(m)}</b> в видеопамять…`;
  const ld = L.loaded;
  if (ld) {
    const gpu = ld.gpu >= 0.99 ? 'целиком на видеокарте' : ld.gpu > 0 ? `на видеокарте ${Math.round(ld.gpu * 100)}%, остальное в ОЗУ` : 'на процессоре';
    return `<b>Готова</b> · ${esc(ld.name)} · ${gpu} · видеопамять ${gb(ld.vram_gb)} ГБ · выгрузится через ${esc(String(L.keep_alive || '10m').replace('m', ' мин').replace('s', ' с'))} простоя`;
  }
  return `Скачана · загрузится в видеопамять при первом вопросе (${L.load_sec ? '~' + gb(L.load_sec) + ' с' : 'несколько секунд'}); пока грузится — отвечает ${esc(c)}.`;
}
function renderLocalStatus() {
  const el = $('#lm-status'); if (!el) return;
  el.innerHTML = localStatusHtml();
  const L = S.local || {}; const pl = S.localPull; const busy = pl && pl.status !== 'success' && pl.status !== 'error';
  const bar = $('#lm-prog'); if (bar) { bar.classList.toggle('on', !!busy); const pct = busy && pl.total ? Math.round(pl.done / pl.total * 100) : 0; bar.querySelector('i').style.width = pct + '%'; }
  const sel = localModels().find((x) => x.name === localModel()) || {};
  const dl = $('#lm-dl'); if (dl) { dl.style.display = (L.installed && !sel.installed && !busy) ? '' : 'none'; }
  const cancel = $('#lm-cancel'); if (cancel) cancel.style.display = busy ? '' : 'none';
  const inst = $('#lm-install'); if (inst) inst.style.display = L.installed ? 'none' : '';
  const ul = $('#lm-unload'); if (ul) ul.style.display = L.loaded ? '' : 'none';
}
function buildModel() {
  const s = S.settings, p = s.provider, info = S.providers[p];
  const m = s.models[p];
  const route = s.ai_route || 'local_first';
  const lms = localModels();
  const cloud = Object.entries(S.providers).filter(([, v]) => !v.local);
  $('#p-model').innerHTML = head('Модель', 'Какой ИИ думает за ассистента: локальная модель на твоей видеокарте или облако.') +
    `<div class="card sect"><h3>${icon('zap', 15)}Где думает ассистент<span class="badge">новое</span></h3>
      <div class="seg" id="route-seg">${routeOpts().map(([k, l]) => `<button data-r="${k}" class="${k === route ? 'on' : ''}">${esc(l)}</button>`).join('')}</div>
      <div class="note">${route === 'local_first' ? `Отвечает модель на этом компьютере — без лимитов и без интернета. Если она не запущена, ещё грузится, занята игрой или ошиблась — тут же отвечает ${esc(cloudName())}.` : route === 'local' ? 'Всё только на этом компьютере: ни запросы, ни скриншоты никуда не уходят. Нужна скачанная модель.' : `Всё через ${esc(cloudName())}, как раньше. Локальная модель не используется и не занимает видеопамять.`}</div></div>
    <div class="card sect" id="lm-card"><h3>${icon('box', 15)}Локальная модель (Ollama)</h3>
      <div class="row"><div class="grow"><div class="t">Состояние</div><div class="s" id="lm-status"></div>
        <div class="vprog" id="lm-prog"><i></i><span></span></div></div>
        <button id="lm-install" class="btn sm" style="display:none">${icon('download', 13)}Скачать Ollama</button>
        <button id="lm-unload" class="btn sm ghost" style="display:none" title="Освободить видеопамять сейчас">Выгрузить</button></div>
      <div class="row"><div class="grow"><div class="t">Модель</div><div class="s">${esc((lms.find((x) => x.name === localModel()) || {}).note || 'Нужна модель с поддержкой картинок и инструментов')}</div></div>
        <select id="lm-model" class="inp" style="max-width:300px">${lms.some((x) => x.installed) ? `<optgroup label="Скачанные">${lms.filter((x) => x.installed).map((x) => `<option value="${esc(x.name)}" ${x.name === localModel() ? 'selected' : ''}>${esc(x.label)} — ${esc(x.sub)}</option>`).join('')}</optgroup>` : ''}
          ${lms.some((x) => !x.installed) ? `<optgroup label="Можно скачать">${lms.filter((x) => !x.installed).map((x) => `<option value="${esc(x.name)}" ${x.name === localModel() ? 'selected' : ''}>${esc(x.label)} — ${esc(x.sub)}</option>`).join('')}</optgroup>` : ''}</select>
        <button id="lm-dl" class="btn sm primary" style="display:none">${icon('download', 13)}Скачать модель</button>
        <button id="lm-cancel" class="btn sm ghost" style="display:none">Отмена</button>
        <button id="lm-test" class="btn sm">${icon('zap', 13)}Проверить</button></div>
      <div class="row"><div class="grow"><div class="t">Освобождать видеопамять в играх</div><div class="s">Игра или тяжёлая графическая программа на переднем плане — модель выгружается, отвечает ${esc(cloudName())}; после игры загружается снова</div></div>${tgl('lm-games', s.gpu_free_in_games !== false)}</div>
      <div class="row"><div class="grow"><div class="t">Держать в видеопамяти после вопроса</div><div class="s">Потом выгружается сама, чтобы не занимать видеокарту зря</div></div>
        <select id="lm-keep" class="inp" style="max-width:140px">${[5, 10, 30, 60].map((v) => `<option value="${v}" ${v === (s.local_keep_alive_min || 10) ? 'selected' : ''}>${v} мин</option>`).join('')}</select></div>
      <div class="row" style="border:0"><span id="lm-res" class="result"></span></div></div>
    <div class="card sect"><h3>${icon('globe', 15)}Облачный провайдер${route === 'local' ? '<span class="badge">не используется</span>' : route === 'local_first' ? '<span class="badge">запасной</span>' : ''}</h3>
      <div class="seg" id="prov-seg">${cloud.map(([k, v]) => `<button data-p="${k}" class="${k === p ? 'on' : ''}">${esc(v.label)}</button>`).join('')}</div>
      <div class="grid2" style="margin-top:16px">
        <div class="field"><label>Модель для разговора</label><input id="m-chat" class="inp" list="dl-chat" placeholder="${info.editable_base ? 'например openai/gpt-4o-mini' : ''}" value="${esc(m.chat)}"><datalist id="dl-chat">${modelOpts(p, 'chat')}</datalist></div>
        <div class="field"><label>Лёгкая модель (проверки экрана, быстрые голосовые ответы)</label><input id="m-lite" class="inp" list="dl-lite" value="${esc(m.lite)}"><datalist id="dl-lite">${modelOpts(p, 'lite')}</datalist></div>
      </div>
      ${info.openai_compat ? `<div class="row" style="border:0;padding-top:6px"><button id="m-list" class="btn sm">${icon('download', 13)}Обновить список моделей</button><span id="m-list-res" class="result">${(S.modelLists || {})[p] ? `В списке ${S.modelLists[p].length} моделей — начни печатать в поле модели` : 'Подтянет все модели сервиса в подсказки (нужен ключ)'}</span></div>` : ''}
      ${info.editable_base ? `<div class="field" style="margin-top:12px"><label>Адрес API (base URL)</label><input id="m-base" class="inp" placeholder="https://openrouter.ai/api/v1" value="${esc(s.base_urls[p] || info.base_url)}">
        <div class="note" style="margin-top:6px">Примеры: OpenRouter — https://openrouter.ai/api/v1 · Groq — https://api.groq.com/openai/v1 · DeepSeek — https://api.deepseek.com/v1 · LM Studio — http://127.0.0.1:1234/v1 (ключ любой)</div></div>` : ''}
      <div class="field" style="margin-top:12px"><label>API-ключ ${esc(info.label)}${info.key_url ? ' · <span class="link" id="key-link">получить ключ</span>' : ''}</label>
        <div class="inp-wrap"><input id="m-key" class="inp" type="password" placeholder="${p === 'hubris' ? 'sk-gw-…' : 'вставь ключ сюда'}" value="${esc(s.api_keys[p] || '')}"><button class="eye" id="key-eye">${icon('eye', 15)}</button></div></div>
      ${info.editable_base ? '' : `<details style="margin-top:10px"><summary class="note" style="cursor:pointer;margin:0">Дополнительно: адрес API</summary>
        <div class="field" style="margin-top:8px"><input id="m-base" class="inp" value="${esc(s.base_urls[p] || info.base_url)}"></div></details>`}
      <div class="row" style="margin-top:12px;border:0"><button id="m-save" class="btn primary">${icon('check', 15)}Сохранить</button><button id="m-test" class="btn">${icon('zap', 15)}Проверить</button><span id="m-res" class="result"></span></div>
      <div class="note">${p === 'hubris' ? 'Hubris — оплата в рублях, сотни моделей одним ключом (Gemini, GPT, Claude, DeepSeek, Qwen, GLM, Kimi и бесплатные). Нужны модели с инструментами (управление ПК) и картинками (живой режим, экран). Если на балансе кончатся деньги, ' + esc(NM()) + ' так и скажет.' : p === 'custom' ? 'Любой сервис с OpenAI-совместимым API (/v1/chat/completions). Для управления ПК модель должна уметь вызывать функции (tools), для экрана — принимать картинки.' : p === 'gemini' ? `Бесплатный Gemini: около 10 запросов в минуту и ограничение в день. При лимите ${esc(NM())} скажет об этом, временно перейдёт на лёгкую модель и реже проверяет экран.` : 'Платный API: запросы списываются с баланса провайдера.'}</div></div>`;
  $$('#route-seg button').forEach((b) => b.onclick = async () => { await save({ ai_route: b.dataset.r }, true); buildModel(); renderComposer(); toast(routeOpts().find((x) => x[0] === b.dataset.r)[1], '', 1600); });
  $('#lm-model').onchange = async (e) => { const v = e.target.value; await save({ models: { ollama: { chat: v, lite: v } } }, true); buildModel(); renderComposer();
    const x = localModels().find((y) => y.name === v); if (x && !x.installed) toast(`Модель ${v} ещё не скачана — нажми «Скачать модель»`, '', 3500); };
  $('#lm-dl').onclick = async () => { const r = await API.local_pull(localModel()); if (!r.ok) toast(r.error || 'Уже что-то скачивается', 'err'); else { S.localPull = { model: localModel(), status: 'подключаюсь…', done: 0, total: 0 }; renderLocalStatus(); } };
  $('#lm-cancel').onclick = () => API.local_cancel_pull();
  $('#lm-install').onclick = () => API.open_link('https://ollama.com/download/windows');
  $('#lm-unload').onclick = async () => { const r = await API.local_unload(); if (r.local) S.local = r.local; renderLocalStatus(); toast('Видеопамять освобождена', '', 1500); };
  $('#lm-test').onclick = async () => { $('#lm-res').textContent = 'Проверяю (первый раз модель грузится несколько секунд)…'; $('#lm-res').textContent = await API.test_ai('ollama', {}); API.local_info(true).then((l) => { S.local = l; renderLocalStatus(); }); };
  $('#lm-games').onclick = async (e) => { const on = !e.currentTarget.classList.contains('on'); await save({ gpu_free_in_games: on }, true); e.currentTarget.classList.toggle('on', on); };
  $('#lm-keep').onchange = async (e) => { await save({ local_keep_alive_min: +e.target.value }, true); };
  renderLocalStatus();
  $$('#prov-seg button').forEach((b) => b.onclick = async () => { await save({ provider: b.dataset.p }, true); buildModel(); renderComposer(); toast('Облачный провайдер: ' + S.providers[b.dataset.p].label, '', 1500); });
  if ($('#key-link')) $('#key-link').onclick = () => API.open_link(info.key_url);
  $('#key-eye').onclick = () => { const k = $('#m-key'); k.type = k.type === 'password' ? 'text' : 'password'; };
  const collect = () => ({ models: { [p]: { chat: $('#m-chat').value.trim(), lite: $('#m-lite').value.trim() } }, api_keys: { [p]: $('#m-key').value.trim() }, base_urls: { [p]: $('#m-base').value.trim() } });
  $('#m-save').onclick = async () => { await save(collect()); };
  $('#m-test').onclick = async () => { $('#m-res').textContent = 'Проверяю…'; const r = await API.test_ai(p, collect()); $('#m-res').textContent = r; };
  if ($('#m-list')) $('#m-list').onclick = async () => {
    const res = $('#m-list-res'); res.textContent = 'Загружаю список…';
    const r = await API.list_models(p, collect());
    if (!r || !r.ok) { res.textContent = (r && r.error) || 'Не удалось получить список'; return; }
    S.modelLists = Object.assign(S.modelLists || {}, { [p]: r.models || [] });
    $('#dl-chat').innerHTML = modelOpts(p, 'chat'); $('#dl-lite').innerHTML = modelOpts(p, 'lite');
    const tools = (r.models || []).filter((x) => x.tools).length;
    res.textContent = `Загружено ${r.count} моделей${tools ? `, с инструментами — ${tools}` : ''}. Начни печатать в поле модели.`;
  };
}

const ANSWER_LANGS = [['auto', 'Как я спросил'], ['ru', 'Русский'], ['uk', 'Українська'], ['en', 'English'], ['de', 'Deutsch'], ['pl', 'Polski']];
const LANG_NAME = { ru: 'Русский', uk: 'Українська', en: 'English', de: 'Deutsch', pl: 'Polski' };
function voiceLang() { const t = S.tts || {}; return S.voiceLang || t.reply_lang || 'ru'; }
function voiceChoice(code) { const s = S.settings; const t = S.tts || {}; return (t.voice_for || {})[code] || (code === 'ru' ? s.piper_voice : ''); }
function voicePatch(code, key) { return code === 'ru' ? { piper_voice: key } : { piper_voices: { [code]: key } }; }
function langOpts(list, cur) { return list.map(([k, l]) => `<option value="${k}" ${k === cur ? 'selected' : ''}>${esc(l)}</option>`).join(''); }
function buildVoice() {
  const s = S.settings; const t = S.tts || { voices: [] };
  const local = s.tts_engine !== 'edge';
  const pvs = t.voices || [];
  const vl = voiceLang();
  const cur = voiceChoice(vl);
  const off = pvs.filter((v) => v.lang === vl && !v.custom);
  const own = pvs.filter((v) => v.lang === vl && v.custom);
  const allOwn = pvs.filter((v) => v.custom);
  const opt = (v) => `<option value="${esc(v.key)}" ${v.key === cur ? 'selected' : ''}>${esc(v.label)}${v.gender ? ` (${v.gender === 'ж' ? 'жен.' : 'муж.'})` : ''}${v.installed ? '' : ` — скачать ~${v.mb || 60} МБ`}</option>`;
  const edgeSel = vl === 'ru' ? `<select id="v-voice" class="inp">${S.voices.map((v) => `<option ${v === s.voice ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>`
    : `<div class="inp ro" id="v-voice-ro">${esc(EDGE_AUTO[vl] || '')}</div>`;
  $('#p-voice').innerHTML = head('Голос', `Как ${esc(NM())} слушает и говорит.`) +
    `<div class="card sect" id="v-langcard"><h3>${icon('globe', 15)}Язык<span class="badge">новое</span></h3>
      <div class="row"><div class="grow"><div class="t">Язык ответов</div><div class="s">Текст и голос: ответы в чате, живой режим, страж фокуса, напоминания, короткие фразы вроде «Открываю». Интерфейс остаётся русским</div></div>
        <select id="v-alang" class="inp" style="max-width:200px">${langOpts(ANSWER_LANGS, s.answer_lang || 'ru')}</select></div>
      <div class="row"><div class="grow"><div class="t">Язык, на котором я говорю</div><div class="s" id="v-sttlang"></div></div>
        <select id="v-slang" class="inp" style="max-width:200px">${langOpts(ANSWER_LANGS.slice(1), s.speech_lang || 'ru')}</select></div>
      <div class="row"><div class="grow"><div class="t">Английские команды</div><div class="s">Если русская модель услышала английскую фразу («write … on Telegram», «play … on Spotify») — перепроверю её английской моделью Vosk (~41 МБ, скачается сама при первой такой фразе)</div></div>${tgl('v-enpass', s.stt_en_pass !== false)}</div>
      <div class="field" style="margin-top:8px"><label>Имена контактов для голоса</label><input id="v-contacts" class="inp" maxlength="1500" placeholder="Nehto, Мама=@mama_tg, Саша" value="${esc((s.voice_contacts || []).join(', '))}"></div>
      <div class="note">Через запятую. Английские имена на слух распознаются плохо — по этому списку «next door» станет «Nehto» в командах вроде «write Nehto on Telegram, hello».
        Можно сразу указать, кого открыть в Telegram: <b>Мама=@mama_tg</b> или <b>Сеня=Семён Петров</b> — скажешь «напиши Сене привет», откроется нужный чат.</div></div>
    ${sttCard()}
    <div class="card sect" id="v-voicecard"><h3>${icon('vol', 15)}Голос ассистента</h3>
      <div class="row"><div class="grow"><div class="t">Синтез речи</div><div class="s" id="v-tstat"></div></div>
        <div class="seg" id="v-engine"><button data-e="piper" class="${local ? 'on' : ''}">Локальный (быстрый)</button><button data-e="edge" class="${local ? '' : 'on'}">Microsoft Edge (онлайн)</button></div></div>
      <div class="grid3" style="margin-top:8px">
        <div class="field"><label>Язык голоса</label><select id="v-vlang" class="inp">${langOpts(ANSWER_LANGS.slice(1), vl)}</select></div>
        <div class="field"><label>${local ? 'Голос (офлайн, Piper)' : 'Голос (Microsoft Edge, нужен интернет)'}</label>
        ${local ? `<select id="v-pvoice" class="inp"><optgroup label="Голоса Piper · ${esc(LANG_NAME[vl])}">${off.map(opt).join('')}</optgroup>${own.length ? `<optgroup label="Свои голоса">${own.map(opt).join('')}</optgroup>` : ''}</select>` : edgeSel}</div>
        <div class="field"><label>&nbsp;</label><button id="v-test" class="btn">${icon('play', 14)}Проверить голос</button></div></div>
      <div class="note" style="margin-top:6px">Голос подбирается по языку каждой фразы: русский ответ — русским голосом, английский — английским. Для каждого языка можно выбрать свой голос; недостающий скачается сам при первом ответе на этом языке.</div>
      <div class="row" style="margin-top:8px"><div class="grow"><div class="t">Скорость речи</div></div><input id="v-rate" class="slider" style="max-width:300px" type="range" min="-50" max="50" value="${s.tts_rate}"><span class="val" id="v-rate-v">${s.tts_rate > 0 ? '+' : ''}${s.tts_rate}%</span></div>
      <div class="row"><div class="grow"><div class="t">Громкость</div></div><input id="v-vol" class="slider" style="max-width:300px" type="range" min="0" max="100" value="${s.tts_volume}"><span class="val" id="v-vol-v">${s.tts_volume}%</span></div>
      <div class="row"><div class="grow"><div class="t">Озвучивать ответы</div><div class="s">Если выключить — ответы только текстом</div></div>${tgl('v-speak', s.speak_replies)}</div></div>
    <div class="card sect" id="v-owncard"><h3>${icon('upload', 15)}Свои голоса</h3>
      <div class="row"><div class="grow"><div class="t">Загрузить свой голос</div><div class="s">Голос Piper: файл <b>.onnx</b> (файл <b>.onnx.json</b> найду рядом) или <b>.zip</b> с обоими. Проверю, что он работает, и включу для его языка.</div></div>
        <button id="v-import" class="btn">${icon('upload', 14)}Загрузить свой голос</button></div>
      <div id="v-ownlist">${allOwn.length ? allOwn.map((v) => `<div class="row own"><div class="grow"><div class="t">${esc(v.label)}</div><div class="s">${esc(LANG_NAME[v.lang] || v.lang_full || v.lang)} · ${v.mb} МБ${voiceChoice(v.lang) === v.key ? ' · <b>используется</b>' : ''}</div></div>
          <button class="btn sm" data-play="${esc(v.key)}" data-lang="${esc(v.lang)}">${icon('play', 13)}Прослушать</button><button class="btn sm ghost" data-del="${esc(v.key)}">${icon('trash', 13)}Удалить</button></div>`).join('')
        : '<div class="s" style="padding:6px 2px">Пока нет своих голосов.</div>'}</div>
      <div class="note">Где взять: <a href="#" data-link="https://huggingface.co/rhasspy/piper-voices">huggingface.co/rhasspy/piper-voices</a> (скачай оба файла голоса) · послушать примеры: <a href="#" data-link="https://rhasspy.github.io/piper-samples/">rhasspy.github.io/piper-samples</a>. Язык голоса берётся из его .onnx.json; голос звучит, когда я отвечаю на этом языке.</div></div>
    <div class="card sect"><h3>${icon('zap', 15)}Скорость ответа</h3>
      <div class="row"><div class="grow"><div class="t">Быстрые ответы</div><div class="s">Голосом отвечает лёгкая модель${S.liteModel ? ' ' + esc(S.liteModel) : ''}: быстрее и больше бесплатный лимит. В чате остаётся основная.</div></div>${tgl('v-fast', s.fast_replies)}</div>
      <div class="row"><div class="grow"><div class="t">Распознавание речи</div><div class="s" id="v-sttstat"></div></div>
        <div class="seg" id="v-stt"><button data-m="local" class="${s.stt_mode !== 'cloud' ? 'on' : ''}">На компьютере</button><button data-m="cloud" class="${s.stt_mode === 'cloud' ? 'on' : ''}">Gemini</button></div></div>
      <div class="row"><div class="grow"><div class="t">Пауза в конце фразы</div><div class="s">Столько тишины — и я считаю, что ты договорил. Меньше — быстрее ответ, но могу перебить на паузе</div></div><input id="v-sil" class="slider" style="max-width:240px" type="range" min="400" max="1500" step="50" value="${s.vad_silence_ms}"><span class="val" id="v-sil-v">${(s.vad_silence_ms / 1000).toFixed(2)} с</span></div>
      <div class="note" id="v-timing"></div></div>
    <div class="card sect"><h3>${icon('keyboard', 15)}Горячая клавиша</h3>
      <div class="row"><div class="grow"><div class="t" id="v-hk">${esc(S.hotkey)}</div><div class="s">Удерживай — говоришь. Короткое нажатие — слушаю до паузы. Повторное — стоп.</div></div><button id="v-hk-btn" class="btn">Изменить</button></div></div>
    ${followCard()}
    <div class="card sect"><h3>${icon('mic', 15)}Слово-активатор</h3>
      <div class="row"><div class="grow"><div class="t">Как звать голосом</div><div class="s" id="v-wstat"></div></div>
        <div class="seg" id="v-wmode">${WAKE_MODES.map(([k, l]) => `<button data-w="${k}" class="${k === s.wake_mode ? 'on' : ''}">${k === 'name' ? esc(`По имени «${NM()}»`) : l}</button>`).join('')}</div></div>
      ${s.wake_mode !== 'off' ? `<div class="row"><div class="grow"><div class="t">Чувствительность</div><div class="s">Меньше — реагирует охотнее (и чаще ошибается)</div></div><input id="v-thr" class="slider" style="max-width:240px" type="range" min="10" max="95" value="${Math.round(thrVal() * 100)}"><span class="val" id="v-thr-v">${thrVal().toFixed(2)}</span></div>` : ''}
      <div class="note">${s.wake_mode === 'hey_jarvis' ? 'Скажи по-английски <b>«Hey Jarvis»</b> — это фиксированная фраза, она не зависит от имени.' :
        `<b>По имени</b>: скажи «${esc(NM())}» или «Эй, ${esc(NM())}» — и сразу просьбу («${esc(NM())}, открой телеграм»), или подожди сигнал. Имя меняется во вкладке «ИИ» — я сразу начну откликаться на новое.`}
        Слово-активатор распознаётся офлайн, звук никуда не отправляется. Имя слушает русская модель Vosk (~45 МБ); просьба после имени распознаётся на твоём языке.</div></div>`;
  $('#v-alang').onchange = async () => { const v = $('#v-alang').value; if (await save({ answer_lang: v }, true)) { S.voiceLang = v === 'auto' ? null : v; toast(`Язык ответов: ${ANSWER_LANGS.find((x) => x[0] === v)[1]}`, '', 1800); buildVoice(); } };
  $('#v-slang').onchange = async () => { const v = $('#v-slang').value; if (await save({ speech_lang: v }, true)) { toast(`Говоришь: ${LANG_NAME[v]}`, '', 1800); buildVoice(); } };
  $('#v-enpass').onclick = async () => { await save({ stt_en_pass: S.settings.stt_en_pass === false }, true); buildVoice(); };
  bindSttCard();
  $('#v-contacts').onchange = () => save({ voice_contacts: $('#v-contacts').value.split(/[,;\n]/).map((x) => x.trim()).filter(Boolean) }, true);
  $('#v-vlang').onchange = () => { S.voiceLang = $('#v-vlang').value; buildVoice(); };
  $$('#v-engine [data-e]').forEach((b) => b.onclick = async () => { if (await save({ tts_engine: b.dataset.e }, true)) { buildVoice(); toast(b.dataset.e === 'piper' ? 'Голос: локальный (быстрый)' : 'Голос: Microsoft Edge (онлайн)', '', 1600); } });
  if ($('#v-pvoice')) $('#v-pvoice').onchange = async () => { const k = $('#v-pvoice').value; if (await save(voicePatch(vl, k), true)) {
    const v = pvs.find((x) => x.key === k); toast(v && !v.installed ? `Скачиваю голос «${v.label}» (~${v.mb || 60} МБ, один раз)…` : `Голос (${LANG_NAME[vl]}): ${pvLabel(k)}`, '', 2400);
    S.tts = await API.tts_info(); buildVoice(); } };
  if ($('#v-voice')) $('#v-voice').onchange = () => save({ voice: $('#v-voice').value });
  $('#v-test').onclick = async () => {
    const r = await API.test_voice(local ? s.voice : ($('#v-voice') ? $('#v-voice').value : ''), +$('#v-rate').value, +$('#v-vol').value, local ? 'piper' : 'edge', local && $('#v-pvoice') ? $('#v-pvoice').value : '', vl);
    if (r && r.downloading) { if (r.tts) { S.tts = r.tts; renderTtsStatus(); } toast(`Сначала скачаю голос «${pvLabel(r.downloading)}» (один раз) — потом нажми ещё раз`, '', 3200); }
  };
  $('#v-import').onclick = () => importVoice();
  $$('#v-ownlist [data-play]').forEach((b) => b.onclick = () => API.test_voice('', +$('#v-rate').value, +$('#v-vol').value, 'piper', b.dataset.play, b.dataset.lang));
  $$('#v-ownlist [data-del]').forEach((b) => b.onclick = async () => {
    const k = b.dataset.del; if (!confirm(`Удалить голос «${pvLabel(k)}»?`)) return;
    const r = await API.delete_voice(k); if (r.settings) S.settings = r.settings; if (r.tts) S.tts = r.tts; toast(r.ok ? 'Голос удалён' : 'Не получилось удалить', r.ok ? '' : 'err', 2000); buildVoice(); });
  $$('#v-owncard [data-link]').forEach((a) => a.onclick = (e) => { e.preventDefault(); API.open_link(a.dataset.link); });
  $('#v-rate').oninput = () => { const v = +$('#v-rate').value; $('#v-rate-v').textContent = (v > 0 ? '+' : '') + v + '%'; };
  $('#v-rate').onchange = () => save({ tts_rate: +$('#v-rate').value }, true);
  $('#v-vol').oninput = () => { $('#v-vol-v').textContent = $('#v-vol').value + '%'; };
  $('#v-vol').onchange = () => save({ tts_volume: +$('#v-vol').value }, true);
  $('#v-fast').onclick = async () => { await save({ fast_replies: !S.settings.fast_replies }, true); buildVoice(); };
  $$('#v-stt [data-m]').forEach((b) => b.onclick = async () => { if (await save({ stt_mode: b.dataset.m }, true)) buildVoice(); });
  $('#v-sil').oninput = () => { $('#v-sil-v').textContent = (+$('#v-sil').value / 1000).toFixed(2) + ' с'; };
  $('#v-sil').onchange = () => save({ vad_silence_ms: +$('#v-sil').value }, true);
  if ($('#v-thr')) {
    $('#v-thr').oninput = () => { $('#v-thr-v').textContent = (+$('#v-thr').value / 100).toFixed(2); };
    $('#v-thr').onchange = () => save({ [S.settings.wake_mode === 'name' ? 'name_threshold' : 'wake_threshold']: +$('#v-thr').value / 100 }, true);
  }
  $$('#v-wmode [data-w]').forEach((b) => b.onclick = () => setWakeMode(b.dataset.w));
  bindFollowCard();
  renderWakeStatus();
  renderTtsStatus();
  renderWhisperStatus();
  $('#v-speak').onclick = async () => { await save({ speak_replies: !S.settings.speak_replies }, true); buildVoice(); };
  $('#v-hk-btn').onclick = captureHotkey;
}
// ── v1.5.x: offline speech recognition engine (Vosk / Whisper) ──
const WH_DEVICES = [['auto', 'Авто'], ['cuda', 'Видеокарта NVIDIA'], ['cpu', 'Процессор']];
const WH_LANGS = [['pair', 'Мой язык + английский'], ['speech', 'Только мой язык'], ['auto', 'Любой (автоопределение)']];
function sttCard() {
  const s = S.settings; const w = (S.tts || {}).whisper || {};
  const wh = s.stt_engine === 'whisper';
  const cat = w.catalog || [];
  return `<div class="card sect" id="v-sttcard"><h3>${icon('mic', 15)}Распознавание речи (офлайн)<span class="badge">новое</span></h3>
      <div class="row"><div class="grow"><div class="t">Движок</div><div class="s">Vosk работает сразу и почти без нагрузки. Whisper заметно точнее (особенно английский и смешанная речь), но нужна загрузка модели и лучше видеокарта NVIDIA</div></div>
        <div class="seg" id="v-stteng"><button data-g="vosk" class="${wh ? '' : 'on'}">Vosk (лёгкий, сразу)</button><button data-g="whisper" class="${wh ? 'on' : ''}">Whisper (точнее, нужна загрузка)</button></div></div>
      ${wh ? `<div class="grid3" style="margin-top:8px">
        <div class="field"><label>Модель Whisper</label><select id="v-whmodel" class="inp">${cat.map((m) => `<option value="${esc(m.key)}" ${m.key === s.whisper_model ? 'selected' : ''}>${esc(m.label)} · ~${m.mb} МБ${m.installed ? ' · скачана' : ''}</option>`).join('')}</select></div>
        <div class="field"><label>Где считать</label><select id="v-whdev" class="inp">${langOpts(WH_DEVICES, s.whisper_device || 'auto')}</select></div>
        <div class="field"><label>Языки фразы</label><select id="v-whlang" class="inp">${langOpts(WH_LANGS, s.whisper_lang || 'pair')}</select></div></div>
      <div class="row" style="margin-top:8px"><div class="grow"><div class="t">Состояние</div><div class="s" id="v-whstat"></div></div>
        <div id="v-whbtns" style="display:flex;gap:6px"></div></div>
      <div class="note">Whisper распознаёт фразу целиком, когда ты договорил: на видеокарте ~0,2–0,6 с, на процессоре 2–8 с. Имя-активатор по-прежнему слушает Vosk, а если Whisper не уверен — подстрахует Vosk/Gemini.
        Модель и библиотека CUDA (~550 МБ, для видеокарты) скачиваются один раз в <b>%LOCALAPPDATA%\\Jarvis\\whisper</b>. Пока Whisper включён, он держит ~1,5–2 ГБ видеопамяти (вместе с локальной моделью ИИ); выключи — память освободится.
        Первый запуск на RTX 50xx дольше (драйвер один раз компилирует ядра).</div>` : `
      <div class="row" style="margin-top:4px"><div class="grow"><div class="t">Английская модель Vosk</div><div class="s">Для проверки английских команд. Точная — заметно меньше ошибок в английских словах, скачается в фоне</div></div>
        <select id="v-enmodel" class="inp" style="max-width:240px">${langOpts([['small', 'Маленькая (~41 МБ)'], ['lgraph', 'Точная (~128 МБ)']], s.stt_en_model || 'small')}</select></div>`}
    </div>`;
}
function bindSttCard() {
  $$('#v-stteng [data-g]').forEach((b) => b.onclick = async () => {
    const g = b.dataset.g; const patch = { stt_engine: g }; if (g === 'whisper') patch.stt_mode = 'local';
    if (await save(patch, true)) { S.tts = Object.assign(S.tts || {}, await API.tts_info()); buildVoice();
      const w = (S.tts || {}).whisper || {};
      if (g === 'whisper') toast(!w.available ? 'Whisper не входит в эту сборку' : (w.catalog || []).some((m) => m.key === S.settings.whisper_model && m.installed) ? 'Распознавание: Whisper' : 'Нажми «Скачать», чтобы загрузить модель Whisper', '', 2600);
      else toast('Распознавание: Vosk', '', 1600); } });
  const ch = (id, key) => { const el = $(id); if (el) el.onchange = async () => { if (await save({ [key]: el.value }, true)) { S.tts = Object.assign(S.tts || {}, await API.tts_info()); buildVoice(); } }; };
  ch('#v-whmodel', 'whisper_model'); ch('#v-whdev', 'whisper_device'); ch('#v-whlang', 'whisper_lang');
  if ($('#v-enmodel')) $('#v-enmodel').onchange = async () => { const v = $('#v-enmodel').value; if (await save({ stt_en_model: v }, true) && v === 'lgraph') toast('Скачиваю точную английскую модель (~128 МБ) в фоне', '', 2400); };
}
function renderWhisperStatus() {
  const el = $('#v-whstat'); const bx = $('#v-whbtns'); if (!el || !bx) return;
  const s = S.settings; const w = (S.tts || {}).whisper || {};
  const m = (w.catalog || []).find((x) => x.key === s.whisper_model) || {};
  const d = S.whisperDl; let txt; let btns = '';
  if (!w.available) txt = 'Whisper не входит в эту сборку (нет faster-whisper) — распознаёт Vosk' + (w.import_error ? ` · ${w.import_error}` : '');
  else if (w.downloading) {
    const pct = d && d.total ? Math.round(d.done / d.total * 100) : 0;
    txt = (d && d.stage === 'cuda' ? 'Скачиваю библиотеку CUDA для видеокарты… ' : `Скачиваю модель ${w.downloading}… `) + (d && d.total ? `${pct}% · ${mb(d.done)} из ${mb(d.total)} МБ` : '');
    btns = `<button class="btn sm ghost" id="v-whcancel">${icon('x', 13)}Отменить</button>`;
  } else if (!m.installed) { txt = `Модель не скачана (~${m.mb || '?'} МБ${w.gpu && s.whisper_device !== 'cpu' && !w.cuda ? ' + ~550 МБ CUDA' : ''}) — пока распознаёт Vosk`; btns = `<button class="btn sm" id="v-whdl">${icon('download', 13)}Скачать</button>`; }
  else if (w.loading) txt = 'Загружаю модель в память…';
  else if (w.loaded && w.loaded_model === s.whisper_model) txt = `Работает: ${m.label || w.loaded_model} · ${w.device === 'cuda' ? 'видеокарта' + (w.gpu ? ' ' + w.gpu : '') : 'процессор'}${w.gpu_error && w.device !== 'cuda' ? ' (видеокарта не запустилась: ' + w.gpu_error + ')' : ''}`;
  else txt = w.error ? `Ошибка: ${w.error}` : 'Скачана — загрузится при следующей фразе';
  if (m.installed && !w.downloading) btns += `<button class="btn sm ghost" id="v-whdel">${icon('trash', 13)}Удалить</button>`;
  if (w.error && w.available && !w.downloading && !txt.includes(w.error)) txt += ` · ${w.error}`;
  el.textContent = txt; bx.innerHTML = btns;
  if ($('#v-whdl')) $('#v-whdl').onclick = async () => { const r = await API.whisper_download(s.whisper_model); if (r && r.whisper) { S.tts = Object.assign(S.tts || {}, { whisper: r.whisper }); renderWhisperStatus(); } };
  if ($('#v-whcancel')) $('#v-whcancel').onclick = () => API.whisper_cancel();
  if ($('#v-whdel')) $('#v-whdel').onclick = async () => { if (!confirm(`Удалить модель Whisper «${m.label || s.whisper_model}» с диска?`)) return;
    const r = await API.whisper_delete(s.whisper_model); if (r && r.whisper) S.tts = Object.assign(S.tts || {}, { whisper: r.whisper }); toast(r && r.ok ? 'Модель удалена' : 'Не получилось удалить', r && r.ok ? '' : 'err', 2000); buildVoice(); };
}
const EDGE_AUTO = { uk: 'uk-UA-OstapNeural / PolinaNeural (по полу русского голоса)', en: 'en-US-GuyNeural / JennyNeural (по полу русского голоса)',
  de: 'de-DE-ConradNeural / KatjaNeural (по полу русского голоса)', pl: 'pl-PL-MarekNeural / ZofiaNeural (по полу русского голоса)' };
async function importVoice(path = '', json = '') {
  const btn = $('#v-import'); if (btn) { btn.disabled = true; btn.textContent = 'Проверяю голос…'; }
  try {
    let r = await API.import_voice(path, json);
    if (r && r.need_json) {
      toast('Не нашёл рядом файл настроек голоса (.onnx.json) — выбери его', '', 3500);
      const j = await API.pick_voice_file('json');
      if (!j) { buildVoice(); return; }
      r = await API.import_voice(r.path, j);
    }
    if (!r || r.cancelled) { buildVoice(); return; }
    if (r.settings) S.settings = r.settings; if (r.tts) S.tts = r.tts;
    if (r.ok) { const v = r.voice || {}; if (r.supported) S.voiceLang = v.lang;
      toast(r.supported ? `Голос «${v.label}» добавлен (${r.lang_label}) и включён для этого языка` : `Голос «${v.label}» добавлен, но язык ${r.lang_label} я пока не говорю`, '', 4000); }
    else toast('Голос не подошёл: ' + (r.error || 'ошибка'), 'err', 7000);
  } finally { buildVoice(); }
}
function renderTtsStatus() {
  const s = S.settings; const t = S.tts || {};
  const vl = voiceLang();
  const el = $('#v-tstat');
  if (el) {
    const key = voiceChoice(vl);
    const v = (t.voices || []).find((x) => x.key === key) || {};
    const dlKey = (t.downloads || []).find((k) => k === key) || t.downloading;
    const dl = S.ttsDl && dlKey && S.ttsDl.key === dlKey ? S.ttsDl : null;
    let txt;
    if (dlKey) txt = `Скачиваю голос «${pvLabel(dlKey)}»… ` + (dl && dl.total ? `${Math.round(dl.done / dl.total * 100)}% · ${mb(dl.done)} из ${mb(dl.total)} МБ` : '');
    else if (s.tts_engine === 'edge') txt = 'Через интернет: каждая фраза 1–10 с, сервис иногда не отвечает';
    else if (t.error) txt = t.error + ' — пока говорю голосом Microsoft Edge';
    else if (!v.installed) txt = `Голос для языка «${LANG_NAME[vl]}» ещё не скачан — пока на этом языке говорю голосом Microsoft Edge`;
    else txt = `Офлайн, на этом компьютере: фраза за ~0,1 с · ${LANG_NAME[vl]}: ${v.label || key}`;
    el.textContent = txt;
  }
  const sl = $('#v-sttlang');
  if (sl) {
    const x = t.stt || {}; const d = (S.sttDl && S.sttDl.lang === x.lang) ? S.sttDl : (x.lang === 'ru' ? S.wakeDl : null) || {};
    sl.textContent = s.stt_mode === 'cloud' ? 'Распознаёт Gemini (выбрано в «Скорость ответа»)'
      : x.error ? x.error + ' — пока распознаёт Gemini'
      : (s.stt_engine === 'whisper' && ((t.whisper || {}).loaded)) ? `Распознаю офлайн (Whisper, ${LANG_NAME[x.lang] || ''}${s.whisper_lang === 'pair' && x.lang !== 'en' ? ' + английский' : ''})`
      : x.model ? `Распознаю офлайн (Vosk, ${LANG_NAME[x.lang] || ''})`
      : x.downloading ? `Скачиваю модель распознавания (${LANG_NAME[x.lang] || ''}, ~${x.mb} МБ)… ${d.total ? Math.round(d.done / d.total * 100) + '%' : ''}`
      : `Модель для этого языка (~${x.mb || 50} МБ) скачается при первой фразе — пока распознаёт Gemini`;
  }
  const st = $('#v-sttstat');
  if (st) {
    const x = t.stt || {}; const d = S.wakeDl || {};
    st.textContent = s.stt_mode === 'cloud' ? 'Звук уходит в Gemini: точнее на шуме, но +1–2 с и ещё один запрос к лимиту'
      : (s.stt_engine === 'whisper' && ((t.whisper || {}).loaded)) ? 'Whisper, офлайн: точнее, фраза распознаётся целиком после паузы. Если не уверен — Vosk, потом Gemini'
      : x.model ? 'Vosk, офлайн: текст готов сразу, как договорил. Если не уверен в словах — спрошу Gemini'
      : (x.downloading || S.wakeDl) ? `Скачиваю модель распознавания… ${d.total ? Math.round(d.done / d.total * 100) : 0}%`
      : 'Модель распознавания не скачана — скачаю, пока распознаёт Gemini';
  }
  const tm = $('#v-timing');
  if (tm) {
    const x = t.timing;
    const f = (v) => v == null ? '—' : v.toFixed(2).replace('.', ',');
    tm.innerHTML = x && x.first_audio != null ? `Последний голосовой ответ: <b>${f(x.first_audio)} с</b> от конца фразы до первого звука
      (пауза ${f(x.endpoint)} · распознавание ${f(x.stt)}${x.stt_mode ? ' ' + (x.stt_mode === 'cloud' ? 'Gemini' : x.stt_mode === 'whisper' ? 'Whisper' : 'локально') : ''} · модель ${f(x.llm_first)} · синтез ${f(x.tts)} · старт ${f(x.play)}).`
      : 'Ответ озвучивается по предложениям, пока модель ещё пишет: первая фраза звучит сразу.';
  }
}
function thrVal() { const s = S.settings; return s.wake_mode === 'name' ? (s.name_threshold ?? 0.5) : s.wake_threshold; }
function captureHotkey() {
  const btn = $('#v-hk-btn'), lbl = $('#v-hk');
  lbl.textContent = 'Нажми новое сочетание… (Esc — отмена)'; btn.disabled = true;
  const onKey = async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (e.key === 'Escape') { done(); lbl.textContent = S.hotkey; return; }
    if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) return;
    const parts = []; if (e.ctrlKey) parts.push('ctrl'); if (e.altKey) parts.push('alt'); if (e.shiftKey) parts.push('shift'); if (e.metaKey) parts.push('windows');
    let k = e.code.startsWith('Key') ? e.code.slice(3).toLowerCase() : e.code.startsWith('Digit') ? e.code.slice(5) : /^F\d+$/.test(e.key) ? e.key.toLowerCase() : e.key.toLowerCase();
    if (k === ' ') k = 'space';
    if (!parts.length && !/^f\d+$/.test(k)) { lbl.textContent = 'Нужен модификатор (Ctrl/Alt/Shift) или F-клавиша'; return; }
    parts.push(k); done();
    if (await save({ hotkey: parts.join('+') })) lbl.textContent = S.hotkey; else lbl.textContent = S.hotkey;
  };
  const done = () => { window.removeEventListener('keydown', onKey, true); btn.disabled = false; };
  window.addEventListener('keydown', onKey, true);
}
function wakePhrase() { return S.wake.mode === 'hey_jarvis' ? 'Hey Jarvis' : NM(); }
function wakeHint() { return S.wake.mode === 'hey_jarvis' ? 'Скажи «Hey Jarvis», чтобы позвать меня' : `Скажи «${NM()}» или «Эй, ${NM()}» — можно сразу с просьбой`; }
async function toggleWake() {
  const on = !(S.wake.enabled || S.wake.downloading);
  const r = await API.set_wake(on);
  if (r && r.ok) { S.settings = r.settings; S.wake = r.wake; renderComposer();
    toast(S.wake.enabled ? wakeHint() : S.wake.downloading ? 'Скачиваю модель распознавания (~45 МБ, один раз)…' : 'Слово-активатор выключено', '', 2600); }
}
async function setWakeMode(mode) {
  const r = await API.set_wake_mode(mode);
  if (r && r.ok) { S.settings = r.settings; S.wake = r.wake; renderComposer(); buildVoice();
    if (mode === 'off') toast('Слово-активатор выключено', '', 1600);
    else if (S.wake.downloading) toast('Скачиваю модель распознавания (~45 МБ, один раз)…', '', 3000);
    else if (S.wake.enabled) toast(wakeHint(), '', 2600); }
}
function renderWakeStatus() {
  const el = $('#v-wstat'); if (!el) return;
  const w = S.wake;
  let t = '';
  if (w.mode === 'off') t = 'Выключено — зови горячей клавишей ' + S.hotkey;
  else if (w.downloading || S.wakeDl) { const d = S.wakeDl || {}; const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
    t = `Скачиваю модель распознавания… ${pct}%` + (d.total ? ` · ${(d.done / 1048576).toFixed(1)} из ${(d.total / 1048576).toFixed(1)} МБ` : ''); }
  else if (w.error) t = 'Не работает: ' + w.error;
  else if (w.enabled) t = w.mode === 'name' ? `Слушаю «${NM()}» и «Эй, ${NM()}»` : 'Слушаю «Hey Jarvis»';
  else t = 'Запускаю…';
  el.textContent = t;
}
async function toggleLive() {
  const on = !S.live.enabled;
  const r = await API.set_live(on);
  if (r && r.ok) { S.settings = r.settings; S.live = r.live; renderComposer(); buildAI();
    toast(on ? 'Живой режим включён: подскажу, когда это уместно' : 'Живой режим выключен', '', 2400); }
}
// ── v1.6 conversation mode chip + PC hearing status ──
function renderFollow() {
  const el = $('#follow-chip'); if (!el) return;
  const f = S.follow || {};
  el.classList.toggle('hidden', !f.active);
  if (!f.active) return;
  el.classList.toggle('wait', !f.listening);
  const txt = f.always ? 'слушаю всегда' : `слушаю… ещё ${esc(f.left || '')}`;
  el.innerHTML = `<span class="fdot"></span><span>${txt}</span><span class="x">${icon('x', 11)}</span>`;
  el.title = (f.listening ? 'Можно продолжать без имени' : 'Подожду, пока договорю') + ' — нажми, чтобы закончить (или скажи «хватит»)';
}
async function followStop() { const r = await API.follow_stop(); if (r && r.follow) { S.follow = r.follow; renderFollow(); } }
function renderPcStatus() {
  const el = $('#v-pchstat'); if (!el) return;
  const p = S.pcHearing || {};
  let t;
  if (!S.settings.pc_hearing) t = p.available === false ? `Недоступно в этой сборке: ${p.import_error || ''}` : 'Выключено';
  else t = p.status || 'Запускаю…';
  el.textContent = t;
}
async function refreshPcPeek() {
  const box = $('#v-pcpeek'); if (!box) return;
  const p = await API.pc_hearing_info(); S.pcHearing = p; renderPcStatus();
  const r = p.recent || [];
  box.innerHTML = r.length ? r.map((x) => `<div><span class="tm">${esc(x.t)}</span>${esc(x.text)}${x.window ? ` <span class="tm">· ${esc(x.window)}</span>` : ''}</div>`).join('')
    : '<div class="tm">Пока ничего не расслышал. Включи видео с речью — через пару секунд здесь появится текст.</div>';
}
function followCard() {
  const s = S.settings; const fm = s.follow_mode || '2m';
  return `<div class="card sect" id="v-followcard"><h3>${icon('mic', 15)}Разговор без имени<span class="badge">новое</span></h3>
      <div class="row"><div class="grow"><div class="t">Слушать после ответа</div><div class="s">После ответа можно продолжать без «${esc(NM())}»: таймер начинается заново после каждого ответа. «Хватит», «пока» или «стоп слушать» — закончить разговор</div></div>
        <div class="seg" id="v-follow">${FOLLOW_MODES.map(([k, l]) => `<button data-f="${k}" class="${k === fm ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      ${fm === 'always' ? `<div class="note">«Всегда»: микрофон слушает постоянно, имя не нужно. Обрывки, шум и чужие разговоры отсеиваются, а если фраза явно не мне — промолчу. Звук игр и видео из колонок лучше отсеивается, если включить «Слышать звук ПК». В наушниках работает лучше всего.</div>` : ''}
      <div class="row"><div class="grow"><div class="t">Слышать звук ПК</div><div class="s" id="v-pchstat"></div></div>${tgl('v-pch', !!s.pc_hearing)}</div>
      ${s.pc_hearing ? `<div class="row"><div class="grow"><div class="t">Что я слышу с ПК</div><div class="s">Последние распознанные фразы (хранятся только в памяти ~3 минуты)</div></div><button class="btn sm" id="v-pcref">${icon('refresh', 13)}Обновить</button></div><div class="pcpeek" id="v-pcpeek"></div>` : ''}
      <div class="note">Звук ПК (видео, игры, звонки) я распознаю у себя на компьютере и помню ~3 минуты — можно спросить «что он сейчас сказал?». Это только контекст: команды из колонок я не выполняю. Пока я говорю, звук ПК не слушаю.
        Распознаёт Whisper на видеокарте (если включён и игра не занимает видеокарту), иначе Vosk на процессоре; музыка и неразборчивая речь распознаются плохо. Звук никуда не отправляется — в модель уходит только текст.</div></div>`;
}
function bindFollowCard() {
  $$('#v-follow [data-f]').forEach((b) => b.onclick = async () => { const v = b.dataset.f; if (await save({ follow_mode: v }, true)) {
    toast(v === 'off' ? 'После ответа снова нужно имя' : v === 'always' ? 'Слушаю всегда — имя не нужно' : `Слушаю ${FOLLOW_MODES.find((x) => x[0] === v)[1]} после ответа`, '', 2200); buildVoice(); } });
  $('#v-pch').onclick = async () => { const on = !S.settings.pc_hearing; if (await save({ pc_hearing: on }, true)) {
    toast(on ? 'Слушаю звук ПК (только как контекст)' : 'Звук ПК больше не слушаю', '', 2200); buildVoice(); if (on) setTimeout(refreshPcPeek, 2500); } };
  if ($('#v-pcref')) { $('#v-pcref').onclick = refreshPcPeek; refreshPcPeek(); }
  renderPcStatus();
}
function renderLiveStatus() {
  const el = $('#a-lstat'); if (el) el.textContent = S.live.enabled ? (S.live.status || 'Включён') : 'Выключен';
  const t = $('#a-live'); if (t) t.classList.toggle('on', !!S.live.enabled);
}

function applyName() {
  const n = NM();
  const wm = $('#wordmark .wm');
  if (wm) { wm.textContent = isDefaultName(n) ? 'Jarvis' : n; $('#wordmark').classList.toggle('long', n.length > 9); $('#wordmark').classList.toggle('xlong', n.length > 14); }
  document.title = n;
  const t = $('#wake-btn'); if (t) t.title = S.wake.mode === 'hey_jarvis' ? 'Слово-активатор «Hey Jarvis»' : `Слово-активатор: позови «${n}»`;
  renderComposer();
  $$('#msgs .msg.jarvis .who').forEach((el) => { el.textContent = n; });
}
function presetOf(key) { return (S.presets || []).find((p) => p.key === key); }
function buildAI() {
  const s = S.settings;
  const opts = [[0, 'Выключено'], [30, 'Каждые 30 с'], [60, 'Каждую минуту'], [90, 'Каждые 90 с'], [180, 'Каждые 3 мин'], [300, 'Каждые 5 мин']];
  if (!opts.some((o) => o[0] === s.screen_check_sec)) opts.push([s.screen_check_sec, `Каждые ${s.screen_check_sec} с`]);
  const pk = s.character_preset || 'butler';
  const pr = presetOf(pk);
  const charText = String(s.character || '').trim() || (pr ? pr.prompt : '');
  const local = s.tts_engine !== 'edge';
  const prVoice = pr ? (local ? (EDGE2PIPER[pr.voice] || s.piper_voice) : pr.voice) : '';
  const curVoice = local ? s.piper_voice : s.voice;
  const sugg = pr && (prVoice !== curVoice || pr.rate !== s.tts_rate) ? { ...pr, voice: prVoice } : null;
  $('#p-ai').innerHTML = head('ИИ', 'Имя, характер, память компаньона, управление ПК и как он следит за фокусом.') +
    `<div class="card sect"><h3>${icon('spark', 15)}Имя</h3>
      <div class="grid2"><div class="field"><label>Как зовут ассистента</label><input id="a-aname" class="inp" maxlength="30" placeholder="Джарвис" value="${esc(s.assistant_name || 'Джарвис')}"></div>
        <div class="field"><label>Как ассистенту обращаться к тебе</label><input id="a-name" class="inp" maxlength="40" placeholder="Имя (необязательно)" value="${esc(s.user_name)}"></div></div>
      <div class="note">Имя видно в окне, трее и чате, и так ассистент называет себя в ответах. ${s.wake_mode === 'name' ? `Голосом зови по имени: <b>«${esc(s.assistant_name || 'Джарвис')}»</b> или «Эй, ${esc(s.assistant_name || 'Джарвис')}» — новое имя подхватывается сразу.` : s.wake_mode === 'hey_jarvis' ? 'Сейчас слово-активатор — «Hey Jarvis». Во вкладке «Голос» можно звать по имени.' : 'Во вкладке «Голос» можно включить вызов голосом по этому имени.'}</div></div>
    <div class="card sect"><h3>${icon('user', 15)}Характер</h3>
      <div class="pchips">${(S.presets || []).map((p) => `<button class="pchip${p.key === pk ? ' on' : ''}" data-preset="${p.key}">${esc(p.label)}</button>`).join('')}<button class="pchip${pk === 'custom' ? ' on' : ''}" data-preset="custom">${icon('edit', 12)}Свой</button></div>
      <div class="field" style="margin-top:12px"><label>Характер — как ассистент говорит и ведёт себя (можно переписать своими словами)</label>
        <textarea id="a-char" class="inp" rows="3" maxlength="2000" placeholder="Например: весёлый пират, говорит «йо-хо-хо», но помогает с домашкой">${esc(charText)}</textarea></div>
      ${sugg ? `<div class="row vsug"><div class="grow"><div class="t">Голос под характер: ${esc(vname(sugg.voice))}, скорость ${sugg.rate > 0 ? '+' : ''}${sugg.rate}%</div><div class="s">Сейчас: ${esc(vname(curVoice))}, ${s.tts_rate > 0 ? '+' : ''}${s.tts_rate}%</div></div>
        <button id="a-vtry" class="btn ghost sm">${icon('play', 13)}Послушать</button><button id="a-vapply" class="btn sm">${icon('check', 13)}Применить</button></div>` : ''}
      <div class="field" style="margin-top:12px"><label>Дополнительные пожелания (что важно знать о тебе, о чём помнить)</label>
        <textarea id="a-persona" class="inp" placeholder="Например: я учусь в 10 классе, готовлюсь к ЕГЭ. Шути иногда.">${esc(s.persona)}</textarea></div>
      <div class="row" style="margin-top:6px"><div class="grow"><div class="t">Подтверждать опасные действия</div><div class="s">Спрашивать перед открытием программ, файлов и сайтов</div></div>${tgl('a-confirm', s.confirm_actions)}</div>
      <div class="field" style="margin-top:12px"><label>Управление компьютером</label>
        <select id="a-pc" class="inp">
          <option value="safe" ${s.pc_control === 'safe' ? 'selected' : ''}>Безопасный — открыть, поиск, экран, громкость</option>
          <option value="standard" ${(s.pc_control || 'standard') === 'standard' ? 'selected' : ''}>Стандарт — + музыка и медиаклавиши (Spotify)</option>
          <option value="full" ${s.pc_control === 'full' ? 'selected' : ''}>Полный — полный доступ и контроль над ПК (окна, ввод, команды, телеграм, скрипты от админа…)</option>
        </select></div>
      <div class="note">«Полный» = полный доступ и контроль над ПК: закрытие/фокус/сворачивание окон, ввод текста и горячие клавиши, shell-команды (опасные — только после твоего «да»), Telegram Desktop, скрипты .ps1/.bat/.py с окном UAC, блокировка, сон и гашение монитора, скриншот/заголовок активного окна. Жёсткие запреты: нет тихого обхода UAC; нет массового удаления файлов и форматирования дисков; закрытие окон мягкое (без kill процесса). Telegram — best-effort (нужен открытый и залогиненный клиент). Секреты и тексты сообщений в лог не пишутся.</div>
      <div class="row"><button id="a-save" class="btn primary">${icon('check', 15)}Сохранить</button><button id="a-reset" class="btn ghost">${icon('refresh', 14)}Новый чат — очистить историю разговора</button></div></div>
    ${typeof memoryCard === 'function' ? memoryCard() : ''}
    <div class="card sect" id="a-livecard"><h3>${icon('eye', 15)}Живой режим<span class="badge">новое</span></h3>
      <div class="row"><div class="grow"><div class="t">Сам поглядывать на экран и подсказывать</div><div class="s" id="a-lstat">${esc(S.live.enabled ? (S.live.status || 'Включён') : 'Выключен')}</div></div>${tgl('a-live', S.live.enabled)}</div>
      <div class="row"><div class="grow"><div class="t">Разговорчивость</div><div class="s">Редко — только очевидные случаи; часто — смелее и с меньшими паузами</div></div>
        <div class="seg" id="a-talk">${TALK.map(([k, l]) => `<button data-t="${k}" class="${k === s.live_talk ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="grid3" style="margin-top:10px">
        <div class="field"><label>Смотреть на экран</label><select id="a-lint" class="inp">${selOpts([[30, 'каждые 30 с'], [45, 'каждые 45 с'], [60, 'каждую минуту'], [90, 'каждые 90 с'], [120, 'каждые 2 мин'], [180, 'каждые 3 мин']], s.live_interval_sec, (v) => `каждые ${v} с`)}</select></div>
        <div class="field"><label>Пауза между репликами</label><select id="a-lgap" class="inp">${selOpts([[60, '1 мин'], [120, '2 мин'], [180, '3 мин'], [300, '5 мин'], [600, '10 мин'], [900, '15 мин']], s.live_min_gap_sec, (v) => `${Math.round(v / 60)} мин`)}</select></div>
        <div class="field"><label>Ждать ответ на вопрос</label><select id="a-lrep" class="inp">${selOpts([[0, 'не слушать'], [5, '5 с'], [8, '8 с'], [12, '12 с'], [20, '20 с']], s.live_reply_sec, (v) => `${v} с`)}</select></div>
      </div>
      <div class="note">Включается и голосом: «следи и подсказывай», выключается — «тихо», «хватит», «выключи живой режим». Молчу, пока ты говоришь со мной, во время звонков и полноэкранных игр/видео, и первые 20 секунд после смены окна. Во время фокус-сессии про отвлечения напоминает страж — без двойных замечаний. Снимок экрана уменьшается, уходит только в модель и не сохраняется на диск. При лимите бесплатного API смотрю реже.</div></div>
    <div class="card sect"><h3>${icon('shield', 15)}Страж фокуса</h3>
      <div class="grid2">
        <div class="field"><label>Смотреть на экран (ИИ, во время фокуса)</label><select id="a-screen" class="inp">${opts.map(([v, l]) => `<option value="${v}" ${v === s.screen_check_sec ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
        <div class="field"><label>Проверять заголовок окна, с</label><input id="a-title" class="inp" type="number" min="2" max="60" value="${s.title_check_sec}"></div>
        <div class="field"><label>Пауза между напоминаниями, с</label><input id="a-cool" class="inp" type="number" min="15" max="900" value="${s.nudge_cooldown_sec}"></div>
        <div class="field"><label>Сколько можно «подсмотреть» без замечания, с</label><input id="a-grace" class="inp" type="number" min="0" max="120" value="${s.distraction_grace_sec}"></div>
      </div>
      <div class="note">Напоминания звучат в выбранном характере. Скриншот уменьшается и уходит только в лёгкую модель вместе с твоей задачей; на диск ничего не сохраняется. Заголовки окон проверяются локально, без ИИ.</div></div>`;
  $$('#p-ai [data-preset]').forEach((b) => { b.onclick = () => pickPreset(b.dataset.preset); });
  $('#a-aname').onchange = () => saveName();
  $('#a-aname').onkeydown = (e) => { if (e.key === 'Enter') e.target.blur(); };
  if (sugg) {
    $('#a-vtry').onclick = () => local ? API.test_voice('', sugg.rate, s.tts_volume, 'piper', sugg.voice) : API.test_voice(sugg.voice, sugg.rate, s.tts_volume, 'edge', '');
    $('#a-vapply').onclick = async () => { if (await save(local ? { piper_voice: sugg.voice, tts_rate: sugg.rate } : { voice: sugg.voice, tts_rate: sugg.rate }, true)) { toast(`Голос: ${vname(sugg.voice)}`, '', 1800); buildAI(); buildVoice(); } };
  }
  $('#a-confirm').onclick = async () => { await save({ confirm_actions: !S.settings.confirm_actions }, true); buildAI(); };
  $('#a-pc').onchange = async () => {
    const v = $('#a-pc').value;
    if (await save({ pc_control: v }, true)) {
      const labels = { safe: 'безопасный', standard: 'стандарт', full: 'полный' };
      toast('Управление ПК: ' + (labels[v] || v), '', 1800);
      buildAI(); buildCommands();
    }
  };
  $('#a-save').onclick = async () => {
    const txt = $('#a-char').value.trim(); const cur = presetOf(S.settings.character_preset);
    const patch = { user_name: $('#a-name').value.trim(), persona: $('#a-persona').value, assistant_name: $('#a-aname').value.trim() || 'Джарвис' };
    if (cur && txt === cur.prompt) Object.assign(patch, { character: '' });
    else Object.assign(patch, { character_preset: txt ? 'custom' : (S.settings.character_preset === 'custom' ? 'butler' : S.settings.character_preset), character: txt });
    if (await save(patch)) { nameChanged(); buildAI(); }
  };
  $('#a-reset').onclick = () => newChat();
  $('#a-screen').onchange = () => save({ screen_check_sec: +$('#a-screen').value });
  $('#a-title').onchange = () => save({ title_check_sec: +$('#a-title').value });
  $('#a-cool').onchange = () => save({ nudge_cooldown_sec: +$('#a-cool').value });
  $('#a-grace').onchange = () => save({ distraction_grace_sec: +$('#a-grace').value });
  $('#a-live').onclick = () => toggleLive();
  $$('#a-talk [data-t]').forEach((b) => b.onclick = async () => { if (await save({ live_talk: b.dataset.t }, true)) { buildAI(); toast('Разговорчивость: ' + b.textContent, '', 1500); } });
  $('#a-lint').onchange = () => save({ live_interval_sec: +$('#a-lint').value });
  $('#a-lgap').onchange = () => save({ live_min_gap_sec: +$('#a-lgap').value });
  $('#a-lrep').onchange = () => save({ live_reply_sec: +$('#a-lrep').value });
  if (typeof bindMemory === 'function') bindMemory();
}
function selOpts(opts, cur, fmt) {
  if (!opts.some((o) => o[0] === cur)) opts = [...opts, [cur, fmt(cur)]].sort((a, b) => a[0] - b[0]);
  return opts.map(([v, l]) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${l}</option>`).join('');
}
async function saveName() {
  const n = $('#a-aname').value.trim() || 'Джарвис';
  if (n === NM()) return;
  if (await save({ assistant_name: n }, true)) { nameChanged(); toast(`Теперь меня зовут ${n}`, '', 2000); }
}
function nameChanged() { applyName(); renderGreeting(); buildCommands(); buildModel(); buildVoice(); buildFocus(); buildSettings(); }
async function pickPreset(key) {
  if (key === 'custom') { const t = $('#a-char'); t.focus(); t.select(); toast('Опиши характер своими словами и нажми «Сохранить»', '', 2600); return; }
  const p = presetOf(key); if (!p) return;
  if (await save({ character_preset: key, character: '' }, true)) { buildAI(); toast(`Характер: ${p.label}`, '', 1600); }
}

const RING_C = 2 * Math.PI * 92;
function buildFocus() {
  const s = S.settings;
  $('#p-focus').innerHTML = head('Фокус', 'Скажи «я делаю домашку 40 минут» — или запусти здесь.') +
    `<div class="card sect"><div class="focus-top">
      <div class="ring"><svg width="210" height="210"><circle class="track" cx="105" cy="105" r="92" fill="none" stroke-width="8"/>
        <circle id="f-bar" class="bar" cx="105" cy="105" r="92" fill="none" stroke-width="8" stroke-linecap="round" stroke-dasharray="${RING_C}" stroke-dashoffset="${RING_C}"/></svg>
        <div class="mid"><div class="time" id="f-time">${fmt(s.focus_minutes * 60)}</div><div class="ph2" id="f-phase">Нет сессии</div></div></div>
      <div class="focus-form">
        <div class="field"><label>Над чем работаешь?</label><input id="f-task" class="inp" placeholder="домашка по физике"></div>
        <div class="grid3"><div class="field"><label>Фокус, мин</label><input id="f-min" class="inp" type="number" min="1" max="240" value="${s.focus_minutes}"></div>
          <div class="field"><label>Перерыв, мин</label><input id="f-brk" class="inp" type="number" min="1" max="60" value="${s.break_minutes}"></div>
          <div class="field"><label>Раундов</label><input id="f-rnd" class="inp" type="number" min="1" max="8" value="${s.rounds}"></div></div>
        <div class="btns" id="f-btns"></div>
        <div class="guard">${icon('shield', 14)}<span id="f-guard"></span></div>
      </div></div></div>
    <div class="card sect"><h3>${icon('chart', 15)}Статистика</h3><div class="metrics" id="f-metrics"></div><div class="bars" id="f-bars"></div></div>
    <div class="card sect"><h3>${icon('eyeoff', 15)}Отвлечения</h3>
      <div class="chips" id="f-dl">${(s.distractions || []).map((d, i) => `<span class="tag">${esc(d)}<button data-rm="${i}">${icon('x', 12)}</button></span>`).join('')}</div>
      <div class="row" style="margin-top:10px;border:0"><input id="f-dadd" class="inp" style="max-width:320px" placeholder="слово из заголовка окна или proc:имя.exe"><button id="f-dbtn" class="btn">${icon('plus', 14)}Добавить</button></div>
      <div class="note">Если в заголовке активного окна есть одно из этих слов во время фокуса — ${esc(NM())} мягко вернёт тебя к задаче. <b>proc:discord.exe</b> — по имени программы.</div></div>`;
  $('#f-dbtn').onclick = addDistraction;
  $('#f-dadd').onkeydown = (e) => { if (e.key === 'Enter') addDistraction(); };
  $$('#f-dl [data-rm]').forEach((b) => b.onclick = async () => { const l = S.settings.distractions.filter((_, i) => i !== +b.dataset.rm); await save({ distractions: l }, true); buildFocus(); });
  renderFocusLive(); renderStats();
}
async function addDistraction() {
  const v = $('#f-dadd').value.trim().toLowerCase(); if (!v) return;
  if (!S.settings.distractions.includes(v)) await save({ distractions: [...S.settings.distractions, v] }, true);
  buildFocus();
}
function renderFocusLive() {
  const f = S.focus || {};
  renderGreeting();
  if (!$('#f-time')) return;
  $('#f-time').textContent = f.active ? fmt(f.remaining) : fmt((+($('#f-min') && $('#f-min').value) || S.settings.focus_minutes) * 60);
  $('#f-phase').textContent = f.active ? `${f.phase_label}${f.rounds > 1 ? ` · ${f.round}/${f.rounds}` : ''}${f.running ? '' : ' · пауза'}` : 'Нет сессии';
  const prog = f.active && f.total ? 1 - f.remaining / f.total : 0;
  $('#f-bar').style.strokeDashoffset = String(RING_C * (1 - prog));
  $('#f-guard').textContent = f.guard || '';
  const t = $('#f-task'); if (f.active && document.activeElement !== t && !t.value) t.value = f.task || '';
  const btns = $('#f-btns');
  const want = f.active ? (f.running ? 'run' : 'pause') : 'idle';
  if (btns.dataset.mode !== want) {
    btns.dataset.mode = want;
    btns.innerHTML = f.active ? `<button class="btn" id="f-pause">${icon(f.running ? 'pause' : 'play', 14)}${f.running ? 'Пауза' : 'Продолжить'}</button><button class="btn" id="f-skip">${icon('skip', 14)}Пропустить</button><button class="btn ghost" id="f-stop">${icon('stop', 14)}Стоп</button>`
                              : `<button class="btn primary" id="f-start">${icon('play', 14)}Начать фокус</button>`;
    if (f.active) {
      $('#f-pause').onclick = async () => { S.focus = await API.focus_pause(); renderFocusLive(); };
      $('#f-skip').onclick = async () => { S.focus = await API.focus_skip(); renderFocusLive(); };
      $('#f-stop').onclick = async () => { S.focus = await API.focus_stop(); renderFocusLive(); refreshStats(); };
    } else {
      $('#f-start').onclick = async () => {
        const r = await API.focus_start($('#f-task').value, +$('#f-min').value, +$('#f-rnd').value, +$('#f-brk').value);
        S.focus = r.focus; renderFocusLive(); toast(`Фокус до ${r.first_block_ends_at}. Я присмотрю.`, '', 2500);
      };
    }
  }
}
async function refreshStats() { try { S.stats = await API.stats(); renderStats(); } catch (e) { } }
function renderStats() {
  const st = S.stats || {}; if (!$('#f-metrics')) return;
  $('#f-metrics').innerHTML = [['Сегодня', st.today], ['Неделя', st.week], ['Серия', st.streak], ['Отвлечений сегодня', st.distractions]]
    .map(([k, v]) => `<div class="metric"><div class="k">${k}</div><div class="v">${esc(v ?? '—')}</div></div>`).join('');
  const days = st.days || []; const max = Math.max(30, ...days.map((d) => d.min));
  $('#f-bars').innerHTML = days.map((d) => `<div class="b" title="${d.date}: ${d.min} мин"><em>${d.min ? d.min : ''}</em><i style="height:${Math.round((d.min / max) * 82)}%"></i><span>${d.label}</span></div>`).join('');
}

function buildSettings() {
  const s = S.settings;
  $('#p-settings').innerHTML = head('Настройки', `${esc(NM())} ${esc(S.version)}`) +
    `<div class="card sect" id="s-vers"><h3>${icon('download', 15)}Версии<span class="grow"></span><button id="s-vref" class="icon-btn" title="Обновить список">${icon('refresh', 14)}</button></h3>
      <div class="row"><div class="grow"><div class="t">Установлена <b>v${esc(S.version)}</b></div><div class="s">Можно обновиться или откатиться на любую версию. Настройки, ключ и история сохраняются.</div></div>
        <button id="s-repo" class="btn ghost sm">${icon('github', 14)}GitHub</button></div>
      <div id="s-vlist" class="vlist"><div class="vempty">Загружаю список версий…</div></div></div>
    <div class="card sect" id="s-phone"><h3>${icon('chat', 15)}Телефон</h3>
      <div class="row"><div class="grow"><div class="t">Пульт в той же сети</div><div class="s" id="s-phone-url">Запускается вместе с Джарвисом. Телефон пишет в чат, ответ идёт с этого компьютера.</div></div></div>
      <div class="row"><div class="grow"><div class="t">PIN</div><div class="s" id="s-phone-pin">······</div></div><button id="s-phone-new" class="btn sm">Новый PIN</button></div></div>
    <div class="card sect"><h3>${icon('app', 15)}Окно и запуск</h3>
      <div class="row"><div class="grow"><div class="t">Запускать вместе с Windows</div><div class="s">Стартует свёрнутым в трей</div></div>${tgl('s-auto', S.autostart)}</div>
      <div class="row"><div class="grow"><div class="t">Поверх всех окон</div></div>${tgl('s-top', s.always_on_top)}</div>
      <div class="row"><div class="grow"><div class="t">Крестик сворачивает в трей</div><div class="s">${esc(NM())} продолжает слушать горячую клавишу и следить за фокусом</div></div>${tgl('s-tray', s.close_to_tray)}</div></div>
    <div class="card sect"><h3>${icon('folder', 15)}Данные</h3>
      <div class="row"><div class="grow"><div class="t">Настройки, ключи, статистика и журнал</div><div class="s">${esc(S.dataDir || '%LOCALAPPDATA%\\Jarvis')}</div></div><button id="s-data" class="btn">${icon('folder', 14)}Открыть папку</button></div></div>
    <div class="card sect"><div class="soon"><div class="big">${icon('camera', 22)}</div><div class="grow"><div class="t" style="font-weight:700">Камера<span class="badge">скоро</span></div>
      <div class="s">${esc(NM())} сможет замечать, что ты взял телефон или ушёл от компьютера.</div></div></div></div>`;
  $('#s-auto').onclick = async () => { const on = !S.autostart; if (await save({ autostart: on }, true)) { S.autostart = on; buildSettings(); } };
  $('#s-top').onclick = async () => { await save({ always_on_top: !S.settings.always_on_top }, true); buildSettings(); };
  $('#s-tray').onclick = async () => { await save({ close_to_tray: !S.settings.close_to_tray }, true); buildSettings(); };
  $('#s-data').onclick = () => API.open_data_folder();
  $('#s-phone-new').onclick = async () => { const r = await API.phone_rotate(); paintPhone(r); toast('Телефон нужно подключить заново', '', 2200); };
  API.phone_info().then(paintPhone).catch(() => {});
  $('#s-vref').onclick = () => loadVersions(true);
  $('#s-repo').onclick = () => API.open_link(S.repo || 'https://github.com/Sinohara1/jarvis/releases');
  if (S.versions) renderVersions(); else loadVersions(false);
}

function paintPhone(r) {
  if (!r || !$('#s-phone-url')) return;
  $('#s-phone-url').textContent = r.url ? `Открой на телефоне ${r.url} — та же Wi‑Fi, что у компьютера.` : (r.error || 'Пульт не запущен');
  $('#s-phone-pin').textContent = r.pin || '······';
}

// ───────────────────────── versions (GitHub Releases) ─────────────────────────
async function loadVersions(force) {
  const box = $('#s-vlist'); if (box && !S.versions) box.innerHTML = '<div class="vempty">Загружаю список версий…</div>';
  if (force && $('#s-vref')) $('#s-vref').classList.add('spin');
  let r;
  try { r = await API.list_versions(!!force); } catch (e) { r = { ok: false, error: String(e) }; }
  if ($('#s-vref')) $('#s-vref').classList.remove('spin');
  S.versions = r; if (r.repo) S.repo = r.repo + '/releases';
  renderVersions();
}
function renderVersions() {
  const box = $('#s-vlist'); if (!box) return;
  const r = S.versions || {};
  if (!r.ok) { box.innerHTML = `<div class="vempty">${esc(r.error || 'Не удалось получить список версий.')}</div>`; return; }
  if (!r.releases.length) { box.innerHTML = '<div class="vempty">На GitHub пока нет опубликованных версий.</div>'; return; }
  const latest = (r.releases.find((x) => !x.prerelease) || {}).tag;
  box.innerHTML = r.releases.map((v) => {
    const cur = v.status === 'current';
    const lbl = v.status === 'newer' ? 'Обновить' : 'Откатить';
    const ic = v.status === 'newer' ? 'download' : 'undo';
    const busy = S.installing === v.tag;
    const btn = cur ? `<span class="vcur">${icon('check', 13)}установлена</span>`
      : `<button class="btn sm${v.status === 'newer' ? ' primary' : ''}" data-inst="${esc(v.tag)}" ${S.installing ? 'disabled' : ''}>${icon(ic, 13)}${lbl}</button>`;
    return `<div class="ver${cur ? ' cur' : ''}" data-tag="${esc(v.tag)}">
      <div class="vhead"><span class="vtag">${esc(v.tag)}</span>${v.tag === latest ? '<span class="badge">последняя</span>' : ''}${v.prerelease ? '<span class="badge">тест</span>' : ''}
        <span class="vdate">${esc(v.date)}${v.size ? ' · ' + (v.size / 1048576).toFixed(1) + ' МБ' : ''}</span><span class="grow"></span>${btn}</div>
      ${v.notes ? `<div class="vnotes">${esc(v.notes)}</div>` : ''}
      <div class="vprog${busy ? ' on' : ''}"><i></i><span></span></div></div>`;
  }).join('');
  box.querySelectorAll('[data-inst]').forEach((b) => { b.onclick = () => confirmInstall(b.dataset.inst); });
}
function confirmInstall(tag) {
  const r = S.versions || {}; const v = (r.releases || []).find((x) => x.tag === tag); if (!v) return;
  const newer = v.status === 'newer';
  openModal(newer ? `Обновить до ${v.tag}?` : `Откатить на ${v.tag}?`,
    `Сейчас установлена v${esc(S.version)}. ${esc(NM())} скачает ${esc(v.tag)} с GitHub${v.size ? ` (${(v.size / 1048576).toFixed(1)} МБ)` : ''}, закроется и запустится уже в новой версии.<br><br>Настройки, API-ключ, история чата и статистика сохранятся. Вернуться обратно можно здесь же в любой момент.`,
    newer ? 'Обновить' : 'Откатить', () => doInstall(tag));
}
async function doInstall(tag) {
  S.installing = tag; renderVersions();
  const r = await API.install_version(tag);
  if (!r.ok) { S.installing = null; renderVersions(); toast(r.error || 'Ошибка', 'err', 6000); return; }
  renderUpdProgress({ tag, done: 0, total: 0 });
}
function renderUpdProgress(d) {
  const row = document.querySelector(`.ver[data-tag="${CSS.escape(d.tag)}"] .vprog`); if (!row) return;
  row.classList.add('on');
  const pct = d.total ? Math.min(100, Math.round((d.done / d.total) * 100)) : 0;
  row.querySelector('i').style.width = pct + '%';
  row.querySelector('span').textContent = d.total ? `Скачиваю… ${pct}% · ${(d.done / 1048576).toFixed(1)} из ${(d.total / 1048576).toFixed(1)} МБ` : 'Подключаюсь к GitHub…';
}
function openVersions() { selectTab('settings'); setTimeout(() => { const el = $('#s-vers'); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' }); loadVersions(true); }, 60); }

// ───────────────────────── modal ─────────────────────────
function openModal(title, html, okText, onOk) {
  closeModal();
  const m = document.createElement('div'); m.id = 'modal'; m.className = 'modal';
  m.innerHTML = `<div class="mbox"><div class="mtitle">${esc(title)}</div><div class="mtext">${html}</div>
    <div class="mbtns"><button class="btn ghost" data-m="no">Отмена</button><button class="btn primary" data-m="ok">${esc(okText)}</button></div></div>`;
  m.onclick = (e) => { const b = e.target.closest('[data-m]'); if (e.target === m || (b && b.dataset.m === 'no')) closeModal(); else if (b && b.dataset.m === 'ok') { closeModal(); onOk(); } };
  document.body.appendChild(m);
  requestAnimationFrame(() => m.classList.add('show'));
}
function closeModal() { const m = $('#modal'); if (m) m.remove(); }

// ───────────────────────── toasts ─────────────────────────
function toast(text, cls = '', ms = 3000, actions = []) {
  const el = document.createElement('div');
  el.className = 'toast ' + cls;
  el.innerHTML = `<span>${esc(text)}</span>` + actions.map((a, i) => `<button class="btn sm" data-a="${i}">${esc(a[0])}</button>`).join('') + (ms === 0 ? `<button class="icon-btn" data-x style="width:22px;height:22px">${icon('x', 12)}</button>` : '');
  el.onclick = (e) => { const b = e.target.closest('[data-a]'); if (b) { actions[+b.dataset.a][1](); el.remove(); } if (e.target.closest('[data-x]')) el.remove(); };
  $('#toasts').appendChild(el);
  while ($('#toasts').children.length > 4) $('#toasts').firstChild.remove();
  if (ms) setTimeout(() => el.remove(), ms);
}

// ───────────────────────── window chrome ─────────────────────────
function setMax(m) { document.body.classList.toggle('maximized', !!m); $('#winctl [data-win=max]').innerHTML = icon(m ? 'restore' : 'max', 12); }

function bindStatic() {
  $('#tabs').onclick = (e) => { const t = e.target.closest('.tab'); if (t) { selectTab(t.dataset.tab); if (t.dataset.tab === 'home') enterChat(S.chatMode); } };
  $('#winctl').onclick = async (e) => {
    const b = e.target.closest('[data-win]'); if (!b) return;
    if (b.dataset.win === 'min') API.win_minimize();
    else if (b.dataset.win === 'max') setMax(await API.win_toggle_max());
    else API.win_close();
  };
  // drag: empty top bar area
  $('#top').addEventListener('mousedown', (e) => { if (e.button === 0 && e.target === $('#top')) API && API.win_begin_move(); });
  $('#top').addEventListener('dblclick', async (e) => { if (e.target === $('#top')) setMax(await API.win_toggle_max()); });
  $$('.rz').forEach((g) => g.addEventListener('mousedown', (e) => { if (e.button === 0) { e.preventDefault(); API && API.win_begin_resize(g.dataset.edge); } }));
  $('#burger').onclick = (e) => popBurger(e.currentTarget);
  $('#grid-btn').onclick = (e) => popGrid(e.currentTarget);
  $('#rail-home').onclick = () => { selectTab('home'); enterChat(false); $('#rail-home').classList.add('active'); };
  $('#rail-new').onclick = () => newChat();
  $('#input').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } if (e.key === 'Escape') { $('#input').value = ''; } });
  $('#send').onclick = () => send();
  $('#mic').onclick = () => { if (S.state === 'speaking') API.stop_speaking(); else API.listen(); };
  $('#slash').onclick = (e) => popCommands(e.currentTarget);
  $('#plus').onclick = (e) => popPlus(e.currentTarget);
  $('#clip').onclick = () => { S.attach = !S.attach; $('#clip').classList.toggle('on', S.attach); toast(S.attach ? 'К следующему сообщению приложу снимок экрана' : 'Снимок экрана не прикладывается', '', 1800); $('#input').focus(); };
  $('#live-wrap').onclick = (e) => { e.preventDefault(); toggleLive(); };
  $('#confirm-wrap').onclick = async (e) => { e.preventDefault(); await save({ confirm_actions: !S.settings.confirm_actions }, true); toast(S.settings.confirm_actions ? 'Буду спрашивать перед действиями' : 'Действую сразу', '', 1600); buildAI(); };
  $('#wake-btn').onclick = () => toggleWake();
  $('#follow-chip').onclick = () => followStop();
  $('#voice-btn').onclick = async () => { const on = !S.settings.speak_replies; if (!on) API.stop_speaking(); await save({ speak_replies: on }, true); toast(on ? 'Голос включён' : 'Отвечаю только текстом', '', 1500); };
  $('#model-btn').onclick = (e) => popModel(e.currentTarget);
  $('#watch-btn').onclick = (e) => popWatch(e.currentTarget);
  $('#quick').onclick = (e) => {
    const b = e.target.closest('[data-q]'); if (!b) return;
    if (b.dataset.q === 'last') { if (S.chat.length) enterChat(true); else toast('Чат пока пуст — спроси что-нибудь', '', 2000); }
    else if (b.dataset.q === 'cmd') { selectTab('commands'); setTimeout(() => $('#cc-name') && $('#cc-name').focus(), 250); }
    else send('Что ты умеешь?');
  };
  document.addEventListener('mousedown', (e) => { if (!e.target.closest('#pop') && popAnchor && !popAnchor.contains(e.target)) closePop(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closePop();
    if (S.tab === 'home' && document.activeElement === document.body && e.key.length === 1 && !e.ctrlKey && !e.altKey) $('#input').focus();
  });
  document.addEventListener('contextmenu', (e) => { if (!e.target.closest('input, textarea, .bubble')) e.preventDefault(); });
  setInterval(renderGreeting, 60000);
}

boot();
connect();
