// Browser-only mock of the Python bridge (open index.html?mock) for UI development.
(function () {
  const providers = {
    gemini: { label: 'Google Gemini', chat_models: ['gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-flash-latest'], lite_models: ['gemini-3.5-flash-lite'], key_url: 'https://aistudio.google.com/apikey', base_url: '' },
    openai: { label: 'OpenAI', chat_models: ['gpt-4.1-mini', 'gpt-4o-mini'], lite_models: ['gpt-4.1-nano'], key_url: 'https://platform.openai.com/api-keys', base_url: '' },
    xai: { label: 'xAI Grok', chat_models: ['grok-4-fast', 'grok-3-mini'], lite_models: ['grok-4-fast'], key_url: 'https://console.x.ai', base_url: '' },
  };
  const settings = { provider: 'gemini', models: { gemini: { chat: 'gemini-3.5-flash', lite: 'gemini-3.5-flash-lite' }, openai: { chat: 'gpt-4.1-mini', lite: 'gpt-4.1-nano' }, xai: { chat: 'grok-4-fast', lite: 'grok-4-fast' } },
    api_keys: { gemini: 'x', openai: '', xai: '' }, base_urls: { gemini: '', openai: '', xai: '' }, voice: 'ru-RU-DmitryNeural', tts_rate: 5, tts_volume: 85, speak_replies: true,
    hotkey: 'ctrl+alt+j', wake_word: true, wake_mode: 'name', wake_threshold: 0.5, name_threshold: 0.5, live_mode: /live=1/.test(location.search), live_interval_sec: 45, live_min_gap_sec: 180, live_talk: 'some', live_reply_sec: 8, screen_check_sec: 90, title_check_sec: 5, nudge_cooldown_sec: 60, distraction_grace_sec: 8,
    distractions: ['tiktok', 'shorts', 'reels', 'instagram'], focus_minutes: 25, break_minutes: 5, rounds: 1, autostart: false, always_on_top: false, close_to_tray: true,
    persona: '', confirm_actions: false, tts_engine: 'piper', piper_voice: 'ru_RU-dmitri-medium', answer_lang: 'auto', speech_lang: 'ru', piper_voices: { pl: 'custom:moya_gosya' }, stt_mode: 'local', fast_replies: true, vad_silence_ms: 600, custom_commands: [], user_name: 'Вова', assistant_name: (/name=([^&]+)/.exec(location.search) || [])[1] ? decodeURIComponent(/name=([^&]+)/.exec(location.search)[1]) : 'Джарвис', character_preset: 'butler', character: '' };
  const emit = (e, d) => window.J.onEvents([{ e, d }]);
  const focus = { active: false, task: '', phase: 'idle', phase_label: '', running: false, remaining: 1500, total: 1500, round: 1, rounds: 1, guard: 'Страж ждёт фокус-сессию', today_min: 42, distractions: 2 };
  if (/demo=chat/.test(location.search)) setTimeout(() => { const i = document.querySelector('#input'); i.value = 'что ты умеешь?'; i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); }, 500);
  if (/demo=think/.test(location.search)) setTimeout(() => { const i = document.querySelector('#input'); i.value = 'открой телеграм'; i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); window.MOCK_THINK = 1; }, 500);
  const cm = /click=([^&]+)/.exec(location.search);
  if (cm) setTimeout(() => decodeURIComponent(cm[1]).split(',').forEach((sel, i) => setTimeout(() => document.querySelector(sel).click(), i * 300)), 1500);
  const tm = /tab=(\w+)/.exec(location.search);
  if (tm) setTimeout(() => document.querySelector(`.tab[data-tab=${tm[1]}]`).click(), 400);
  window.MOCK_API = {
    init: async () => ({ version: '1.4.0', settings, providers, presets: [
      { key: 'butler', label: 'Джарвис-дворецкий', prompt: 'Спокойный, остроумный и заботливый, как Джарвис из «Железного человека», но без пафоса.', voice: 'ru-RU-DmitryNeural', rate: 0 },
      { key: 'friend', label: 'Друг', prompt: 'Тёплый, простой и весёлый, как лучший друг.', voice: 'ru-RU-DmitryNeural', rate: 8 },
      { key: 'coach', label: 'Строгий тренер', prompt: 'Строгий, требовательный тренер.', voice: 'ru-RU-DmitryNeural', rate: 6 },
      { key: 'sarcastic', label: 'Саркастичный', prompt: 'Саркастичный и язвительный, но добрый.', voice: 'ru-RU-DmitryNeural', rate: 4 },
      { key: 'calm', label: 'Спокойный', prompt: 'Очень спокойный и мягкий.', voice: 'ru-RU-SvetlanaNeural', rate: -6 }], voices: ['ru-RU-DmitryNeural', 'ru-RU-SvetlanaNeural'], abilities: [
      { icon: 'app', title: 'Открыть программу', desc: 'Ищет в меню «Пуск»', example: 'Открой телеграм' },
      { icon: 'globe', title: 'Открыть сайт', desc: 'Любая ссылка', example: 'Открой ютуб' },
      { icon: 'target', title: 'Фокус-сессия', desc: 'Таймер + страж', example: 'Я делаю домашку 40 минут' },
      { icon: 'bell', title: 'Напоминания', desc: 'Через N минут', example: 'Напомни через 15 минут' }],
      tools: [], chat: [], focus, stats: { today: '42 мин', week: '5 ч 10 мин', month: '12 ч', total: '30 ч', streak: '3 дня', distractions: 2,
      days: ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].map((l, i) => ({ label: l, date: '0' + (i + 1) + '.10', min: [30, 55, 0, 80, 25, 60, 42][i] })) },
      state: 'idle', hotkey: 'Ctrl+Alt+J', autostart: false, wake: { enabled: true, mode: settings.wake_mode, phrase: settings.assistant_name, downloading: false, error: null }, live: { enabled: settings.live_mode, status: settings.live_mode ? '10:58 посмотрел — молчу (пишет код в VS Code)' : 'Выключен' }, maximized: false, has_key: true, lite_model: 'gemini-3.5-flash-lite',
      tts: { engine: 'piper', voice: 'ru_RU-dmitri-medium', active: 'piper', available: true, downloading: /dl=1/.test(location.search) ? 'ru_RU-irina-medium' : null, error: null,
        voices: [{"key": "ru_RU-dmitri-medium", "label": "Дмитрий", "gender": "м", "lang": "ru", "installed": true, "license": "CC0", "mb": 63}, {"key": "ru_RU-denis-medium", "label": "Денис", "gender": "м", "lang": "ru", "installed": false, "license": "CC0", "mb": 63}, {"key": "ru_RU-irina-medium", "label": "Ирина", "gender": "ж", "lang": "ru", "installed": false, "license": "RHVoice", "mb": 63}, {"key": "ru_RU-ruslan-medium", "label": "Руслан", "gender": "м", "lang": "ru", "installed": false, "license": "CC BY-NC-SA 4.0", "mb": 63}, {"key": "uk_UA-mykyta-medium", "label": "Микита", "gender": "м", "lang": "uk", "installed": true, "license": "CC0", "mb": 77}, {"key": "uk_UA-lada-medium", "label": "Лада", "gender": "ж", "lang": "uk", "installed": true, "license": "CC0", "mb": 77}, {"key": "uk_UA-tetiana-medium", "label": "Тетяна", "gender": "ж", "lang": "uk", "installed": true, "license": "CC0", "mb": 77}, {"key": "en_US-ryan-medium", "label": "Ryan (US)", "gender": "м", "lang": "en", "installed": true, "license": "CC BY-NC-SA 4.0", "mb": 63}, {"key": "en_US-lessac-medium", "label": "Lessac (US)", "gender": "ж", "lang": "en", "installed": true, "license": "Blizzard 2013 (non-commercial)", "mb": 63}, {"key": "en_US-amy-medium", "label": "Amy (US)", "gender": "ж", "lang": "en", "installed": false, "license": "Mimic 3", "mb": 63}, {"key": "en_GB-alan-medium", "label": "Alan (UK)", "gender": "м", "lang": "en", "installed": false, "license": "Mimic 3", "mb": 63}, {"key": "de_DE-thorsten-medium", "label": "Thorsten", "gender": "м", "lang": "de", "installed": true, "license": "CC0", "mb": 63}, {"key": "de_DE-kerstin-low", "label": "Kerstin", "gender": "ж", "lang": "de", "installed": false, "license": "CC0", "mb": 63}, {"key": "de_DE-ramona-low", "label": "Ramona", "gender": "ж", "lang": "de", "installed": false, "license": "M-AILABS", "mb": 63}, {"key": "pl_PL-darkman-medium", "label": "Darkman", "gender": "м", "lang": "pl", "installed": true, "license": "CC0", "mb": 63}, {"key": "pl_PL-gosia-medium", "label": "Gosia", "gender": "ж", "lang": "pl", "installed": true, "license": "CC0", "mb": 63}, {"key": "pl_PL-mc_speech-medium", "label": "Magda (MC Speech)", "gender": "ж", "lang": "pl", "installed": false, "license": "CC0", "mb": 63}, {"key": "custom:moya_gosya", "label": "Моя Гося", "gender": "", "installed": true, "lang": "pl", "lang_full": "pl_PL", "mb": 63, "custom": true}],
        voice_for: {"ru": "ru_RU-dmitri-medium", "uk": "uk_UA-mykyta-medium", "en": "en_US-ryan-medium", "de": "de_DE-thorsten-medium", "pl": "custom:moya_gosya"}, reply_lang: 'ru', downloads: [],
        langs: [{"code": "ru", "label": "Русский", "short": "RU", "vosk_mb": 46}, {"code": "uk", "label": "Українська", "short": "UK", "vosk_mb": 78}, {"code": "en", "label": "English", "short": "EN", "vosk_mb": 41}, {"code": "de", "label": "Deutsch", "short": "DE", "vosk_mb": 46}, {"code": "pl", "label": "Polski", "short": "PL", "vosk_mb": 53}], links: ["https://huggingface.co/rhasspy/piper-voices", "https://rhasspy.github.io/piper-samples/"],
        stt: { mode: 'local', model: true, loaded: true, downloading: false, lang: 'ru', mb: 46 },
        timing: { first_audio: 1.42, endpoint: 0.7, stt: 0.03, stt_mode: 'local', llm_first: 0.6, tts: 0.08, play: 0.01 } }, data_dir: 'C:\\Users\\rpgpe\\AppData\\Local\\Jarvis', update: false }),
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
    test_ai: async () => 'gemini-3.5-flash: «Да» за 1.0 с', test_voice: async () => ({ ok: true }), download_voice: async () => ({ ok: true }), tts_info: async () => ({}),
    pick_voice_file: async () => '', import_voice: async () => ({ ok: false, error: 'mock' }), delete_voice: async () => ({ ok: true }),
    focus_start: async (task, min) => { Object.assign(focus, { active: true, task, phase: 'focus', phase_label: 'Фокус', running: true, remaining: min * 60, total: min * 60 }); return { ok: true, focus, first_block_ends_at: '22:10' }; },
    focus_stop: async () => { focus.active = false; return focus; }, focus_pause: async () => { focus.running = !focus.running; return focus; }, focus_skip: async () => focus, focus_state: async () => focus,
    stats: async () => (await window.MOCK_API.init()).stats, open_data_folder: async () => {}, open_link: async () => {}, list_versions: async () => ({ ok: true, current: '1.1.0', repo: 'https://github.com/Sinohara1/jarvis-releases', frozen: true, releases: [
      { tag: 'v1.1.0', version: '1.1.0', date: '03.10.2026', size: 50960000, status: 'current', prerelease: false, notes: '• Можно переименовать ассистента — имя в окне, трее, чате и ответах\n• Характер: пресеты (дворецкий, друг, строгий тренер, саркастичный, спокойный) или свой текст' },
      { tag: 'v1.0.0', version: '1.0.0', date: '03.10.2026', size: 50950000, status: 'older', prerelease: false, notes: '• Новый интерфейс\n• Менеджер версий: обновление и откат прямо из настроек' }] }),
    install_version: async (tag) => { let d = 0; const t = 50960000; const iv = setInterval(() => { d = Math.min(t, d + 9e6); emit('upd_progress', { tag, done: d, total: t }); if (d >= t) { clearInterval(iv); } }, 200); return { ok: true }; },
    win_minimize: async () => {}, win_toggle_max: async () => false, win_close: async () => {}, win_begin_move: async () => {}, win_begin_resize: async () => {}, hide: async () => {}, quit: async () => {},
  };
})();
