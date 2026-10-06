// Browser-only mock of the Python bridge (open index.html?mock) for UI development.
(function () {
  const providers = {
    gemini: { label: 'Google Gemini', chat_models: ['gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-flash-latest'], lite_models: ['gemini-3.5-flash-lite'], key_url: 'https://aistudio.google.com/apikey', base_url: '' },
    openai: { label: 'OpenAI', chat_models: ['gpt-4.1-mini', 'gpt-4o-mini'], lite_models: ['gpt-4.1-nano'], key_url: 'https://platform.openai.com/api-keys', base_url: '' },
    xai: { label: 'xAI Grok', chat_models: ['grok-4-fast', 'grok-3-mini'], lite_models: ['grok-4-fast'], key_url: 'https://console.x.ai', base_url: '' },
    hubris: { label: 'Hubris (рубли, все модели)', chat_models: ['google/gemini-3.8-flash', 'google/gemini-3.1-flash-lite', 'openai/gpt-5.4-mini', 'anthropic/claude-haiku-4.5', 'deepseek/deepseek-v4-flash', 'google/gemma-4-31b-it:free'], lite_models: ['google/gemini-3.1-flash-lite', 'google/gemini-2.5-flash-lite', 'openai/gpt-5.4-nano'], key_url: 'https://hubris.pw/keys', base_url: 'https://api.hubris.pw/v1', openai_compat: true },
    custom: { label: 'Свой OpenAI-совместимый API', chat_models: [], lite_models: [], key_url: 'https://openrouter.ai/keys', base_url: '', openai_compat: true, editable_base: true },
    ollama: { label: 'Локальный (Ollama)', chat_models: ['gpt-oss:20b', 'gemma4:26b', 'gemma4:12b', 'qwen3:14b', 'ministral-3:14b', 'qwen3.5:9b', 'qwen3.5:27b'], lite_models: ['gpt-oss:20b', 'gemma4:26b', 'gemma4:12b', 'qwen3:14b', 'ministral-3:14b', 'qwen3.5:9b', 'qwen3.5:27b'], key_url: '', base_url: 'http://127.0.0.1:11434', local: true },
  };
  const whisperMock = { engine: 'vosk', model: 'large-v3-turbo', device_pref: 'auto', lang_mode: 'pair', available: true, import_error: '', gpu: 'NVIDIA GeForce RTX 5070 Ti', cuda: false,
    catalog: [{ key: 'small', label: 'Small — быстрая, проще', mb: 484, installed: false }, { key: 'medium', label: 'Medium', mb: 1530, installed: false }, { key: 'large-v3-turbo', label: 'Large-v3 Turbo — лучшая (рекомендую)', mb: 1620, installed: false }],
    downloading: null, error: null, loaded: false, device: '', compute: '', loading: false, gpu_error: '', load_sec: 0, loaded_model: '' };
  const settings = { provider: 'gemini', models: { gemini: { chat: 'gemini-3.5-flash', lite: 'gemini-3.5-flash-lite' }, openai: { chat: 'gpt-4.1-mini', lite: 'gpt-4.1-nano' }, xai: { chat: 'grok-4-fast', lite: 'grok-4-fast' }, hubris: { chat: 'google/gemini-3.8-flash', lite: 'google/gemini-3.1-flash-lite' }, custom: { chat: '', lite: '' }, ollama: { chat: 'gemma4:12b', lite: 'gemma4:12b' } }, ai_route: 'local_first', gpu_free_in_games: true, local_keep_alive_min: 10,
    api_keys: { gemini: 'x', openai: '', xai: '', hubris: '', custom: '', ollama: '' }, base_urls: { gemini: '', openai: '', xai: '', hubris: '', custom: '', ollama: '' }, follow_mode: '2m', pc_hearing: false, voice: 'ru-RU-DmitryNeural', tts_rate: 5, tts_volume: 85, speak_replies: true,
    hotkey: 'ctrl+alt+j', wake_word: true, wake_mode: 'name', wake_threshold: 0.5, name_threshold: 0.5, live_mode: /live=1/.test(location.search), live_interval_sec: 45, live_min_gap_sec: 180, live_talk: 'some', live_reply_sec: 8, screen_check_sec: 90, title_check_sec: 5, nudge_cooldown_sec: 60, distraction_grace_sec: 8,
    distractions: ['tiktok', 'shorts', 'reels', 'instagram'], focus_minutes: 25, break_minutes: 5, rounds: 1, autostart: false, always_on_top: false, close_to_tray: true,
    persona: '', confirm_actions: false, pc_control: 'standard', tts_engine: 'piper', piper_voice: 'ru_RU-dmitri-medium', answer_lang: 'auto', speech_lang: 'ru', stt_en_pass: true, stt_en_model: 'small', stt_engine: 'vosk', whisper_model: 'large-v3-turbo', whisper_device: 'auto', whisper_lang: 'pair', voice_contacts: ['Nehto', 'Мама=@mama_tg'], piper_voices: { pl: 'custom:moya_gosya' }, stt_mode: 'local', fast_replies: true, vad_silence_ms: 600, custom_commands: [], user_name: 'Вова', assistant_name: (/name=([^&]+)/.exec(location.search) || [])[1] ? decodeURIComponent(/name=([^&]+)/.exec(location.search)[1]) : 'Джарвис', character_preset: 'butler', character: '' };
  const emit = (e, d) => window.J.onEvents([{ e, d }]);
  Object.assign(settings, { memory_enabled: true, memory_max_facts: 100, memory_inject: 8, memory_explicit: true, memory_auto_extract: false });
  const memCats = [{ key: 'user', label: 'О тебе' }, { key: 'preference', label: 'Предпочтения' }, { key: 'people', label: 'Люди' }, { key: 'project', label: 'Проекты и дела' }, { key: 'other', label: 'Разное' }];
  const memFacts = [
    { id: 'a1', text: 'Учусь в 10 классе, готовлюсь к ЕГЭ по физике', category: 'user', source: 'explicit', pinned: true, created: 0, updated: 3 },
    { id: 'a2', text: 'Люблю кофе без сахара', category: 'preference', source: 'model', pinned: false, created: 0, updated: 2 },
    { id: 'a3', text: 'Сестру зовут Аня', category: 'people', source: 'ui', pinned: false, created: 0, updated: 1 }];
  const memPayload = () => ({ ok: true, facts: memFacts.slice(), count: memFacts.length, max_facts: settings.memory_max_facts, categories: memCats, path: 'C:\\Users\\user\\AppData\\Local\\Jarvis\\memory.json', settings: {} });
  const game = /game=1/.test(location.search);
  const local = { installed: true, running: true, version: '0.35.1', model: 'gemma4:12b', mode: 'local_first', model_ok: true, warming: false, pulling: null, error: '', keep_alive: game ? '20s' : '10m', load_sec: 6.2, gpu_free: true, game: game ? 'isaac-ng.exe' : '',
    loaded: game ? null : { name: 'gemma4:12b', gb: 9.3, vram_gb: 9.3, gpu: 1.0, until: '', ctx: 8192 },
    models: [
      { name: 'gemma4:12b', gb: 8.0, params: '11.9B', quant: 'Q4_K_M', family: 'gemma4' },
      { name: 'qwen3.5:9b', gb: 6.6, params: '9.7B', quant: 'Q4_K_M', family: 'qwen35' },
      { name: 'ministral-3:14b', gb: 9.1, params: '14B', quant: 'Q4_K_M', family: 'ministral3' }
    ],
    recommended: [
      { name: 'gpt-oss:20b', label: 'GPT-OSS 20B', gb: 14.0, note: 'сильнее в рассуждениях и командах (MoE, ~14 ГБ VRAM); без зрения экрана — для live/фокуса лучше Gemma' },
      { name: 'gemma4:26b', label: 'Gemma 4 26B', gb: 16.0, note: 'заметно умнее 12B, видит экран · впритык к 16 ГБ (выгрузи Whisper / не держи другие модели)' },
      { name: 'gemma4:12b', label: 'Gemma 4 12B', gb: 8.0, note: 'Лучший выбор: отлично по-русски и по-украински, видит экран, уверенно вызывает команды · ~9 ГБ видеопамяти' },
      { name: 'qwen3:14b', label: 'Qwen 3 14B', gb: 9.3, note: 'плотнее и умнее 9B · ~9–11 ГБ; без встроенного зрения как у Gemma 4' },
      { name: 'ministral-3:14b', label: 'Ministral 3 14B', gb: 9.1, note: 'Запасной: быстрый, видит экран, команды почти всегда; в русском бывают ошибки · ~9,5 ГБ' },
      { name: 'qwen3.5:9b', label: 'Qwen 3.5 9B', gb: 6.6, note: 'Самый лёгкий (~5,5 ГБ), видит экран, но часто говорит «открываю», ничего не открыв' },
      { name: 'qwen3.5:27b', label: 'Qwen 3.5 27B', gb: 17.0, note: 'ещё сильнее, ~17–20 ГБ — часть может уйти в RAM, медленнее; видит экран' }
    ] };
  const focus = { active: false, task: '', phase: 'idle', phase_label: '', running: false, remaining: 1500, total: 1500, round: 1, rounds: 1, guard: 'Страж ждёт фокус-сессию', today_min: 42, distractions: 2 };
  if (/demo=chat/.test(location.search)) setTimeout(() => { const i = document.querySelector('#input'); i.value = 'что ты умеешь?'; i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); }, 500);
  if (/demo=think/.test(location.search)) setTimeout(() => { const i = document.querySelector('#input'); i.value = 'открой телеграм'; i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); window.MOCK_THINK = 1; }, 500);
  const cm = /click=([^&]+)/.exec(location.search);
  if (cm) setTimeout(() => decodeURIComponent(cm[1]).split(',').forEach((sel, i) => setTimeout(() => document.querySelector(sel).click(), i * 300)), 1500);
  const tm = /tab=(\w+)/.exec(location.search);
  if (tm) setTimeout(() => document.querySelector(`.tab[data-tab=${tm[1]}]`).click(), 400);
  const follow = { active: /follow=1/.test(location.search), mode: settings.follow_mode, always: false, remaining: 100, left: '1:40', listening: true };
  const pcMock = { running: false, status: 'Выключено', device: 'Динамики (Realtek)', error: '', available: true, import_error: '', lines: 2, segments: 2, dropped: 0, enabled: false,
    recent: [{ t: '12:01:04', text: 'Сегодня мы разберём, как работает трансформер', window: 'YouTube — Google Chrome' }, { t: '12:01:09', text: 'и почему внимание — это всё, что нужно', window: '' }] };
  window.MOCK_API = {
    init: async () => ({ version: '1.6.0', follow, pc_hearing: pcMock, settings, providers, local, presets: [
      { key: 'butler', label: 'Джарвис-дворецкий', prompt: 'Спокойный компаньон, делает дела вместе с тобой.', voice: 'ru-RU-DmitryNeural', rate: 0 },
      { key: 'companion', label: 'Компаньон', prompt: 'Тёплый близкий компаньон: делаешь дела вместе, не только смотришь.', voice: 'ru-RU-DmitryNeural', rate: 8 },
      { key: 'friend', label: 'Друг', prompt: 'Тёплый, простой и весёлый, как лучший друг.', voice: 'ru-RU-DmitryNeural', rate: 8 },
      { key: 'coach', label: 'Строгий тренер', prompt: 'Строгий, требовательный тренер.', voice: 'ru-RU-DmitryNeural', rate: 6 },
      { key: 'sarcastic', label: 'Саркастичный', prompt: 'Саркастичный и язвительный, но добрый.', voice: 'ru-RU-DmitryNeural', rate: 4 },
      { key: 'calm', label: 'Спокойный', prompt: 'Очень спокойный и мягкий.', voice: 'ru-RU-SvetlanaNeural', rate: -6 }], voices: ['ru-RU-DmitryNeural', 'ru-RU-SvetlanaNeural'], abilities: [
      { icon: 'app', title: 'Открыть программу', desc: 'Ищет в меню «Пуск»', example: 'Открой телеграм' },
      { icon: 'play', title: 'Музыка', desc: 'Spotify / медиаклавиши', example: 'Включи музыку в спотике' },
      { icon: 'globe', title: 'Открыть сайт', desc: 'Любая ссылка', example: 'Открой ютуб' },
      { icon: 'target', title: 'Фокус-сессия', desc: 'Таймер + страж', example: 'Я делаю домашку 40 минут' },
      { icon: 'bell', title: 'Напоминания', desc: 'Через N минут', example: 'Напомни через 15 минут' }],
      tools: [], chat: [], focus, memory: memPayload(), stats: { today: '42 мин', week: '5 ч 10 мин', month: '12 ч', total: '30 ч', streak: '3 дня', distractions: 2,
      days: ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].map((l, i) => ({ label: l, date: '0' + (i + 1) + '.10', min: [30, 55, 0, 80, 25, 60, 42][i] })) },
      state: 'idle', hotkey: 'Ctrl+Alt+J', autostart: false, wake: { enabled: true, mode: settings.wake_mode, phrase: settings.assistant_name, downloading: false, error: null }, live: { enabled: settings.live_mode, status: settings.live_mode ? '10:58 посмотрел — молчу (пишет код в VS Code)' : 'Выключен' }, maximized: false, has_key: true, lite_model: 'gemini-3.5-flash-lite',
      tts: { engine: 'piper', voice: 'ru_RU-dmitri-medium', active: 'piper', available: true, downloading: /dl=1/.test(location.search) ? 'ru_RU-irina-medium' : null, error: null,
        voices: [{"key": "ru_RU-dmitri-medium", "label": "Дмитрий", "gender": "м", "lang": "ru", "installed": true, "license": "CC0", "mb": 63}, {"key": "ru_RU-denis-medium", "label": "Денис", "gender": "м", "lang": "ru", "installed": false, "license": "CC0", "mb": 63}, {"key": "ru_RU-irina-medium", "label": "Ирина", "gender": "ж", "lang": "ru", "installed": false, "license": "RHVoice", "mb": 63}, {"key": "ru_RU-ruslan-medium", "label": "Руслан", "gender": "м", "lang": "ru", "installed": false, "license": "CC BY-NC-SA 4.0", "mb": 63}, {"key": "uk_UA-mykyta-medium", "label": "Микита", "gender": "м", "lang": "uk", "installed": true, "license": "CC0", "mb": 77}, {"key": "uk_UA-lada-medium", "label": "Лада", "gender": "ж", "lang": "uk", "installed": true, "license": "CC0", "mb": 77}, {"key": "uk_UA-tetiana-medium", "label": "Тетяна", "gender": "ж", "lang": "uk", "installed": true, "license": "CC0", "mb": 77}, {"key": "en_US-ryan-medium", "label": "Ryan (US)", "gender": "м", "lang": "en", "installed": true, "license": "CC BY-NC-SA 4.0", "mb": 63}, {"key": "en_US-lessac-medium", "label": "Lessac (US)", "gender": "ж", "lang": "en", "installed": true, "license": "Blizzard 2013 (non-commercial)", "mb": 63}, {"key": "en_US-amy-medium", "label": "Amy (US)", "gender": "ж", "lang": "en", "installed": false, "license": "Mimic 3", "mb": 63}, {"key": "en_GB-alan-medium", "label": "Alan (UK)", "gender": "м", "lang": "en", "installed": false, "license": "Mimic 3", "mb": 63}, {"key": "de_DE-thorsten-medium", "label": "Thorsten", "gender": "м", "lang": "de", "installed": true, "license": "CC0", "mb": 63}, {"key": "de_DE-kerstin-low", "label": "Kerstin", "gender": "ж", "lang": "de", "installed": false, "license": "CC0", "mb": 63}, {"key": "de_DE-ramona-low", "label": "Ramona", "gender": "ж", "lang": "de", "installed": false, "license": "M-AILABS", "mb": 63}, {"key": "pl_PL-darkman-medium", "label": "Darkman", "gender": "м", "lang": "pl", "installed": true, "license": "CC0", "mb": 63}, {"key": "pl_PL-gosia-medium", "label": "Gosia", "gender": "ж", "lang": "pl", "installed": true, "license": "CC0", "mb": 63}, {"key": "pl_PL-mc_speech-medium", "label": "Magda (MC Speech)", "gender": "ж", "lang": "pl", "installed": false, "license": "CC0", "mb": 63}, {"key": "custom:moya_gosya", "label": "Моя Гося", "gender": "", "installed": true, "lang": "pl", "lang_full": "pl_PL", "mb": 63, "custom": true}],
        voice_for: {"ru": "ru_RU-dmitri-medium", "uk": "uk_UA-mykyta-medium", "en": "en_US-ryan-medium", "de": "de_DE-thorsten-medium", "pl": "custom:moya_gosya"}, reply_lang: 'ru', downloads: [],
        langs: [{"code": "ru", "label": "Русский", "short": "RU", "vosk_mb": 46}, {"code": "uk", "label": "Українська", "short": "UK", "vosk_mb": 78}, {"code": "en", "label": "English", "short": "EN", "vosk_mb": 41}, {"code": "de", "label": "Deutsch", "short": "DE", "vosk_mb": 46}, {"code": "pl", "label": "Polski", "short": "PL", "vosk_mb": 53}], links: ["https://huggingface.co/rhasspy/piper-voices", "https://rhasspy.github.io/piper-samples/"],
        stt: { mode: 'local', model: true, loaded: true, downloading: false, lang: 'ru', mb: 46 },
        whisper: whisperMock,
        timing: { first_audio: 1.42, endpoint: 0.7, stt: 0.03, stt_mode: 'local', llm_first: 0.6, tts: 0.08, play: 0.01 } }, data_dir: 'C:\\Users\\user\\AppData\\Local\\Jarvis', update: false }),
    send: async (text) => {
      emit('state', { state: 'thinking', detail: 'Думаю…' });
      setTimeout(() => emit('chat', { role: 'user', text }), 50);
      setTimeout(() => {
        emit('chat', { role: 'jarvis', text: 'Я умею открывать программы и сайты, искать файлы, ставить таймеры и напоминания, смотреть на твой экран и следить, чтобы ты не залипал в TikTok во время фокус-сессии. Просто скажи, что нужно.' });
        emit('state', { state: 'speaking' });
      }, 900);
      setTimeout(() => emit('state', { state: 'idle' }), 2500);
      return true;
    },
    listen: async () => { emit('state', { state: 'listening' }); setTimeout(() => emit('state', { state: 'idle' }), 3000); },
    stop_speaking: async () => {}, new_chat: async () => {}, get_chat: async () => [],
    save: async (p) => { const m = (a, b) => { for (const k in b) { if (b[k] && typeof b[k] === 'object' && !Array.isArray(b[k])) { a[k] = a[k] || {}; m(a[k], b[k]); } else a[k] = b[k]; } }; m(settings, p); return { ok: true, settings, hotkey: 'Ctrl+Alt+J', has_key: true }; },
    set_wake: async (on) => window.MOCK_API.set_wake_mode(on ? 'name' : 'off'),
    set_wake_mode: async (mode) => { settings.wake_mode = mode; return { ok: true, settings, wake: { enabled: mode !== 'off', mode, phrase: settings.assistant_name, downloading: false, error: null } }; },
    set_live: async (on) => { settings.live_mode = on; return { ok: true, settings, live: { enabled: on, status: on ? 'Включён — присматриваюсь' : 'Выключен' } }; },
    test_ai: async (p) => p === 'ollama' ? 'gemma4:12b (локально): «Да» за 0.4 с' : 'gemini-3.5-flash: «Да» за 1.0 с',
    local_info: async () => local, local_start: async () => ({ ok: true, local }), local_load: async () => ({ ok: true, local }),
    local_unload: async () => { local.loaded = null; return { ok: true, local }; }, local_cancel_pull: async () => ({ ok: true }),
    local_pull: async (name) => { let d = 0; const t = 9.1e9; const iv = setInterval(() => { d = Math.min(t, d + 7e8); emit('local_pull', { model: name, status: d >= t ? 'success' : 'pulling', done: d, total: t }); if (d >= t) { clearInterval(iv); local.models.push({ name, gb: 9.1, params: '14B', quant: 'Q4_K_M' }); } }, 300); return { ok: true }; }, test_voice: async () => ({ ok: true }), download_voice: async () => ({ ok: true }), tts_info: async () => ({ whisper: Object.assign(whisperMock, { engine: settings.stt_engine }) }),
    whisper_info: async () => whisperMock,
    whisper_download: async (m) => { whisperMock.downloading = m; let d = 0; const t = 1.6e9; const iv = setInterval(() => { d = Math.min(t, d + 2e8); emit('whisper_dl', { stage: 'model', model: m, done: d, total: t });
      if (d >= t) { clearInterval(iv); whisperMock.downloading = null; whisperMock.catalog.forEach((c) => { if (c.key === m) c.installed = true; }); Object.assign(whisperMock, { loaded: true, loaded_model: m, device: 'cuda' }); emit('whisper', whisperMock); } }, 300);
      return { ok: true, whisper: whisperMock }; },
    whisper_cancel: async () => ({ ok: true }),
    whisper_delete: async (m) => { whisperMock.catalog.forEach((c) => { if (c.key === m) c.installed = false; }); whisperMock.loaded = false; return { ok: true, whisper: whisperMock }; },
    pick_voice_file: async () => '', import_voice: async () => ({ ok: false, error: 'mock' }), delete_voice: async () => ({ ok: true }),
    focus_start: async (task, min) => { Object.assign(focus, { active: true, task, phase: 'focus', phase_label: 'Фокус', running: true, remaining: min * 60, total: min * 60 }); return { ok: true, focus, first_block_ends_at: '22:10' }; },
    focus_stop: async () => { focus.active = false; return focus; }, focus_pause: async () => { focus.running = !focus.running; return focus; }, focus_skip: async () => focus, focus_state: async () => focus,
    stats: async () => (await window.MOCK_API.init()).stats, open_data_folder: async () => {}, open_link: async () => {}, list_versions: async () => ({ ok: true, current: '1.1.0', repo: 'https://github.com/Sinohara1/jarvis', frozen: true, releases: [
      { tag: 'v1.1.0', version: '1.1.0', date: '03.10.2026', size: 50960000, status: 'current', prerelease: false, notes: '• Можно переименовать ассистента — имя в окне, трее, чате и ответах\n• Характер: пресеты (дворецкий, друг, строгий тренер, саркастичный, спокойный) или свой текст' },
      { tag: 'v1.0.0', version: '1.0.0', date: '03.10.2026', size: 50950000, status: 'older', prerelease: false, notes: '• Новый интерфейс\n• Менеджер версий: обновление и откат прямо из настроек' }] }),
    install_version: async (tag) => { let d = 0; const t = 50960000; const iv = setInterval(() => { d = Math.min(t, d + 9e6); emit('upd_progress', { tag, done: d, total: t }); if (d >= t) { clearInterval(iv); } }, 200); return { ok: true }; },
    list_models: async (p) => p === 'custom' && !settings.base_urls.custom ? { ok: false, error: 'Не указан адрес API' } : { ok: true, count: 3, models: [
      { id: 'google/gemini-3.1-flash-lite', label: '25/150 ₽ за 1M · инструменты · картинки', tools: true, vision: true },
      { id: 'deepseek/deepseek-v4-flash', label: '14/28 ₽ за 1M · инструменты', tools: true, vision: false },
      { id: 'google/gemma-4-31b-it:free', label: 'бесплатно · картинки', tools: false, vision: true }] },
    follow_stop: async () => { follow.active = false; return { ok: true, follow }; }, follow_info: async () => follow,
    pc_hearing_info: async () => Object.assign(pcMock, { enabled: settings.pc_hearing, running: settings.pc_hearing, status: settings.pc_hearing ? 'Слушаю звук ПК (Динамики (Realtek))' : 'Выключено' }),
    memory_info: async () => memPayload(),
    memory_add: async (text, category) => { if (/парол|password/i.test(text)) return { ok: false, error: 'это похоже на пароль — такое я не запоминаю', memory: memPayload() }; memFacts.unshift({ id: 'm' + Date.now(), text, category: category || 'other', source: 'ui', pinned: false }); return { ok: true, memory: memPayload() }; },
    memory_update: async (id, text, category, pinned) => { const f = memFacts.find((x) => x.id === id); if (f && pinned !== null && pinned !== undefined) f.pinned = pinned; return { ok: !!f, memory: memPayload() }; },
    memory_delete: async (id) => { const i = memFacts.findIndex((x) => x.id === id); if (i >= 0) memFacts.splice(i, 1); return { ok: i >= 0, memory: memPayload() }; },
    memory_clear: async () => { const n = memFacts.length; memFacts.length = 0; return { ok: true, removed: n, memory: memPayload() }; },
    win_minimize: async () => {}, win_toggle_max: async () => false, win_close: async () => {}, win_begin_move: async () => {}, win_begin_resize: async () => {}, hide: async () => {}, quit: async () => {},
  };
})();
