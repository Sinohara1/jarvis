'use strict';
window.addEventListener('error', (e) => { try { window.pywebview && window.pywebview.api && window.pywebview.api.log_js(`${e.message} @${e.filename}:${e.lineno}`); } catch (_) {} });
// Джарвис UI — talks to Python via window.pywebview.api (see jarvis_app/webui.py).
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let API = null;
const S = { settings: {}, providers: {}, voices: [], chat: [], focus: {}, stats: {}, state: 'idle', tab: 'home',
            chatMode: false, attach: false, wake: { enabled: false }, echo: [], hotkey: 'Ctrl+Alt+J' };

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
  ['chart', 'Как мой фокус сегодня?', 'Сколько я сегодня был в фокусе?'],
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
  Object.assign(S, { settings: d.settings, providers: d.providers, voices: d.voices, abilities: d.abilities, tools: d.tools,
                     chat: d.chat || [], focus: d.focus, stats: d.stats, version: d.version, hotkey: d.hotkey,
                     autostart: d.autostart, wake: d.wake, hasKey: d.has_key, dataDir: d.data_dir });
  setState(d.state || 'idle');
  setMax(d.maximized);
  renderComposer();
  renderGreeting();
  buildPages();
  if (!d.has_key) toast('Нет API-ключа — добавь его во вкладке «Модель».', 'err', 7000);
}

// ───────────────────────── events from Python ─────────────────────────
window.J = {
  autotest(cmd) {   // set only via JARVIS_AUTOTEST for automated screenshots
    if (cmd.startsWith('tab:')) { selectTab(cmd.slice(4)); return; }
    if (cmd.startsWith('click:')) {   // click:sel1|sel2|… — clicks in order, waiting up to 15 s for each element
      const sels = cmd.slice(6).split('|');
      (async () => { for (const sel of sels) { let el = null; for (let i = 0; i < 150 && !el; i++) { el = document.querySelector(sel); if (!el) await new Promise((r) => setTimeout(r, 100)); }
        API.log_js('autotest click ' + sel + (el ? '' : ' — not found')); if (!el) return; el.scrollIntoView({ block: 'center' }); el.click(); await new Promise((r) => setTimeout(r, 800)); } })();
      return;
    }
    if (cmd === 'chat') { selectTab('home'); enterChat(true); return; }
    $('#input').value = cmd; send();
  },
  onEvents(list) { for (const { e, d } of list) { try { onEvent(e, d || {}); } catch (err) { console.error(err); } } },
};
function onEvent(e, d) {
  switch (e) {
    case 'state': setState(d.state, d.detail); break;
    case 'level': document.documentElement.style.setProperty('--lvl', Math.min(1, (d.level || 0) * 1.6).toFixed(3)); break;
    case 'chat': addChat(d); break;
    case 'tool': addMsg({ role: 'tool', text: d.label }); break;
    case 'focus': case 'tick': if (d.focus) { S.focus = d.focus; renderFocusLive(); } break;
    case 'notify': if (S.tab !== 'home' || !S.chatMode) toast(d.text); break;
    case 'error': toast(d.text, 'err', 6000); break;
    case 'wake': S.wake = { enabled: !!d.enabled }; renderComposer(); if (d.error) toast('Слово-активатор недоступно: ' + d.error, 'err', 6000); break;
    case 'visible': window.BG && BG.setPaused(!d.visible); break;
    case 'window': setMax(d.maximized); break;
    case 'update': toast(`Доступна новая версия ${d.tag}`, '', 0, [['Посмотреть', openVersions]]); break;
    case 'upd_progress': renderUpdProgress(d); break;
    case 'upd_ready': toast(`Устанавливаю ${d.tag} — Джарвис перезапустится…`, '', 0); closeModal(); break;
    case 'upd_error': S.installing = null; toast('Не удалось установить: ' + d.error, 'err', 8000); renderVersions(); break;
    case 'reminders': break;
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
  el.className = `msg ${role}${m.kind === 'nudge' ? ' nudge' : ''}`;
  if (role === 'jarvis') el.innerHTML = `<div class="av">${icon(m.kind === 'nudge' ? 'target' : 'spark', 14)}</div><div class="bubble">${esc(m.text)}</div>`;
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
function renderTyping() {
  const box = $('#msgs');
  const t = $('#msgs .typing');
  if (S.state === 'thinking' && S.chatMode) {
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
  return `${p}/${m}`;
}
function watchLabel() {
  const v = S.settings.screen_check_sec;
  const w = WATCH.find((x) => x[0] === v);
  return w ? w[1] : `Каждые ${v} с`;
}
function renderComposer() {
  const s = S.settings;
  $('#confirm-tgl').classList.toggle('on', !!s.confirm_actions);
  const wb = $('#wake-btn');
  wb.innerHTML = icon('mic', 15) + `<span>${S.wake.enabled ? 'Слушаю «Hey Jarvis»' : 'Запустить'}</span>`;
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
  const name = S.settings.user_name || 'Вова';
  const g = h >= 1 && h < 5 ? `Не спится, ${name}?` : `Пора работать, ${name}?`;
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
  let h = '';
  for (const [p, info] of Object.entries(S.providers)) {
    h += `<div class="ph-h">${esc(info.label)}</div>`;
    const cur = S.settings.models[p].chat;
    const list = Array.from(new Set([cur, ...info.chat_models])).slice(0, 6);
    for (const m of list) {
      const sel = S.settings.provider === p && cur === m;
      h += `<button class="pi ${sel ? 'sel' : ''}" data-v="${esc(p + '|' + m)}"><span>${esc(m)}</span>${icon('check', 14, 'chk')}</button>`;
    }
  }
  h += '<div class="sep"></div>' + pi('cfg', 'settings', 'Ключи и модели…');
  openPop(anchor, h, S.chatMode ? 'above' : 'below', async (v) => {
    if (v === 'cfg') { selectTab('model'); return; }
    const [p, m] = v.split('|');
    await save({ provider: p, models: { [p]: { chat: m } } }, true);
    toast(`Модель: ${p}/${m}`, '', 1600);
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
            pi('data', 'folder', 'Папка данных') + '<div class="sep"></div>' + pi('quit', 'power', 'Выйти из Джарвиса');
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
            pi('cmds', 'terminal', 'Что умеет Джарвис') + pi('cam', 'camera', 'Камера', 'скоро', 'dim') + pi('help', 'keyboard', 'Горячие клавиши', S.hotkey);
  openPop(anchor, h, 'below', (v) => {
    if (v === 'focus') selectTab('focus');
    else if (v === 'cmds') selectTab('commands');
    else if (v === 'cam') toast('Камера появится в следующих версиях — Джарвис сможет замечать, что ты отвлёкся на телефон.', '', 4000);
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
  $('#p-commands').innerHTML = head('Команды', 'Скажи или напиши своими словами — Джарвис сам выберет действие. Нажми на карточку, чтобы попробовать.') +
    `<div class="grid2" style="margin-bottom:14px">${ab.map((a, i) => `<div class="card abil" data-try="${i}"><div class="top"><div class="ico">${icon(a.icon, 16)}</div><div class="ttl">${esc(a.title)}</div></div>
      <div class="dsc">${esc(a.desc)}</div><div class="ex">«${esc(a.example)}»</div></div>`).join('')}</div>
    <div class="card sect"><h3>${icon('terminal', 15)}Свои команды<span class="badge">в меню «/»</span></h3>
      <div id="cc-list">${cc.length ? cc.map((c, i) => `<div class="row cmd-row"><div class="grow"><div class="name">/${esc(c.name)}</div><div class="prompt">${esc(c.prompt)}</div></div>
        <button class="btn sm ghost" data-run="${i}">${icon('play', 13)}Запустить</button><button class="icon-btn" data-del="${i}" title="Удалить">${icon('trash', 15)}</button></div>`).join('') : '<div class="note" style="margin:0 0 10px">Пока пусто. Например: «утро» → «Открой почту, календарь и скажи, сколько времени».</div>'}</div>
      <div class="grid2" style="grid-template-columns: 1fr 2fr auto; align-items:end; margin-top:10px">
        <div class="field"><label>Название</label><input id="cc-name" class="inp" placeholder="утро"></div>
        <div class="field"><label>Что сказать Джарвису</label><input id="cc-prompt" class="inp" placeholder="Открой почту и скажи, сколько времени"></div>
        <button id="cc-add" class="btn primary">${icon('plus', 15)}Добавить</button>
      </div></div>
    <div class="card sect"><h3>${icon('shield', 15)}Безопасность</h3><div class="note" style="margin:0">Джарвис не удаляет файлы, не закрывает программы и не отправляет сообщения. Скрипты и установщики он не запускает. Включи «Подтверждать опасные действия», чтобы он спрашивал перед открытием программ и сайтов.</div></div>`;
  $$('#p-commands [data-try]').forEach((el) => el.onclick = () => { const a = ab[+el.dataset.try]; selectTab('home'); $('#input').value = a.example; $('#input').focus(); });
  $$('#p-commands [data-run]').forEach((el) => el.onclick = () => send(cc[+el.dataset.run].prompt));
  $$('#p-commands [data-del]').forEach((el) => el.onclick = async () => { const list = cc.filter((_, i) => i !== +el.dataset.del); await save({ custom_commands: list }); buildCommands(); });
  $('#cc-add').onclick = async () => {
    const name = $('#cc-name').value.trim().replace(/^\//, ''), prompt = $('#cc-prompt').value.trim();
    if (!name || !prompt) { toast('Заполни название и текст команды', 'err'); return; }
    await save({ custom_commands: [...cc, { name, prompt }] }); buildCommands();
  };
}

function buildModel() {
  const s = S.settings, p = s.provider, info = S.providers[p];
  const m = s.models[p];
  $('#p-model').innerHTML = head('Модель', 'Какой ИИ думает за Джарвиса. Можно переключиться в любой момент.') +
    `<div class="card sect"><h3>${icon('box', 15)}Провайдер</h3>
      <div class="seg" id="prov-seg">${Object.entries(S.providers).map(([k, v]) => `<button data-p="${k}" class="${k === p ? 'on' : ''}">${esc(v.label)}</button>`).join('')}</div>
      <div class="grid2" style="margin-top:16px">
        <div class="field"><label>Модель для разговора</label><input id="m-chat" class="inp" list="dl-chat" value="${esc(m.chat)}"><datalist id="dl-chat">${info.chat_models.map((x) => `<option value="${esc(x)}">`).join('')}</datalist></div>
        <div class="field"><label>Лёгкая модель (проверки экрана)</label><input id="m-lite" class="inp" list="dl-lite" value="${esc(m.lite)}"><datalist id="dl-lite">${info.lite_models.map((x) => `<option value="${esc(x)}">`).join('')}</datalist></div>
      </div>
      <div class="field" style="margin-top:12px"><label>API-ключ ${esc(info.label)} · <span class="link" id="key-link">получить ключ</span></label>
        <div class="inp-wrap"><input id="m-key" class="inp" type="password" placeholder="вставь ключ сюда" value="${esc(s.api_keys[p] || '')}"><button class="eye" id="key-eye">${icon('eye', 15)}</button></div></div>
      <details style="margin-top:10px"><summary class="note" style="cursor:pointer;margin:0">Дополнительно: адрес API</summary>
        <div class="field" style="margin-top:8px"><input id="m-base" class="inp" value="${esc(s.base_urls[p] || info.base_url)}"></div></details>
      <div class="row" style="margin-top:12px;border:0"><button id="m-save" class="btn primary">${icon('check', 15)}Сохранить</button><button id="m-test" class="btn">${icon('zap', 15)}Проверить</button><span id="m-res" class="result"></span></div>
      <div class="note">Бесплатный Gemini: около 10 запросов в минуту и ограничение в день. При лимите Джарвис скажет об этом, временно перейдёт на лёгкую модель и реже проверяет экран. <b>gemini-2.5-flash</b> недоступна для новых ключей — по умолчанию <b>gemini-3.5-flash</b>.</div></div>`;
  $$('#prov-seg button').forEach((b) => b.onclick = async () => { await save({ provider: b.dataset.p }, true); buildModel(); renderComposer(); toast('Провайдер: ' + S.providers[b.dataset.p].label, '', 1500); });
  $('#key-link').onclick = () => API.open_link(info.key_url);
  $('#key-eye').onclick = () => { const k = $('#m-key'); k.type = k.type === 'password' ? 'text' : 'password'; };
  const collect = () => ({ models: { [p]: { chat: $('#m-chat').value.trim(), lite: $('#m-lite').value.trim() } }, api_keys: { [p]: $('#m-key').value.trim() }, base_urls: { [p]: $('#m-base').value.trim() } });
  $('#m-save').onclick = async () => { await save(collect()); };
  $('#m-test').onclick = async () => { $('#m-res').textContent = 'Проверяю…'; const r = await API.test_ai(p, collect()); $('#m-res').textContent = r; };
}

function buildVoice() {
  const s = S.settings;
  $('#p-voice').innerHTML = head('Голос', 'Как Джарвис слушает и говорит.') +
    `<div class="card sect"><h3>${icon('vol', 15)}Голос Джарвиса</h3>
      <div class="grid2"><div class="field"><label>Голос (Microsoft Edge, бесплатно)</label><select id="v-voice" class="inp">${S.voices.map((v) => `<option ${v === s.voice ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></div>
      <div class="field"><label>&nbsp;</label><button id="v-test" class="btn">${icon('play', 14)}Проверить голос</button></div></div>
      <div class="row" style="margin-top:8px"><div class="grow"><div class="t">Скорость речи</div></div><input id="v-rate" class="slider" style="max-width:300px" type="range" min="-50" max="50" value="${s.tts_rate}"><span class="val" id="v-rate-v">${s.tts_rate > 0 ? '+' : ''}${s.tts_rate}%</span></div>
      <div class="row"><div class="grow"><div class="t">Громкость</div></div><input id="v-vol" class="slider" style="max-width:300px" type="range" min="0" max="100" value="${s.tts_volume}"><span class="val" id="v-vol-v">${s.tts_volume}%</span></div>
      <div class="row"><div class="grow"><div class="t">Озвучивать ответы</div><div class="s">Если выключить — ответы только текстом</div></div>${tgl('v-speak', s.speak_replies)}</div></div>
    <div class="card sect"><h3>${icon('keyboard', 15)}Горячая клавиша</h3>
      <div class="row"><div class="grow"><div class="t" id="v-hk">${esc(S.hotkey)}</div><div class="s">Удерживай — говоришь. Короткое нажатие — слушаю до паузы. Повторное — стоп.</div></div><button id="v-hk-btn" class="btn">Изменить</button></div></div>
    <div class="card sect"><h3>${icon('mic', 15)}Слово-активатор</h3>
      <div class="row"><div class="grow"><div class="t">Слушать «Hey Jarvis»</div><div class="s">Работает офлайн, без отправки звука в интернет. Говори по-английски: «Hey Jarvis».</div></div>${tgl('v-wake', S.wake.enabled || s.wake_word)}</div>
      <div class="row"><div class="grow"><div class="t">Чувствительность</div><div class="s">Меньше — реагирует охотнее (и чаще ошибается)</div></div><input id="v-thr" class="slider" style="max-width:240px" type="range" min="10" max="95" value="${Math.round(s.wake_threshold * 100)}"><span class="val" id="v-thr-v">${s.wake_threshold.toFixed(2)}</span></div></div>`;
  $('#v-voice').onchange = () => save({ voice: $('#v-voice').value });
  $('#v-test').onclick = () => API.test_voice($('#v-voice').value, +$('#v-rate').value, +$('#v-vol').value);
  $('#v-rate').oninput = () => { const v = +$('#v-rate').value; $('#v-rate-v').textContent = (v > 0 ? '+' : '') + v + '%'; };
  $('#v-rate').onchange = () => save({ tts_rate: +$('#v-rate').value }, true);
  $('#v-vol').oninput = () => { $('#v-vol-v').textContent = $('#v-vol').value + '%'; };
  $('#v-vol').onchange = () => save({ tts_volume: +$('#v-vol').value }, true);
  $('#v-thr').oninput = () => { $('#v-thr-v').textContent = (+$('#v-thr').value / 100).toFixed(2); };
  $('#v-thr').onchange = () => save({ wake_threshold: +$('#v-thr').value / 100 }, true);
  $('#v-speak').onclick = async () => { await save({ speak_replies: !S.settings.speak_replies }, true); buildVoice(); };
  $('#v-wake').onclick = () => toggleWake().then(buildVoice);
  $('#v-hk-btn').onclick = captureHotkey;
}
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
async function toggleWake() {
  const on = !S.wake.enabled;
  const r = await API.set_wake(on);
  if (r && r.ok) { S.settings = r.settings; S.wake = r.wake; renderComposer(); toast(S.wake.enabled ? 'Скажи «Hey Jarvis», чтобы позвать меня' : 'Слово-активатор выключено', '', 2200); }
}

function buildAI() {
  const s = S.settings;
  const opts = [[0, 'Выключено'], [30, 'Каждые 30 с'], [60, 'Каждую минуту'], [90, 'Каждые 90 с'], [180, 'Каждые 3 мин'], [300, 'Каждые 5 мин']];
  if (!opts.some((o) => o[0] === s.screen_check_sec)) opts.push([s.screen_check_sec, `Каждые ${s.screen_check_sec} с`]);
  $('#p-ai').innerHTML = head('ИИ', 'Характер Джарвиса и то, как он следит за фокусом.') +
    `<div class="card sect"><h3>${icon('user', 15)}Характер</h3>
      <div class="grid2"><div class="field"><label>Как к тебе обращаться</label><input id="a-name" class="inp" value="${esc(s.user_name)}"></div><div></div></div>
      <div class="field" style="margin-top:12px"><label>Дополнительные инструкции (что важно знать Джарвису о тебе, стиль общения)</label>
        <textarea id="a-persona" class="inp" placeholder="Например: я учусь в 10 классе, готовлюсь к ЕГЭ. Будь построже, когда я отвлекаюсь. Шути иногда.">${esc(s.persona)}</textarea></div>
      <div class="row" style="margin-top:6px"><div class="grow"><div class="t">Подтверждать опасные действия</div><div class="s">Спрашивать перед открытием программ, файлов и сайтов</div></div>${tgl('a-confirm', s.confirm_actions)}</div>
      <div class="row"><button id="a-save" class="btn primary">${icon('check', 15)}Сохранить</button><button id="a-reset" class="btn ghost">${icon('refresh', 14)}Новый чат — очистить память разговора</button></div></div>
    <div class="card sect"><h3>${icon('shield', 15)}Страж фокуса</h3>
      <div class="grid2">
        <div class="field"><label>Смотреть на экран (ИИ, во время фокуса)</label><select id="a-screen" class="inp">${opts.map(([v, l]) => `<option value="${v}" ${v === s.screen_check_sec ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
        <div class="field"><label>Проверять заголовок окна, с</label><input id="a-title" class="inp" type="number" min="2" max="60" value="${s.title_check_sec}"></div>
        <div class="field"><label>Пауза между напоминаниями, с</label><input id="a-cool" class="inp" type="number" min="15" max="900" value="${s.nudge_cooldown_sec}"></div>
        <div class="field"><label>Сколько можно «подсмотреть» без замечания, с</label><input id="a-grace" class="inp" type="number" min="0" max="120" value="${s.distraction_grace_sec}"></div>
      </div>
      <div class="note">Скриншот уменьшается и уходит только в лёгкую модель вместе с твоей задачей; на диск ничего не сохраняется. Заголовки окон проверяются локально, без ИИ.</div></div>`;
  $('#a-confirm').onclick = async () => { await save({ confirm_actions: !S.settings.confirm_actions }, true); buildAI(); };
  $('#a-save').onclick = async () => { await save({ user_name: $('#a-name').value.trim(), persona: $('#a-persona').value }); renderGreeting(); };
  $('#a-reset').onclick = () => newChat();
  $('#a-screen').onchange = () => save({ screen_check_sec: +$('#a-screen').value });
  $('#a-title').onchange = () => save({ title_check_sec: +$('#a-title').value });
  $('#a-cool').onchange = () => save({ nudge_cooldown_sec: +$('#a-cool').value });
  $('#a-grace').onchange = () => save({ distraction_grace_sec: +$('#a-grace').value });
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
      <div class="note">Если в заголовке активного окна есть одно из этих слов во время фокуса — Джарвис мягко вернёт тебя к задаче. <b>proc:discord.exe</b> — по имени программы.</div></div>`;
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
  $('#p-settings').innerHTML = head('Настройки', `Джарвис ${esc(S.version)}`) +
    `<div class="card sect" id="s-vers"><h3>${icon('download', 15)}Версии<span class="grow"></span><button id="s-vref" class="icon-btn" title="Обновить список">${icon('refresh', 14)}</button></h3>
      <div class="row"><div class="grow"><div class="t">Установлена <b>v${esc(S.version)}</b></div><div class="s">Можно обновиться или откатиться на любую версию. Настройки, ключ и история сохраняются.</div></div>
        <button id="s-repo" class="btn ghost sm">${icon('github', 14)}GitHub</button></div>
      <div id="s-vlist" class="vlist"><div class="vempty">Загружаю список версий…</div></div></div>
    <div class="card sect"><h3>${icon('app', 15)}Окно и запуск</h3>
      <div class="row"><div class="grow"><div class="t">Запускать вместе с Windows</div><div class="s">Стартует свёрнутым в трей</div></div>${tgl('s-auto', S.autostart)}</div>
      <div class="row"><div class="grow"><div class="t">Поверх всех окон</div></div>${tgl('s-top', s.always_on_top)}</div>
      <div class="row"><div class="grow"><div class="t">Крестик сворачивает в трей</div><div class="s">Джарвис продолжает слушать горячую клавишу и следить за фокусом</div></div>${tgl('s-tray', s.close_to_tray)}</div></div>
    <div class="card sect"><h3>${icon('folder', 15)}Данные</h3>
      <div class="row"><div class="grow"><div class="t">Настройки, ключи, статистика и журнал</div><div class="s">${esc(S.dataDir || '%LOCALAPPDATA%\\Jarvis')}</div></div><button id="s-data" class="btn">${icon('folder', 14)}Открыть папку</button></div></div>
    <div class="card sect"><div class="soon"><div class="big">${icon('camera', 22)}</div><div class="grow"><div class="t" style="font-weight:700">Камера<span class="badge">скоро</span></div>
      <div class="s">Джарвис сможет замечать, что ты взял телефон или ушёл от компьютера.</div></div></div></div>`;
  $('#s-auto').onclick = async () => { const on = !S.autostart; if (await save({ autostart: on }, true)) { S.autostart = on; buildSettings(); } };
  $('#s-top').onclick = async () => { await save({ always_on_top: !S.settings.always_on_top }, true); buildSettings(); };
  $('#s-tray').onclick = async () => { await save({ close_to_tray: !S.settings.close_to_tray }, true); buildSettings(); };
  $('#s-data').onclick = () => API.open_data_folder();
  $('#s-vref').onclick = () => loadVersions(true);
  $('#s-repo').onclick = () => API.open_link(S.repo || 'https://github.com/Sinohara1/jarvis-releases/releases');
  if (S.versions) renderVersions(); else loadVersions(false);
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
    `Сейчас установлена v${esc(S.version)}. Джарвис скачает ${esc(v.tag)} с GitHub${v.size ? ` (${(v.size / 1048576).toFixed(1)} МБ)` : ''}, закроется и запустится уже в новой версии.<br><br>Настройки, API-ключ, история чата и статистика сохранятся. Вернуться обратно можно здесь же в любой момент.`,
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
  $('#confirm-wrap').onclick = async (e) => { e.preventDefault(); await save({ confirm_actions: !S.settings.confirm_actions }, true); toast(S.settings.confirm_actions ? 'Буду спрашивать перед действиями' : 'Действую сразу', '', 1600); buildAI(); };
  $('#wake-btn').onclick = () => toggleWake();
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
