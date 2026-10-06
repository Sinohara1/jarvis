'use strict';
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&', '<': '<', '>': '>', '"': '"', "'": '&#39;' }[c]));
const tokenKey = 'jarvis-phone-token';
let token = localStorage.getItem(tokenKey) || '';
let state = { name: 'Джарвис' };
let chat = JSON.parse(localStorage.getItem('jarvis-phone-chat') || '[]');

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({ ok: false, error: 'битый ответ' }));
  if (res.status === 401) { token = ''; localStorage.removeItem(tokenKey); renderPair(data.error); throw new Error('pin'); }
  return data;
}

function renderPair(err) {
  $('#app').innerHTML = `<h1>Джарвис</h1><p class="dim">Телефон говорит с Джарвисом на компьютере. Оба в одной сети. PIN показан в настройках на ПК.</p>
    <div class="card"><div class="dim">PIN с компьютера</div><input id="pin" inputmode="numeric" maxlength="6" placeholder="000000"></div>
    <button class="primary" id="go">Подключить</button>
    ${err ? `<p class="err">${esc(err)}</p>` : ''}`;
  $('#go').onclick = async () => {
    const r = await fetch('/api/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin: $('#pin').value.trim() }) }).then((x) => x.json());
    if (!r.ok) return renderPair(r.error);
    token = r.token; localStorage.setItem(tokenKey, token);
    boot();
  };
}

async function boot() {
  if (!token) return renderPair();
  try { state = await api('/api/state'); } catch (e) { if (String(e.message) !== 'pin') renderPair('ПК не ответил'); return; }
  render();
}

function render() {
  $('#app').innerHTML = `<h1>${esc(state.name || 'Джарвис')}</h1><p class="dim">Подключён к компьютеру. Ответы идут с ПК.</p>
    <div id="view"></div>`;
  renderChat();
}

function renderChat() {
  $('#view').innerHTML = `<div id="log">${chat.map((m) => `<div class="msg ${m.role}">${esc(m.text)}</div>`).join('') || '<p class="dim">Напиши сюда — ответит Джарвис на ПК.</p>'}</div>
    <label class="dim"><input type="checkbox" id="speak"> произнести на компьютере</label>
    <div class="compose"><textarea id="text" placeholder="Спросить…"></textarea><button class="primary" id="send">→</button></div>`;
  const log = $('#log'); if (log) log.scrollTop = log.scrollHeight;
  $('#send').onclick = send;
  $('#text').onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
}

async function send() {
  const text = $('#text').value.trim();
  const speak = $('#speak') && $('#speak').checked;
  if (!text) return;
  $('#text').value = '';
  chat.push({ role: 'user', text });
  localStorage.setItem('jarvis-phone-chat', JSON.stringify(chat.slice(-80)));
  render();
  const r = await api('/api/chat', { text, speak });
  chat.push({ role: 'jarvis', text: r.reply || r.error || '…' });
  localStorage.setItem('jarvis-phone-chat', JSON.stringify(chat.slice(-80)));
  if (r.reply && window.speechSynthesis) speechSynthesis.speak(new SpeechSynthesisUtterance(r.reply));
  render();
}

boot();
