// badge.js — обновление индикатора «● Симулятор / ● Онлайн / ● Офлайн»
// на всех страницах VEGAREX (а не только /). Грузится через base.html.
//
// На /index.html бейдж дополнительно обновляется WebSocket'ом из control.js
// (мгновенно при подключении/отключении). На остальных страницах
// (Настройки, Миссии, Справка, Статистика, История, О программе, Админ)
// control.js не подключён → бейдж раньше «застревал» в значении из шаблона.
//
// Решение: опрашиваем /api/status раз в 3 секунды, обновляем DOM.
// Если страница /index.html — control.js всё равно подмешивает свежее
// значение через WS, конфликта нет (оба пишут одно и то же).
(function() {
  const el = document.getElementById('robot-badge');
  if (!el) return;

  const mode_el = document.getElementById('robot-mode-badge');

  // Режим подключения → текст и класс чипа SIM / LOC / NET.
  const MODE = {
    sim:   { text: 'SIM', cls: 'badge badge--mode-sim' },
    local: { text: 'LOC', cls: 'badge badge--mode-loc' },
    prod:  { text: 'NET', cls: 'badge badge--mode-net' },
  };

  function apply(status) {
    if (!status) return;
    if (status.simulated) {
      el.textContent = '● Симулятор';
      el.className = 'badge badge--online';
    } else if (status.robot_online) {
      el.textContent = '● Онлайн';
      el.className = 'badge badge--online';
    } else {
      el.textContent = '● Офлайн';
      el.className = 'badge badge--offline';
    }
    if (mode_el && status.mode && MODE[status.mode]) {
      const m = MODE[status.mode];
      mode_el.textContent = m.text;
      mode_el.className = m.cls;
    }
  }

  async function update() {
    try {
      const r = await fetch('/api/status', {cache: 'no-store'});
      if (!r.ok) return;
      apply(await r.json());
    } catch (_) {
      // Сервер недоступен — пусть бейдж останется как есть
    }
  }

  // Первый запрос — сразу. Дальше — каждые 3 секунды.
  update();
  setInterval(update, 3000);
})();
