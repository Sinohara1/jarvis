'use strict';
// Память компаньона (вкладка «ИИ» → карточка «Память»). Python: jarvis_app/memory.py, мост: Bridge.memory_*.
const MEM_MAX = [[25, '25'], [50, '50'], [100, '100'], [200, '200'], [300, '300'], [500, '500']];
const MEM_INJECT = [[0, 'не подмешивать — только по запросу'], [3, '3 самых важных'], [5, '5'], [8, '8 (рекомендую)'], [12, '12'], [20, '20']];
const MEM_SRC = { explicit: '«запомни»', model: 'сам', auto: 'авто', ui: 'вручную', user: '' };
let memFilter = '';

function memCats() { return (S.memory && S.memory.categories && S.memory.categories.length) ? S.memory.categories
  : [{ key: 'user', label: 'О тебе' }, { key: 'preference', label: 'Предпочтения' }, { key: 'people', label: 'Люди' }, { key: 'project', label: 'Проекты и дела' }, { key: 'other', label: 'Разное' }]; }
function memOpts(list, cur) { if (!list.some((o) => o[0] === cur)) list = [...list, [cur, String(cur)]].sort((a, b) => a[0] - b[0]); return list.map(([v, l]) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${esc(l)}</option>`).join(''); }

function memoryCard() {
  const s = S.settings; const m = S.memory || { facts: [], count: 0 };
  const on = s.memory_enabled !== false;
  return `<div class="card sect" id="m-card"><h3>${icon('user', 15)}Память<span class="badge">новое</span></h3>
    <div class="row"><div class="grow"><div class="t">Долгая память</div><div class="s">Помнить факты о тебе, твои предпочтения, людей и проекты между разговорами</div></div>${tgl('m-on', on)}</div>
    <div class="mem-body${on ? '' : ' off'}">
      <div class="row"><div class="grow"><div class="t">«Запомни, что …» — сохранять сразу</div><div class="s">Явная просьба записывается мгновенно, без участия модели</div></div>${tgl('m-explicit', s.memory_explicit !== false)}</div>
      <div class="row"><div class="grow"><div class="t">Сам замечать важное в разговоре</div><div class="s">Раз в несколько реплик модель выбирает устойчивые факты (учёба, вкусы, близкие, проекты). Тратит немного запросов</div></div>${tgl('m-auto', !!s.memory_auto_extract)}</div>
      <div class="grid2" style="margin-top:10px">
        <div class="field"><label>Сколько фактов хранить</label><select id="m-max" class="inp">${memOpts(MEM_MAX, s.memory_max_facts ?? 100)}</select></div>
        <div class="field"><label>Сколько подмешивать в каждый ответ</label><select id="m-inject" class="inp">${memOpts(MEM_INJECT, s.memory_inject ?? 8)}</select></div>
      </div>
      <div class="grid2" style="grid-template-columns: 2fr 1fr auto; align-items:end; margin-top:12px">
        <div class="field"><label>Добавить факт вручную</label><input id="m-text" class="inp" maxlength="300" placeholder="Например: готовлюсь к ЕГЭ по физике, экзамен в июне"></div>
        <div class="field"><label>Категория</label><select id="m-cat" class="inp"><option value="">определить само</option>${memCats().map((c) => `<option value="${c.key}">${esc(c.label)}</option>`).join('')}</select></div>
        <button id="m-add" class="btn primary">${icon('plus', 15)}Запомнить</button>
      </div>
      <div class="pchips" id="m-filter" style="margin-top:12px"></div>
      <div id="m-list"></div>
      <div class="row" style="border:0"><span class="grow s" id="m-count"></span><button id="m-clear" class="btn ghost">${icon('trash', 14)}Очистить память</button></div>
    </div>
    <div class="note">Голосом и текстом: «запомни, что я люблю кофе без сахара», «забудь, что я учусь в 10 классе», «что ты обо мне знаешь?». Память лежит только на этом ПК (<b>${esc((m.path || (S.dataDir ? S.dataDir + '\\memory.json' : 'memory.json')))}</b>); в каждый ответ уходит лишь несколько подходящих фактов — к выбранной модели (локальной или облачной). Пароли, коды, ключи и номера карт не запоминаются; тексты фактов не пишутся в лог. Закреплённые (кнопка-булавка) факты попадают в каждый ответ и не вытесняются.</div></div>`;
}

function renderMemoryList() {
  const box = $('#m-list'); if (!box) return;
  const m = S.memory || { facts: [] }; const facts = m.facts || [];
  const cats = memCats();
  const counts = {}; facts.forEach((f) => { counts[f.category] = (counts[f.category] || 0) + 1; });
  if (memFilter && !counts[memFilter]) memFilter = '';
  $('#m-filter').innerHTML = facts.length ? [['', `Все · ${facts.length}`], ...cats.filter((c) => counts[c.key]).map((c) => [c.key, `${c.label} · ${counts[c.key]}`])]
    .map(([k, l]) => `<button class="pchip${k === memFilter ? ' on' : ''}" data-mf="${k}">${esc(l)}</button>`).join('') : '';
  const shown = facts.filter((f) => !memFilter || f.category === memFilter);
  const label = Object.fromEntries(cats.map((c) => [c.key, c.label]));
  box.innerHTML = shown.length ? shown.map((f) => `<div class="row cmd-row mem-row"><div class="grow"><div class="prompt mem-text" title="${esc(f.text)}">${esc(f.text)}</div>
      <div class="s">${esc(label[f.category] || f.category)}${MEM_SRC[f.source] ? ' · ' + esc(MEM_SRC[f.source]) : ''}</div></div>
      <button class="icon-btn${f.pinned ? ' on' : ''}" data-mpin="${esc(f.id)}" title="${f.pinned ? 'Открепить' : 'Закрепить: всегда в ответах, не вытесняется'}">${icon('pin', 14)}</button>
      <button class="icon-btn" data-mdel="${esc(f.id)}" title="Забыть">${icon('trash', 15)}</button></div>`).join('')
    : `<div class="note" style="margin:6px 0 10px">${facts.length ? 'В этой категории пусто.' : 'Пока ничего не помню. Скажи «запомни, что …» или добавь факт выше.'}</div>`;
  const cnt = $('#m-count'); if (cnt) cnt.textContent = `${facts.length} из ${(S.settings.memory_max_facts ?? m.max_facts ?? 100)}`;
  $$('#m-filter [data-mf]').forEach((b) => b.onclick = () => { memFilter = b.dataset.mf; renderMemoryList(); });
  $$('#m-list [data-mdel]').forEach((b) => b.onclick = async () => { const r = await API.memory_delete(b.dataset.mdel); memApply(r, 'Забыл'); });
  $$('#m-list [data-mpin]').forEach((b) => b.onclick = async () => {
    const f = facts.find((x) => x.id === b.dataset.mpin); if (!f) return;
    const r = await API.memory_update(f.id, null, null, !f.pinned); memApply(r, f.pinned ? 'Откреплено' : 'Закреплено');
  });
}

function memApply(r, okText) {
  if (r && r.memory) S.memory = r.memory;
  if (!r || !r.ok) { toast((r && r.error) || 'Не получилось', 'err', 4000); renderMemoryList(); return false; }
  if (okText) toast(okText, '', 1400);
  renderMemoryList();
  return true;
}

function bindMemory() {
  if (!$('#m-card')) return;
  $('#m-on').onclick = async () => { if (await save({ memory_enabled: !(S.settings.memory_enabled !== false) }, true)) { toast(S.settings.memory_enabled ? 'Память включена' : 'Память выключена — факты сохранены, но не используются', '', 2200); buildAI(); } };
  $('#m-explicit').onclick = async () => { if (await save({ memory_explicit: !(S.settings.memory_explicit !== false) }, true)) buildAI(); };
  $('#m-auto').onclick = async () => { if (await save({ memory_auto_extract: !S.settings.memory_auto_extract }, true)) { toast(S.settings.memory_auto_extract ? 'Буду сам замечать важное' : 'Запоминаю только по просьбе', '', 1800); buildAI(); } };
  $('#m-max').onchange = async () => { if (await save({ memory_max_facts: +$('#m-max').value }, true)) { await memRefresh(); toast('Лимит памяти: ' + S.settings.memory_max_facts, '', 1500); } };
  $('#m-inject').onchange = async () => { if (await save({ memory_inject: +$('#m-inject').value }, true)) toast('Сохранено', '', 1200); };
  const add = async () => {
    const t = $('#m-text').value.trim(); if (!t) { toast('Напиши факт', 'err'); return; }
    const r = await API.memory_add(t, $('#m-cat').value);
    if (memApply(r, r && r.updated ? 'Обновил похожий факт' : 'Запомнил')) $('#m-text').value = '';
  };
  $('#m-add').onclick = add;
  $('#m-text').onkeydown = (e) => { if (e.key === 'Enter') add(); };
  $('#m-clear').onclick = () => {
    const n = ((S.memory || {}).facts || []).length;
    if (!n) { toast('Память и так пустая', '', 1600); return; }
    openModal('Очистить память?', `${esc(NM())} забудет все ${n} фактов. Это нельзя отменить.`, 'Очистить', async () => { memApply(await API.memory_clear(), 'Память очищена'); });
  };
  renderMemoryList();
}

async function memRefresh() {
  try { if (API && API.memory_info) S.memory = await API.memory_info(); } catch (e) { }
  renderMemoryList();
}
function onMemoryEvent(d) {
  // the model / «запомни» changed memory: refresh the list when it is visible, tell the user briefly
  memRefresh();
  if (d && d.what === 'add' && (S.tab !== 'ai')) toast('Запомнил', '', 1400);
}
