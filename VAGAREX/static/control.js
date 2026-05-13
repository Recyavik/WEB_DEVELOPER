/**
 * control.js — WebSocket клиент + управление интерфейсом
 *
 * Работает с canvas.js (RobotCanvas) и WebSocket /ws.
 */

(function () {
  'use strict';

  // ── WebSocket ───────────────────────────────────────────────────────────────

  let ws   = null;
  let canvas = null;
  let reconnectTimer = null;

  function wsConnect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
      clearTimeout(reconnectTimer);
      logMsg('Соединение установлено.', 'info');
      updateRobotBadge(true);
    };

    ws.onmessage = e => {
      try {
        const msg = JSON.parse(e.data);
        handleWsMsg(msg);
      } catch (_) {}
    };

    ws.onclose = () => {
      logMsg('Соединение потеряно. Переподключение…', 'warning');
      // Сразу гасим зеленый бейдж — иначе он висит «Онлайн» при оборванном WS.
      updateRobotBadge(false);
      reconnectTimer = setTimeout(wsConnect, 3000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  function sendCmd(text) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      logMsg('Нет соединения!', 'error');
      return;
    }
    ws.send(JSON.stringify({ type: 'command', text }));
    logMsg(`→ ${text}`, 'info');
  }

  // ── Обработка сообщений ─────────────────────────────────────────────────────

  function handleWsMsg(msg) {
    switch (msg.type) {
      case 'full_state':
        updateRobotBadge(msg.robot_online);
        if (canvas) canvas.update(msg.robot, msg.world);
        updateStatePanel(msg.robot);
        updateZoneList(msg.world.danger_zones || []);
        break;

      case 'state':
        if (canvas) {
          if (msg.path) canvas.pathHistory = msg.path;
          canvas.updateRobot(msg.robot);
        }
        updateStatePanel(msg.robot);
        break;

        case 'world':
        if (canvas) canvas.updateWorld(msg.world);
        updateZoneList(msg.world.danger_zones || []);
        break;

      case 'path_visible':
        if (canvas) {
          canvas.showPath = msg.visible;
          const chk = document.getElementById('chk-path');
          if (chk) chk.checked = msg.visible;
          canvas.draw();
        }
        break;

      case 'modal_warning':
        // Сервер прислал блокирующее предупреждение — показываем модалку.
        showWarningModal(msg.title || 'Команда не выполнена',
                         msg.text  || '');
        break;

      case 'message':
        // Каждая команда приносит свой Python-код — наращиваем textarea
        // через умный merge (без дублирования преамбулы и def-блоков).
        if (msg.code) {
          appendPythonCode(msg.description || 'Python-код команды', msg.code);
        }
        logMsg(msg.text, msg.level || 'info');
        updateVoiceStatus(msg.text);
        break;

      case 'program':
        // Полная замена текста программы. Используется при подключении WS,
        // после очистки и после run_textarea_program — чтобы синхронизировать
        // textarea с self._program (восстановление накопленного Python).
        if (typeof msg.text === 'string') {
          const textarea = document.getElementById('python-code');
          const desc     = document.getElementById('python-code-description');
          if (desc) desc.textContent = 'Программа робота (редактируется)';
          // Если в localStorage есть черновик пользовательских правок,
          // которые ещё не были применены (не нажимал ▶) — он переживает
          // reload страницы и подставляется поверх серверного состояния.
          // ВО ВРЕМЯ АКТИВНОЙ МИССИИ draft игнорируется: голосовые команды
          // в режиме миссии добавляются прямо в _program на сервере, и
          // каждый push 'program' несёт самый свежий код — заменяем им
          // textarea, иначе устаревший draft проглатывает новые строки.
          const draft = _readCodeDraft();
          const serverText = msg.text;
          const missionActive = !!window._currentMission;
          const useDraft = !missionActive
                           && draft && draft !== serverText
                           && draft.trim() !== '';
          const finalText = useDraft ? draft : serverText;
          if (textarea) {
            textarea.value = finalText;
            if (useDraft) {
              logMsg('↻ Восстановлены несохранённые правки кода '
                   + '(▶ применит их, ✕ — отменит).', 'info');
            } else if (missionActive) {
              // Подчищаем draft, чтобы он не «всплыл» позже как устаревший
              // и не подменил очередной серверный апдейт.
              _clearCodeDraft();
            }
          }
          // Если модалка открыта — тоже обновляем
          const modal     = document.getElementById('python-code-modal');
          const modalArea = document.getElementById('python-code-modal-area');
          if (modal && !modal.hidden && modalArea) modalArea.value = finalText;
        }
        break;

      case 'code_exec_result':
        logMsg(msg.output || 'Выполнено.', 'info');
        break;


      // ── Миссии ────────────────────────────────────────────────────────
      case 'mission_active':
        // Активирована миссия: overlay на холсте, кнопка «Задание» и
        // таймер в шапке журнала, журнал очищается (Python-код приходит
        // отдельным сообщением 'program' от сервера после _save_program).
        if (canvas && msg.mission) {
          canvas.setMission(msg.mission);
        }
        if (msg.mission) {
          window._currentMission = msg.mission;
          _clearLogPanel();
          // На старте миссии чистим draft-черновик в localStorage: он
          // мог остаться от свободного режима и при следующем 'program'
          // подменил бы серверный код миссии (mark_danger + добавленные
          // голосом команды) на устаревший текст.
          _clearCodeDraft();
          showMissionButton(msg.mission);
          _startMissionTimer();
          // Уровень ≥ 2 (опасные зоны) — режим зафиксирован «осторожно»
          // на всё время миссии. Сервер тоже не даст переключиться.
          const hasDanger = Array.isArray(msg.mission.danger_zones)
                            && msg.mission.danger_zones.length > 0;
          _setModeButtonsLocked(hasDanger,
            'Режим зафиксирован миссией с опасными зонами. '
            + 'Завершите или остановите миссию, чтобы переключиться.');
          // Запрещаем «📍 В точку…» — обучающийся составляет маршрут
          // программно, а не телепортирует робота одной кнопкой.
          _setMissionShortcutsLocked(true);
        }
        break;

      case 'mission_finished':
        // Завершена миссия — таймер стоп, модалка результата.
        // Карточку задания НЕ удаляем — пользователь хочет видеть
        // условие, чтобы проанализировать что не сошлось. Деактивируем
        // кнопки в ней (миссия уже не активна, нажимать бессмысленно).
        _stopMissionTimer();
        showMissionResultModal(msg);
        if (canvas) canvas.setMission(null);
        window._currentMission = null;
        hideMissionButton();
        _setModeButtonsLocked(false);
        _setMissionShortcutsLocked(false);
        document.querySelectorAll('#cmd-log .log-task-card').forEach(card => {
          card.classList.add('log-task-card--finalized');
          card.querySelectorAll('button').forEach(b => { b.disabled = true; });
          // Бейдж с вердиктом в заголовок карточки.
          const title = card.querySelector('.log-task-card__title');
          if (title && !title.querySelector('.task-verdict')) {
            const v = document.createElement('span');
            v.className = 'task-verdict ' +
              (msg.success ? 'task-verdict--ok' : 'task-verdict--fail');
            v.textContent = msg.success ? '✓ выполнено' : '✗ не выполнено';
            title.appendChild(v);
          }
        });
        break;

      case 'mission_inactive':
        // Серверный сигнал «миссии нет» — обычно при подключении WS
        // после рестарта сервера. Сбрасываем клиентское состояние,
        // чтобы UI не остался в «думает что миссия идёт».
        if (window._currentMission) {
          _resetMissionClientState();
        }
        break;

      case 'pong':
        break;
    }
    // state-сообщения могут содержать mission_progress — обновляем чип.
    if (msg.type === 'state' && msg.mission_progress) {
      updateMissionProgress(msg.mission_progress);
    }
  }

  // ── Python код команды ─────────────────────────────────────────────────────

  function copyPythonCode() {
    const textarea = document.getElementById('python-code');
    if (!textarea) return;
    navigator.clipboard.writeText(textarea.value).catch(() => {});
  }

  // ── Модалка-предупреждение (сервер блокирует команду) ────────────────────
  function showWarningModal(title, text) {
    let modal = document.getElementById('warning-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'warning-modal';
      modal.className = 'warn-modal';
      modal.innerHTML = `
        <div class="warn-modal__panel">
          <div class="warn-modal__header">
            <span class="warn-modal__icon">⚠</span>
            <h3 class="warn-modal__title"></h3>
          </div>
          <div class="warn-modal__body"></div>
          <div class="warn-modal__footer">
            <button type="button" class="btn btn--primary warn-modal__close">Закрыть</button>
          </div>
        </div>`;
      document.body.appendChild(modal);
      const close = () => { modal.hidden = true; };
      modal.querySelector('.warn-modal__close').addEventListener('click', close);
      modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !modal.hidden) close();
      });
    }
    modal.querySelector('.warn-modal__title').textContent = title;
    modal.querySelector('.warn-modal__body').textContent  = text;
    modal.hidden = false;
    modal.querySelector('.warn-modal__close').focus();
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Миссии: статичная кнопка «📋 Задание» в шапке журнала, таймер,
  // описание, результат, прогресс
  // ─────────────────────────────────────────────────────────────────────────

  // Состояние таймера активной миссии.
  let _missionTimerId    = null;
  let _missionStartedAt  = 0;

  function _fmtElapsed(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const mm = String(Math.floor(total / 60)).padStart(2, '0');
    const ss = String(total % 60).padStart(2, '0');
    return `⏱ ${mm}:${ss}`;
  }
  function _startMissionTimer() {
    const el = document.getElementById('mission-timer');
    if (!el) return;
    _missionStartedAt = Date.now();
    el.hidden = false;
    el.textContent = _fmtElapsed(0);
    if (_missionTimerId) clearInterval(_missionTimerId);
    _missionTimerId = setInterval(() => {
      el.textContent = _fmtElapsed(Date.now() - _missionStartedAt);
    }, 1000);
  }
  function _stopMissionTimer() {
    if (_missionTimerId) { clearInterval(_missionTimerId); _missionTimerId = null; }
    const el = document.getElementById('mission-timer');
    if (el) el.hidden = true;
  }

  function showMissionButton(mission) {
    // Обновляет tooltip кнопки «📋 Задание» под активную миссию. Сама
    // кнопка всегда видна — клик вставляет карточку задания в журнал
    // (либо описание миссии, либо подсказку «миссия не загружена»).
    const btn = document.getElementById('btn-show-mission');
    if (btn) {
      const title = mission ? (mission.title || `Миссия #${mission.mission_id}`) : null;
      btn.title = title ? `Задание: ${title}` : 'Показать задание (миссия не загружена)';
    }
  }

  function hideMissionButton() {
    // Кнопка «📋» остаётся видимой — просто сбрасываем tooltip.
    const btn = document.getElementById('btn-show-mission');
    if (btn) btn.title = 'Показать задание (миссия не загружена)';
  }

  async function _requestMissionHint() {
    try {
      const r = await fetch('/missions/active/hint', {method: 'POST'});
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        if (err.error === 'no active mission' ||
            err.error === 'no active session') {
          _resetMissionClientState();
          logMsg('⚠ Миссия не активна на сервере (перезагрузка?). '
               + 'Откройте каталог и нажмите ▶ Пройти заново.', 'warning');
        } else {
          logMsg(`Подсказка недоступна: ${err.error || r.status}`, 'error');
        }
      }
      // Серверный push_message сам положит подсказку в журнал.
    } catch (e) {
      logMsg(`Ошибка подсказки: ${e.message}`, 'error');
    }
  }

  function _resetMissionClientState() {
    // Полный сброс клиентского состояния миссии — используется, когда
    // обнаруживаем рассинхрон (например, сервер перезагрузился и потерял
    // активную миссию). Чтобы UI не оставался в «думает что миссия идёт».
    _stopMissionTimer();
    if (canvas && typeof canvas.setMission === 'function') {
      canvas.setMission(null);
    }
    window._currentMission = null;
    hideMissionButton();
    _setModeButtonsLocked(false);
    _setMissionShortcutsLocked(false);
    document.querySelectorAll('#cmd-log .log-task-card')
            .forEach(el => el.remove());
  }

  function _setModeButtonsLocked(locked, reason) {
    // Блокирует кнопки «Инспектор» / «⚠ Осторожно» во время миссии
    // с опасными зонами: переключаться нельзя до stop/finalize. Сервер
    // тоже отвергает такой intent — это просто UX-индикация.
    const btnInsp = document.getElementById('btn-mode-inspector');
    const btnCaut = document.getElementById('btn-mode-cautious');
    [btnInsp, btnCaut].forEach(b => {
      if (!b) return;
      b.disabled = !!locked;
      b.classList.toggle('is-locked', !!locked);
      if (locked) {
        if (!b.dataset._titleOrig) b.dataset._titleOrig = b.title || '';
        b.title = reason || 'Режим зафиксирован миссией';
      } else if (b.dataset._titleOrig !== undefined) {
        b.title = b.dataset._titleOrig;
        delete b.dataset._titleOrig;
      }
    });
  }

  function _setMissionShortcutsLocked(locked) {
    // Блокирует команды-«читы», обходящие программирование во время миссии:
    // «📍 В точку…» (телепорт-подобный goto). Сервер тоже отвергает,
    // но визуальная индикация важна — иначе пользователь думает, что
    // кнопка просто не работает.
    const btn = document.getElementById('btn-goto');
    if (!btn) return;
    btn.disabled = !!locked;
    btn.classList.toggle('is-locked', !!locked);
    if (locked) {
      if (!btn.dataset._titleOrig) btn.dataset._titleOrig = btn.title || '';
      btn.title = 'Во время миссии команду «в точку» нельзя — '
                + 'составьте маршрут из forward/поворотов в коде.';
    } else if (btn.dataset._titleOrig !== undefined) {
      btn.title = btn.dataset._titleOrig;
      delete btn.dataset._titleOrig;
    }
  }

  function _clearLogPanel() {
    const log = document.getElementById('cmd-log');
    if (log) log.innerHTML = '';
  }

  // Карточка, которая по клику на «📋 Задание» вставляется в журнал —
  // либо описание текущей миссии (+ кнопка «⏹ Завершить»), либо подсказка
  // как загрузить миссию из каталога.
  function _appendTaskCard() {
    const log = document.getElementById('cmd-log');
    if (!log) return;
    // Если карточка уже есть (например осталась после finalize — мы
    // её сохраняем как «архив задания»), просто прокрутим к ней,
    // не плодя дубликаты и не затирая описание подсказкой «нет миссии».
    const existing = log.querySelector('.log-task-card');
    if (existing) {
      existing.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      return;
    }
    const card = document.createElement('div');
    card.className = 'log-task-card';
    const m = window._currentMission;
    if (m) {
      const title = m.title || `Миссия #${m.mission_id}`;
      card.innerHTML =
        `<div class="log-task-card__title">📋 ${escHtml(title)}</div>` +
        `<pre class="log-task-card__body">${escHtml(m.description || '')}</pre>` +
        `<div class="log-task-card__actions">` +
        `  <button type="button" class="btn btn--xs js-mission-hint"` +
        `          title="Показать угол и расстояние до ближайшей непосещённой точки">` +
        `    💡 Подсказка</button>` +
        `  <button type="button" class="btn btn--xs btn--success js-mission-finalize"` +
        `          title="Зафиксировать результат: оценка по последнему прогону + суммарное время алгоритма">` +
        `    🏁 Проверка задания</button>` +
        `  <button type="button" class="btn btn--xs js-mission-stop"` +
        `          style="background:#3d1a1a;color:var(--danger);border-color:rgba(248,81,73,0.35)"` +
        `          title="Отказаться от миссии — 0 звёзд">` +
        `    ⏹ Стоп миссия</button>` +
        `</div>`;
      card.querySelector('.js-mission-hint').addEventListener('click', _requestMissionHint);
      card.querySelector('.js-mission-finalize').addEventListener('click', async () => {
        if (!confirm('Зафиксировать результат миссии?\n\n'
            + 'Звёзды считаются по ПОСЛЕДНЕМУ прогону программы.\n'
            + 'После этого миссию можно будет запустить заново через каталог.')) return;
        await fetch('/missions/active/finalize', {method: 'POST'});
      });
      card.querySelector('.js-mission-stop').addEventListener('click', async () => {
        if (!confirm('Отказаться от миссии? Звёзд не будет.')) return;
        await fetch('/missions/active/stop', {method: 'POST'});
      });
    } else {
      card.innerHTML =
        '<div class="log-task-card__title">🎯 Миссия не загружена</div>' +
        '<div class="log-task-card__body">' +
          'Откройте <a href="/tasks#catalog">📋 Миссии → Каталог</a> и нажмите ' +
          '<strong>▶ Пройти</strong> на любой миссии, чтобы загрузить её в это окно.' +
        '</div>';
    }
    log.appendChild(card);
    log.scrollTop = log.scrollHeight;
  }

  function updateMissionProgress(p) {
    // Передаём прогресс на canvas — там есть встроенная плашка
    // «⭐ N · точки X/Y · действия A/B · коэф %» с подсветкой отклонений.
    if (canvas && typeof canvas.updateMissionProgress === 'function') {
      canvas.updateMissionProgress(p);
    }
  }


  function showMissionResultModal(msg) {
    let modal = document.getElementById('mission-result-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'mission-result-modal';
      modal.className = 'warn-modal';
      modal.innerHTML = `
        <div class="warn-modal__panel">
          <div class="warn-modal__header">
            <span class="warn-modal__icon mission-result-icon"></span>
            <h3 class="warn-modal__title"></h3>
          </div>
          <div class="warn-modal__body mission-result-body"></div>
          <div class="warn-modal__footer">
            <button type="button" class="btn btn--primary warn-modal__close">OK</button>
          </div>
        </div>`;
      document.body.appendChild(modal);
      const close = () => { modal.hidden = true; };
      modal.querySelector('.warn-modal__close').addEventListener('click', close);
      // Закрытие по клику на подложку. Используем mousedown (а не click) и
      // отдельно фиксируем, что нажатие СТАРТОВАЛО на подложке. Иначе при
      // выделении текста результата для копирования (Ctrl+C) mouseup
      // может оказаться на подложке и click закроет модалку.
      let _downOnBackdrop = false;
      modal.addEventListener('mousedown', (e) => {
        _downOnBackdrop = (e.target === modal);
      });
      modal.addEventListener('mouseup', (e) => {
        if (_downOnBackdrop && e.target === modal) close();
        _downOnBackdrop = false;
      });
    }
    const ok = !!msg.success;
    modal.querySelector('.mission-result-icon').textContent = ok ? '⭐' : '⚠';
    // Заголовок: предпочитаем пользовательский title (он уже несёт #ID
    // в fallback-варианте «Миссия #N»). Если title пустой — fallback
    // на номер миссии. Слово «Задание» вместо «Миссия» — чтобы избежать
    // двойного «Миссия «Миссия #N»».
    const which = msg.title ? `«${msg.title}»`
                : (msg.mission_id ? `#${msg.mission_id}` : '');
    const verdict = ok ? 'выполнено!' : 'не завершено';
    modal.querySelector('.warn-modal__title').textContent =
      which ? `Задание ${which} ${verdict}` : `Задание ${verdict}`;
    const stars = msg.stars || 0;
    const starsStr = '⭐'.repeat(Math.max(0, Math.min(10, stars)));
    const factStars  = (msg.stars_fact  != null) ? msg.stars_fact  : null;
    const trackStars = (msg.stars_track != null) ? msg.stars_track : null;
    const timeStars  = (msg.stars_time  != null) ? msg.stars_time  : null;
    const precision  = (msg.precision_pct != null)
                       ? msg.precision_pct
                       : Math.round((msg.coefficient || 0) * 100);
    const duration   = (msg.duration_sec != null) ? msg.duration_sec : null;
    const algoDur    = (msg.algo_duration_sec != null) ? msg.algo_duration_sec : null;
    const _fmt = sec => `${String(Math.floor(sec/60)).padStart(2,'0')}:${String(Math.floor(sec%60)).padStart(2,'0')}`;
    const timeStr = duration != null ? _fmt(duration) : null;
    const algoStr = (algoDur != null && algoDur > 0) ? _fmt(algoDur) : null;
    let breakdown = `<div>Звёзд: <strong>${stars}</strong></div>`;
    if (factStars != null && trackStars != null) {
      breakdown =
        `<div>⭐ Всего: <strong>${stars}</strong></div>` +
        `<div style="color:var(--text-dim); font-size:0.85rem; margin-top:0.3rem">` +
        `  За точки и действия: <strong>${factStars}</strong><br>` +
        `  За точность траектории: <strong>${trackStars}</strong>` +
        (timeStars != null ? `<br>  За скорость прохождения: <strong>${timeStars}</strong>` : '') +
        `</div>`;
    }
    modal.querySelector('.mission-result-body').innerHTML =
      `<div style="text-align:center; font-size:1.4rem; margin-bottom:0.6rem">${starsStr || '—'}</div>` +
      breakdown +
      `<div style="margin-top:0.5rem">Точность траектории: <strong>${precision}%</strong></div>` +
      (timeStr ? `<div>Время задания: <strong>${timeStr}</strong></div>` : '') +
      (algoStr ? `<div>Время алгоритма: <strong>${algoStr}</strong></div>` : '') +
      `<div>Отклонений: <strong>${msg.deviations || 0}</strong></div>`;
    modal.hidden = false;
  }

  // ── Подсветка Python-кода (комментарии — зеленым) ─────────────────────────
  function escHtmlCode(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function highlightPython(text) {
    // Двухпроходный лексер. Сначала находим диапазоны комментариев:
    //   1) тройные строки """ … """ и ''' … ''' (могут быть многострочные);
    //   2) хвост строки от '#' вне обычных строк.
    // Потом одним проходом склеиваем HTML, оборачивая эти диапазоны
    // в <span class="hl-comment">.
    const ranges = [];   // {start, end} — глобальные смещения в text
    const N = text.length;
    let i = 0, inStr = false, strCh = '';
    while (i < N) {
      const ch = text[i];
      // 1) Тройные кавычки — независимо от режима, считаем их комментарием
      //    (это докстринги функций, лучше красить как комментарий)
      if (!inStr && (text.startsWith('"""', i) || text.startsWith("'''", i))) {
        const tri = text.substr(i, 3);
        const end = text.indexOf(tri, i + 3);
        const stop = end >= 0 ? end + 3 : N;
        ranges.push({start: i, end: stop});
        i = stop;
        continue;
      }
      if (inStr) {
        if (ch === '\\') { i += 2; continue; }
        if (ch === strCh) { inStr = false; }
        if (ch === '\n') { inStr = false; }   // обычная строка не переносится
        i++;
        continue;
      }
      if (ch === '"' || ch === "'") {
        inStr = true; strCh = ch; i++;
        continue;
      }
      if (ch === '#') {
        const nl = text.indexOf('\n', i);
        const stop = nl >= 0 ? nl : N;
        ranges.push({start: i, end: stop});
        i = stop;
        continue;
      }
      i++;
    }
    if (ranges.length === 0) return escHtmlCode(text) + '\n';
    // Сшиваем итоговый HTML
    let out = '', pos = 0;
    for (const r of ranges) {
      if (pos < r.start) out += escHtmlCode(text.slice(pos, r.start));
      out += '<span class="hl-comment">' +
             escHtmlCode(text.slice(r.start, r.end)) +
             '</span>';
      pos = r.end;
    }
    if (pos < N) out += escHtmlCode(text.slice(pos));
    return out + '\n';   // финальный \n чтобы overlay не «съедал» нижнюю строку
  }
  // ── Черновик кода (localStorage) ─────────────────────────────────────────
  // Когда пользователь правит textarea, но ещё не нажал ▶ — сервер про эти
  // правки не знает. Если страница перезагружается, сервер шлёт `program`
  // с серверным состоянием и стирает правки. Чтобы это не происходило,
  // на каждое изменение textarea сохраняем её в localStorage; при reload
  // (обработчик 'program') читаем оттуда и подставляем поверх серверного.
  // Удаляется при ▶ (правки применены) и при ✕ (явный сброс).
  const _CODE_DRAFT_KEY = 'vegarex.code_draft';
  function _readCodeDraft() {
    try { return localStorage.getItem(_CODE_DRAFT_KEY) || ''; }
    catch (_) { return ''; }
  }
  function _saveCodeDraft(text) {
    try {
      if (text && text.trim()) localStorage.setItem(_CODE_DRAFT_KEY, text);
      else                     localStorage.removeItem(_CODE_DRAFT_KEY);
    } catch (_) {}
  }
  function _clearCodeDraft() {
    try { localStorage.removeItem(_CODE_DRAFT_KEY); }
    catch (_) {}
  }

  function attachCodeHighlight(textareaId, overlayId) {
    const ta = document.getElementById(textareaId);
    const ov = document.getElementById(overlayId);
    if (!ta || !ov) return;
    // overlay <pre> с overflow:hidden — content движем через CSS transform
    // на внутреннем <code>. Так не получится «второго скроллбара» и
    // программный сдвиг работает в любом браузере.
    const syncScroll = () => {
      const code = ov.querySelector('code') || ov;
      code.style.transform =
        `translate(${-ta.scrollLeft}px, ${-ta.scrollTop}px)`;
    };
    const syncContent = () => {
      const code = ov.querySelector('code') || ov;
      code.innerHTML = highlightPython(ta.value || '');
      syncScroll();
    };
    // Каждое пользовательское изменение текстареи — backup в localStorage,
    // чтобы reload не стёр правки. Применяется к обоим редакторам
    // (боковая панель + модальное окно «во весь экран»).
    const saveDraft = () => _saveCodeDraft(ta.value || '');
    ta.addEventListener('input',  syncContent);
    ta.addEventListener('input',  saveDraft);
    ta.addEventListener('scroll', syncScroll);
    // Стрелки/PageDown/PageUp могут двигать каретку без срабатывания scroll —
    // на них тоже досинхронизируем сразу.
    ta.addEventListener('keyup', syncScroll);
    ta.addEventListener('click', syncScroll);
    // Первоначальная подсветка
    syncContent();
    // Программные изменения value (например, server append) не дают input —
    // дублирующий polling 200мс.
    let lastVal = ta.value;
    setInterval(() => {
      if (ta.value !== lastVal) {
        lastVal = ta.value;
        syncContent();
      }
    }, 200);
  }

  // ── UI обновление ───────────────────────────────────────────────────────────

  function updateRobotBadge(online) {
    const el = document.getElementById('robot-badge');
    if (!el) return;
    el.textContent = online ? '● Онлайн' : '● Офлайн';
    el.className   = 'badge ' + (online ? 'badge--online' : 'badge--offline');
  }

  function updateStatePanel(s) {
    set('st-x',       s.x.toFixed(1));
    set('st-y',       s.y.toFixed(1));
    set('st-heading', s.heading.toFixed(0));
    // Показываем фактическую скорость с учетом просадки от заряда батареи.
    // s.effective_speed_pct = s.speed * battery_factor (0..1).
    const eff = (typeof s.effective_speed_pct === 'number')
                  ? s.effective_speed_pct
                  : s.speed;
    const stSpeed = document.getElementById('st-speed');
    if (stSpeed) {
      stSpeed.textContent = eff.toFixed(0);
      // Подсветим ярче, если просадка заметная
      const factor = (typeof s.battery_factor === 'number') ? s.battery_factor : 1.0;
      stSpeed.title = (factor < 0.999 && s.speed !== 0)
        ? `Подано ${s.speed.toFixed(0)}%, фактически ${eff.toFixed(0)}% (фактор заряда ${factor.toFixed(2)})`
        : `Скорость робота, %`;
    }
    set('st-steer',   s.steer != null ? (s.steer > 0 ? '+' : '') + s.steer.toFixed(0) : '0');
    set('st-dist',    s.dist_left > 0 ? s.dist_left.toFixed(0) : '—');
    set('st-laser',   s.laser_dist > 0 ? s.laser_dist.toFixed(0) : '—');

    // Один источник правды: «осторожно» имеет приоритет над модальным режимом.
    const cautious = !!s.cautious;
    const modeText = cautious ? 'осторожно' : modeLabel(s.mode);
    const stMode = document.getElementById('st-mode');
    if (stMode) {
      stMode.textContent = modeText;
      stMode.classList.toggle('state-value--cautious', cautious);
    }

    // Зарядка: шкала с цветом как у реальных индикаторов
    //   ≥ 60% — зеленый, 30–60% — желто-зеленый, 15–30% — оранжевый, < 15% — красный
    const battery = (typeof s.battery === 'number') ? s.battery : 100;
    const stBatt  = document.getElementById('st-battery');
    const stFill  = document.getElementById('st-battery-fill');
    if (stBatt) stBatt.textContent = battery.toFixed(0) + '%';
    if (stFill) {
      stFill.style.width = Math.max(0, Math.min(100, battery)) + '%';
      stFill.classList.remove(
        'battery-gauge__fill--mid',
        'battery-gauge__fill--low',
        'battery-gauge__fill--crit',
      );
      if      (battery < 15) stFill.classList.add('battery-gauge__fill--crit');
      else if (battery < 30) stFill.classList.add('battery-gauge__fill--low');
      else if (battery < 60) stFill.classList.add('battery-gauge__fill--mid');
    }

    // Переключаем mode-классы ТОЛЬКО при реальной смене режима — иначе
    // при каждом push_state (а они идут ~10/с во время движения) classList
    // дёргается, и transition в .btn вызывает визуальное мигание кнопок
    // «Инспектор» / «Осторожно». Кэшируем последнее значение на window.
    if (window._lastCautious !== cautious) {
      window._lastCautious = cautious;
      const btnInsp = document.getElementById('btn-mode-inspector');
      const btnCaut = document.getElementById('btn-mode-cautious');
      if (btnInsp) btnInsp.classList.toggle('is-active', !cautious);
      if (btnCaut) btnCaut.classList.toggle('is-active',  cautious);
      const appbar = document.querySelector('.appbar');
      if (appbar) appbar.classList.toggle('appbar--cautious', cautious);
      // body.mode-cautious — глобальный сигнал «всё работает в осторожно»
      // для action-кнопок (.btn--mode-action перекрашиваются под warn).
      document.body.classList.toggle('mode-cautious', cautious);
    }

    // Дублирующий бейдж больше не нужен — режим уже виден в строке «Режим».
    const caut = document.getElementById('caution-badge');
    if (caut) caut.style.display = 'none';

  }

  function modeLabel(m) {
    return { normal: 'инспектор', marker: 'маркировщик' }[m] || m;
  }

  function set(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  // ── Лог ────────────────────────────────────────────────────────────────────

  function logMsg(text, level = 'info') {
    const log = document.getElementById('cmd-log');
    if (log) {
      const now  = new Date();
      const time = `${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}:${now.getSeconds().toString().padStart(2,'0')}`;
      const entry = document.createElement('div');
      entry.className = `log-entry log-entry--${level}`;
      entry.innerHTML = `<span class="log-time">${time}</span>${escHtml(text)}`;
      log.appendChild(entry);
      log.scrollTop = log.scrollHeight;
      if (log.children.length > 200) log.children[0].remove();
    }
    // Mirror в подвал. Hint-сообщения (режим зон) приоритетнее — их
    // снимает только переключение режима, не следующий лог.
    setStatus(text, level);
  }

  // ── Статус-подвал ────────────────────────────────────────────────────────
  // hint-режим (зоны) — sticky, держится пока его не снимут явно через
  // setStatus(null, 'hint:clear'). Прочие уровни перетирают друг друга.
  let _statusHintActive = false;
  function setStatus(text, level = 'info') {
    const bar = document.getElementById('status-bar');
    if (!bar) return;
    if (level === 'hint') {
      _statusHintActive = true;
      bar.textContent = text;
      bar.className = 'appbar__status appbar__status--hint';
      return;
    }
    if (level === 'hint:clear') {
      _statusHintActive = false;
      bar.textContent = bar.dataset.default || '';
      bar.className = 'appbar__status';
      return;
    }
    if (_statusHintActive) return;   // не перетираем sticky-подсказку
    bar.textContent = text;
    bar.className = `appbar__status appbar__status--${level}`;
  }

  function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ── Зоны опасности ──────────────────────────────────────────────────────────

  function updateZoneList(zones) {
    const list = document.getElementById('zone-list');
    if (!list) return;
    list.innerHTML = '';
    if (!zones.length) {
      list.innerHTML = '<span style="color:var(--text-dim);font-size:.75rem">Нет зон</span>';
      return;
    }
    for (const z of zones) {
      const item = document.createElement('div');
      item.className = 'zone-item';
      item.innerHTML = `
        <span class="zone-item__dot">⚠</span>
        <span class="zone-item__label">${escHtml(z.label)}</span>
        <span class="zone-item__coords">(${z.x.toFixed(0)},${z.y.toFixed(0)})</span>
        <button class="zone-item__del" data-id="${z.db_id}" title="Удалить">✕</button>
      `;
      list.appendChild(item);
    }
  }

  function updateVoiceStatus(text) {
    const el = document.getElementById('voice-status');
    if (el) el.textContent = text;
  }

  // ── Программа ──────────────────────────────────────────────────────────────

  const PREAMBLE_SENTINEL = '# === НАЧАЛО ПРОГРАММЫ ===';

  function extractCodeBody(code) {
    // Returns just the command-specific lines, stripping the preamble
    const idx = code.indexOf(PREAMBLE_SENTINEL);
    if (idx !== -1) return code.slice(idx + PREAMBLE_SENTINEL.length).trimStart();
    return code.trim(); // fallback: no sentinel found
  }

  function appendPythonCode(description, code) {
    const textarea = document.getElementById('python-code');
    const desc = document.getElementById('python-code-description');
    if (!textarea) return;
    if (desc) desc.textContent = description || 'Python код команды';

    const existing = textarea.value || '';
    const hasDefault = existing.trim().startsWith('# После выполнения команды');

    if (!existing.trim() || hasDefault) {
      textarea.value = code; // первая команда — полный текст (константы + сентинель + helpers + вызов)
      return;
    }

    // Последующие команды: берём только тело после сентинеля.
    // Дедуп helper-функций — по имени `def NAME(` или `class NAME:`.
    // Helper-блок включает ОДНУ предшествующую строку-комментарий
    // («русский заголовок» функции) и тянется до следующей пустой строки
    // на нулевом отступе (или до следующего col-0 def/class).
    const body = extractCodeBody(code);
    if (!body.trim()) return;

    // 1) Имена функций/классов, уже существующих в textarea.
    const existingDefs = new Set(
      Array.from(existing.matchAll(/^(?:def|class)\s+(\w+)\s*[\(:]/gm)).map(m => m[1])
    );

    const bodyLines = body.split('\n');
    let merged = existing.trimEnd();

    // 2) Идём по строкам тела, собирая блоки.
    //    «Сырые» строки (комментарии, пустые) кладём в lookahead — это
    //    может быть либо отдельная строка, либо «шапка» следующего def.
    let buffer = [];          // накопленные строки до классификации
    let i = 0;

    function flushBufferAsPlain() {
      // Все накопленные строки — обычные (вне helper-блока).
      for (const ln of buffer) {
        if (ln.trim()) merged += '\n' + ln;
      }
      buffer = [];
    }

    while (i < bodyLines.length) {
      const line = bodyLines[i];
      const defMatch = line.match(/^(?:def|class)\s+(\w+)\s*[\(:]/);

      if (defMatch) {
        const name = defMatch[1];
        // Шапка-комментарий — последняя строка в buffer, если она
        // начинается с `#` и предыдущий элемент пустой (или начало).
        const headerLines = [];
        if (buffer.length && buffer[buffer.length - 1].trimStart().startsWith('#')) {
          headerLines.push(buffer.pop());
        }
        flushBufferAsPlain();   // всё кроме «шапки» отдаём как plain

        // Считаем тело функции: пока следующая строка с отступом
        // или это пустая строка ВНУТРИ блока (перед которой ещё есть
        // отступная строка); останавливаемся на col-0 непустой строке.
        const blockLines = [...headerLines, line];
        i++;
        while (i < bodyLines.length) {
          const next = bodyLines[i];
          if (next === '' || /^\s+/.test(next)) {
            // Пустая или с отступом — может быть частью тела.
            // Пустая — кандидат на конец, посмотрим следующую.
            if (next === '') {
              // Если следующая после пустой — col-0 непустая (новая верхняя
              // конструкция), пустую в блок не включаем, выходим.
              const peek = bodyLines[i + 1];
              if (peek === undefined || (peek !== '' && !/^\s+/.test(peek))) {
                break;
              }
              blockLines.push(next);
              i++;
            } else {
              blockLines.push(next);
              i++;
            }
          } else {
            // Особый случай: одиночная строка вида `name = ClassName(...)`
            // (синглтон-инстанс типа `odo = Odometry()`) — считаем продолжением.
            if (/^[a-z_]\w*\s*=\s*[A-Z]\w*\(/.test(next)) {
              blockLines.push(next);
              i++;
            } else {
              break;
            }
          }
        }
        // Дедуп по имени функции/класса
        if (!existingDefs.has(name)) {
          merged += '\n\n' + blockLines.join('\n').replace(/\n+$/, '');
          existingDefs.add(name);
        }
      } else {
        buffer.push(line);
        i++;
      }
    }
    // Хвостовые plain-строки
    flushBufferAsPlain();

    textarea.value = merged;
    const modal     = document.getElementById('python-code-modal');
    const modalArea = document.getElementById('python-code-modal-area');
    if (modal && !modal.hidden && modalArea) modalArea.value = merged;
  }

  async function runPythonCode() {
    // Если активна миссия — нажатие ▶ означает «проверь моё прохождение»,
    // а не «запусти код заново».
    if (window._currentMission) {
      logMsg('🧪 Проверка миссии…', 'info');
      const textareaEl = document.getElementById('python-code');
      const codeText = textareaEl ? textareaEl.value : '';
      try {
        const r = await fetch('/missions/active/check', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({code: codeText}),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          // Если сервер говорит «нет активной миссии» — клиент держит
          // устаревшее состояние (обычно после рестарта сервера).
          // Сбрасываем UI и подсказываем перезапустить миссию.
          if (err.error === 'no active mission' ||
              err.error === 'no active session') {
            _resetMissionClientState();
            logMsg('⚠ Миссия не активна на сервере (перезагрузка?). '
                 + 'Откройте каталог и нажмите ▶ Пройти заново.', 'warning');
          } else {
            logMsg(`Ошибка проверки: ${err.error || r.status}`, 'error');
          }
        }
      } catch (e) {
        logMsg(`Ошибка проверки: ${e.message}`, 'error');
      }
      return;
    }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      logMsg('Нет соединения!', 'error');
      return;
    }
    const textarea = document.getElementById('python-code');
    if (!textarea) return;
    ws.send(JSON.stringify({ type: 'run_python_code', code: textarea.value }));
    // Черновик НЕ стираем: сервер при reload пришлёт пересборку из _program
    // (стандартный layout без пользовательских правок). Если черновик
    // стереть, F5 покажет server-pересборку и правки визуально «пропадут».
    // Стираем только при явном ✕ Очистить — там пользователь сам этого хочет.
    logMsg('▶ Выполняется Python-код…', 'info');
  }

  function clearPythonCode() {
    // Очистка идет через сервер — он перешлет обновленный текст программы (пустой + шапка).
    // Заодно стираем localStorage-черновик: без этого сервер пришлёт пустой
    // текст, а клиент подменит его старым черновиком — и команда «✕» ничего
    // не очистит.
    _clearCodeDraft();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'clear_program' }));
    } else {
      const textarea = document.getElementById('python-code');
      if (textarea) textarea.value = '';
    }
  }

  // ── 🧹 Упорядочить: константы → def-блоки (без дублей) → вызовы ──────────
  //
  // Парсит текстарею, разделяет на:
  //   • header   — всё до сентинеля «# === НАЧАЛО ПРОГРАММЫ ===»
  //                (константы, import-ы, комментарии)
  //   • def/class блоки в теле — собираются с шапкой-комментарием,
  //                индентом, поддержкой singleton-инициализации
  //                (`odo = Odometry()` за классом)
  //   • plain-строки — `# CMD: …`, `robot.X(...)`, `name_cmd(...)` и т.п.
  //
  // Итоговая раскладка:
  //   <header (константы + сентинель)>
  //   <все уникальные def/class блоки (в порядке первого появления)>
  //   # --- основной алгоритм ---
  //   <все plain-строки (вызовы) в исходном порядке>
  //
  // Дубли def/class по имени схлопываются — остаётся первое встретившееся.
  function tidyPythonCode(text) {
    if (!text) return text;
    const SENTINEL = '# === НАЧАЛО ПРОГРАММЫ ===';
    const lines = text.split('\n');

    // 1) Разделить header / body по сентинелю.
    let sentIdx = lines.findIndex(l => l.trim() === SENTINEL);
    let headerLines, bodyLines;
    if (sentIdx === -1) {
      headerLines = [];
      bodyLines = lines;
    } else {
      headerLines = lines.slice(0, sentIdx + 1);   // включая сентинель
      bodyLines = lines.slice(sentIdx + 1);
    }

    // 2) Прогон по телу: выделяем def/class блоки + plain-строки.
    const seenDefs = new Set();
    const defBlocks = [];   // [{name, lines: [...]}]
    const callLines = [];   // плоские строки (вызовы / комментарии)
    const buffer = [];      // накопленные строки до классификации

    function flushBufferToCalls() {
      for (const ln of buffer) callLines.push(ln);
      buffer.length = 0;
    }

    let i = 0;
    while (i < bodyLines.length) {
      const line = bodyLines[i];
      const defMatch = line.match(/^(?:def|class)\s+(\w+)\s*[(:]/);
      if (defMatch) {
        const name = defMatch[1];
        // Шапка-комментарий — последняя строка в buffer (если она `# …`).
        const headerComment = [];
        if (buffer.length && buffer[buffer.length - 1].trimStart().startsWith('#')) {
          headerComment.push(buffer.pop());
        }
        flushBufferToCalls();   // остаток buffer — обычные строки

        // Собираем тело def/class.
        const blockLines = [...headerComment, line];
        i++;
        while (i < bodyLines.length) {
          const next = bodyLines[i];
          if (next === '' || /^\s+/.test(next)) {
            if (next === '') {
              const peek = bodyLines[i + 1];
              // Пустая строка завершает блок, если следующая
              // непустая — col-0 неотступная.
              if (peek === undefined || (peek !== '' && !/^\s+/.test(peek)
                  && !/^[a-z_]\w*\s*=\s*[A-Z]\w*\(/.test(peek))) {
                break;
              }
            }
            blockLines.push(next);
            i++;
          } else {
            // Singleton-инициализация (`odo = Odometry()`) — продолжение блока.
            if (/^[a-z_]\w*\s*=\s*[A-Z]\w*\(/.test(next)) {
              blockLines.push(next);
              i++;
            } else {
              break;
            }
          }
        }
        // Дедуп по имени — оставляем первое появление.
        if (!seenDefs.has(name)) {
          seenDefs.add(name);
          defBlocks.push({name, lines: blockLines});
        }
      } else {
        buffer.push(line);
        i++;
      }
    }
    flushBufferToCalls();

    // 3) Сборка итогового текста.
    const out = [];

    // Header — почистим хвостовые пустые строки.
    while (headerLines.length && headerLines[headerLines.length - 1].trim() === '') {
      headerLines.pop();
    }
    out.push(...headerLines);
    out.push('');

    // Def/class блоки.
    for (const blk of defBlocks) {
      // Каждый блок — без хвостовых пустых строк, потом одна разделительная.
      const blkLines = [...blk.lines];
      while (blkLines.length && blkLines[blkLines.length - 1].trim() === '') {
        blkLines.pop();
      }
      out.push(...blkLines);
      out.push('');
    }

    // Заголовок «основной алгоритм» — только если ниже что-то есть.
    const trimmedCalls = [];
    for (const ln of callLines) {
      // Схлопнуть подряд идущие пустые.
      if (ln.trim() === '' && trimmedCalls.length
          && trimmedCalls[trimmedCalls.length - 1].trim() === '') {
        continue;
      }
      trimmedCalls.push(ln);
    }
    while (trimmedCalls.length && trimmedCalls[0].trim() === '') trimmedCalls.shift();
    while (trimmedCalls.length && trimmedCalls[trimmedCalls.length - 1].trim() === '') {
      trimmedCalls.pop();
    }
    if (trimmedCalls.length) {
      out.push('# --- основной алгоритм ---');
      out.push(...trimmedCalls);
    }

    return out.join('\n') + '\n';
  }

  function tidyCurrentTextarea() {
    const main = document.getElementById('python-code');
    if (!main) return;
    const tidied = tidyPythonCode(main.value);
    main.value = tidied;
    // Зеркалим в модалку, если она открыта.
    const modal = document.getElementById('python-code-modal');
    const modalArea = document.getElementById('python-code-modal-area');
    if (modal && !modal.hidden && modalArea) modalArea.value = tidied;
    // Триггерим input-событие, чтобы overlay-подсветка пересчиталась.
    main.dispatchEvent(new Event('input', {bubbles: true}));
    if (modalArea) modalArea.dispatchEvent(new Event('input', {bubbles: true}));
    logMsg('🧹 Код упорядочен: функции собраны вверху, вызовы внизу.', 'info');
  }

  // ── Голосовое управление (Web Speech API) ──────────────────────────────────

  let recognition = null;
  let listening    = false;

  function initVoice() {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    const btnVoice  = document.getElementById('btn-voice');
    if (!btnVoice) return;

    if (!SpeechRec || !window.isSecureContext) {
      btnVoice.textContent = '🎙 Нужен HTTPS';
      btnVoice.title       = 'Web Speech API работает только на localhost или HTTPS';
      btnVoice.disabled    = true;
      return;
    }

    recognition = new SpeechRec();
    recognition.lang        = 'ru-RU';
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.continuous  = false;

    recognition.onresult = e => {
      const text = e.results[0][0].transcript;
      updateVoiceStatus(`Услышано: "${text}"`);
      sendCmd(text);
    };

    recognition.onerror = e => {
      updateVoiceStatus(`Ошибка: ${e.error}`);
      stopListening();
    };

    recognition.onend = () => stopListening();

    btnVoice.addEventListener('click', toggleListening);
  }

  function toggleListening() {
    if (listening) stopListening();
    else startListening();
  }

  function startListening() {
    if (!recognition) return;
    listening = true;
    recognition.start();
    const btn = document.getElementById('btn-voice');
    if (btn) { btn.textContent = '🔴 Слушаю…'; btn.classList.add('listening'); }
    updateVoiceStatus('Говорите: «Вега» + команда…');
  }

  function stopListening() {
    if (!recognition) return;
    listening = false;
    try { recognition.stop(); } catch (_) {}
    const btn = document.getElementById('btn-voice');
    if (btn) { btn.textContent = '🎙 Слушать'; btn.classList.remove('listening'); }
    updateVoiceStatus('Ожидание…');
  }

  // ── Инициализация ─────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', () => {
    // Canvas (ошибка здесь не должна глушить WS и голос)
    try { canvas = initCanvas(); } catch (e) { console.error('Canvas init failed:', e); }

    // WebSocket
    wsConnect();

    // Auto-start миссии через ?mission=N в URL (приход с карточки
    // «▶ Пройти» в каталоге). Делаем POST после установки WS, чтобы
    // mission_active точно дошёл до клиента.
    const _missionId = new URLSearchParams(window.location.search).get('mission');
    if (_missionId) {
      // Удалим query param из URL чтобы при F5 не активировать заново.
      const cleanUrl = window.location.pathname + window.location.hash;
      window.history.replaceState({}, '', cleanUrl);
      // Небольшая задержка чтобы WS успел подключиться (auto-reconnect
      // в wsConnect занимает <100мс). Без неё mission_active не дойдёт.
      setTimeout(async () => {
        try {
          const r = await fetch('/missions/' + _missionId + '/start',
                                {method: 'POST'});
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            logMsg(`Ошибка запуска миссии #${_missionId}: ${err.error || r.status}`,
                   'error');
          }
        } catch (e) {
          logMsg(`Ошибка запуска миссии: ${e.message}`, 'error');
        }
      }, 300);
    }

    // Форма текстовых команд
    const form  = document.getElementById('cmd-form');
    const input = document.getElementById('cmd-input');
    if (form && input) {
      form.addEventListener('submit', e => {
        e.preventDefault();
        const text = input.value.trim();
        if (text) { sendCmd(text); input.value = ''; }
      });
    }

    // Быстрые кнопки
    document.querySelectorAll('[data-cmd]').forEach(btn => {
      btn.addEventListener('click', () => sendCmd(btn.dataset.cmd));
    });

    // Кнопки тулбара
    const btnCenter = document.getElementById('btn-center-view');
    if (btnCenter) btnCenter.addEventListener('click', () => canvas && canvas.centerView());

    const chkGrid = document.getElementById('chk-grid');
    if (chkGrid) chkGrid.addEventListener('change', e => {
      if (canvas) { canvas.showGrid = e.target.checked; canvas.draw(); }
    });

    const chkPath = document.getElementById('chk-path');
    if (chkPath) chkPath.addEventListener('change', e => {
      if (canvas) { canvas.showPath = e.target.checked; canvas.draw(); }
    });

    const chkLaser = document.getElementById('chk-laser');
    if (chkLaser) chkLaser.addEventListener('change', e => {
      const enabled = e.target.checked;
      if (canvas) { canvas.showLaser = enabled; canvas.draw(); }
      // Передаем серверу — чтобы физика тоже учитывала отключение датчика.
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'set_laser', enabled }));
      }
    });

    // Удаление зоны
    document.getElementById('zone-list')?.addEventListener('click', e => {
      const btn = e.target.closest('.zone-item__del');
      if (!btn) return;
      const id  = btn.dataset.id;
      if (id) {
        fetch(`/api/danger_zones/${id}`, { method: 'DELETE' });
      }
    });

    // Python код
    document.getElementById('btn-run-python-code')?.addEventListener('click', runPythonCode);
    document.getElementById('btn-clear-python-code')?.addEventListener('click', clearPythonCode);
    document.getElementById('btn-copy-python-code')?.addEventListener('click', copyPythonCode);
    document.getElementById('btn-tidy-python-code')?.addEventListener('click', tidyCurrentTextarea);

    // Подсветка комментариев в обоих редакторах кода
    attachCodeHighlight('python-code',            'python-code-overlay');
    attachCodeHighlight('python-code-modal-area', 'python-code-modal-overlay');

    // ── Модальное окно «Python код во весь экран» ─────────────────────
    const modal      = document.getElementById('python-code-modal');
    const modalArea  = document.getElementById('python-code-modal-area');
    const sideArea   = document.getElementById('python-code');

    function openCodeModal() {
      if (!modal || !modalArea || !sideArea) return;
      modalArea.value = sideArea.value;     // последняя версия из боковой
      modal.hidden = false;
      // Принудительно обновляем overlay-подсветку (textarea имеет
      // прозрачный текст — без overlay код виден не будет; polling 200мс
      // тут слишком медленный для пользователя).
      const modalOverlay = document.getElementById('python-code-modal-overlay');
      if (modalOverlay) {
        modalOverlay.innerHTML = highlightPython(modalArea.value || '');
        modalOverlay.style.transform = 'translate(0px, 0px)';
      }
      modalArea.focus();
    }
    function closeCodeModal() {
      if (!modal || !modalArea || !sideArea) return;
      sideArea.value = modalArea.value;     // правки из модалки → в боковую
      modal.hidden = true;
      // Так же синхронизируем overlay в боковой (если правили в модалке).
      const sideOverlay = document.getElementById('python-code-overlay');
      if (sideOverlay) {
        sideOverlay.innerHTML = highlightPython(sideArea.value || '');
        sideOverlay.style.transform = 'translate(0px, 0px)';
      }
    }
    document.getElementById('btn-expand-python-code')?.addEventListener('click', openCodeModal);
    document.getElementById('btn-collapse-python-code')?.addEventListener('click', closeCodeModal);
    // Esc — тоже закрывает
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && modal && !modal.hidden) closeCodeModal();
    });
    // Backspace вне textarea/input — браузер пытается «назад» в истории,
    // что закрывает модалку. Перехватываем, если фокус не на редактируемом
    // элементе.
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Backspace') return;
      if (!modal || modal.hidden) return;
      const t = e.target;
      const editable = t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.isContentEditable);
      if (!editable) e.preventDefault();
    });
    // Клик по фону модалки — закрывает (но не на сам редактор и не внутри панели).
    // Используем mousedown, а не click: иначе выделение текста с тащением мыши
    // из textarea на фон (mouseup на бэкдропе) синтезирует click с target=modal
    // и закрывает окно прямо посреди выделения. Через mousedown это работает
    // надёжно — клик-старт по фону = закрыть, клик-старт внутри textarea =
    // никаких эффектов на модалку, даже если палец доехал до фона.
    modal?.addEventListener('mousedown', (e) => {
      if (e.target === modal) closeCodeModal();
    });
    // Кнопки в модалке делегируют в основные обработчики, синхронизируя текст
    document.getElementById('btn-modal-copy')?.addEventListener('click', () => {
      navigator.clipboard.writeText(modalArea.value).catch(() => {});
    });
    document.getElementById('btn-modal-clear')?.addEventListener('click', () => {
      if (confirm('Очистить весь код?')) {
        modalArea.value = '';
        sideArea.value  = '';
        _clearCodeDraft();
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'clear_program' }));
        }
      }
    });
    document.getElementById('btn-modal-tidy')?.addEventListener('click', () => {
      // Упорядочиваем текст из модалки и зеркалим в боковую панель.
      const tidied = tidyPythonCode(modalArea.value || '');
      modalArea.value = tidied;
      if (sideArea) sideArea.value = tidied;
      modalArea.dispatchEvent(new Event('input', {bubbles: true}));
      if (sideArea) sideArea.dispatchEvent(new Event('input', {bubbles: true}));
      logMsg('🧹 Код упорядочен.', 'info');
    });
    document.getElementById('btn-modal-run')?.addEventListener('click', () => {
      // sync from modal to main, then run
      sideArea.value = modalArea.value;
      runPythonCode();
    });

    // «📍 В точку…» — спросить координаты у пользователя
    document.getElementById('btn-goto')?.addEventListener('click', () => {
      const xy = prompt('Координаты цели (X Y), пример: 100 -50', '0 0');
      if (xy === null) return;
      const cleaned = xy.trim();
      if (!cleaned) return;
      sendCmd('Вега в точку ' + cleaned);
    });

    // «🟡 Установить зону…» — спросить только радиус, поставить в
    // текущей позиции робота. Без goto: пользователь сам подъехал куда
    // надо (стрелками или «📍 В точку…»), и просто помечает место.
    document.getElementById('btn-set-zone')?.addEventListener('click', () => {
      const raw = prompt('Радиус зоны внимания (см):', '30');
      if (raw === null) return;
      const cleaned = raw.trim();
      if (!cleaned) return;
      // Сервер: «установи зону» без X Y → ставит в позиции робота;
      // «радиус N см» парсится nlu.extract_radius из общего raw.
      sendCmd('Вега установи зону радиус ' + cleaned + ' см');
    });

    // «✕ Убрать зону» — сразу убирает зону под роботом, без диалога.
    document.getElementById('btn-remove-zone')?.addEventListener('click', () => {
      sendCmd('Вега убрать зону');
    });

    // «💾 Сохранить миссию» — снимок текущего состояния свободного режима
    // (позиция, зоны, программа) → кастомная миссия в Каталоге.
    document.getElementById('btn-save-mission')?.addEventListener('click',
      async () => {
        const raw = window.prompt(
          'Название кастомной миссии:\n' +
          '(оставьте пустым — будет «Кастомная #N»)',
          ''
        );
        if (raw === null) return;        // нажал Отмена — не сохраняем
        const title = raw.trim();
        try {
          const r = await fetch('/missions/save_custom', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title}),
          });
          if (!r.ok) {
            logMsg(`Ошибка сохранения миссии: HTTP ${r.status}`, 'error');
            return;
          }
          const data = await r.json();
          logMsg(`✓ Миссия #${data.id} «${data.title}» сохранена в Каталог.`,
                 'success');
        } catch (e) {
          logMsg(`Ошибка сохранения миссии: ${e.message}`, 'error');
        }
      });

    // ── ⛯ Режим установки опасных зон мышью ───────────────────────────
    // Toggle-кнопка: ЛКМ ставит, Ctrl+ЛКМ удаляет, +/- меняет радиус,
    // ESC/ПКМ выход. Видимое состояние — класс .is-active на кнопке.
    const btnZoneMode = document.getElementById('btn-zone-mode');
    function setZoneModeActive(on) {
      if (!canvas) return;
      canvas.setZoneMode(on);
      if (btnZoneMode) btnZoneMode.classList.toggle('is-active', on);
      if (on) {
        logMsg(`⛯ Режим зон ВКЛ. Радиус ${canvas.zoneRadius} см. ` +
               `ЛКМ ставит опасную зону, ПКМ удаляет опасную (зоны внимания не трогаются), ` +
               `[+/-] меняет радиус, ESC выход.`, 'info');
        // Закрепить sticky-подсказку в подвале — пока режим включён,
        // обычные log-сообщения её не перебьют.
        setStatus(`⛯ Режим зон  •  радиус ${canvas.zoneRadius} см  •  ` +
                  `ЛКМ — поставить, ПКМ — удалить (опасные), ` +
                  `[+/-] — радиус, ESC — выход`, 'hint');
      } else {
        setStatus(null, 'hint:clear');
        logMsg('⛯ Режим зон выключен.', 'info');
      }
    }
    if (btnZoneMode && canvas) {
      btnZoneMode.addEventListener('click', () => {
        setZoneModeActive(!canvas.zoneMode);
      });
      // Коллбеки от canvas — отправляют команды на сервер
      canvas.onZonePlace = (wx, wy, r) => {
        sendCmd(`Вега опасная зона ${wx.toFixed(0)} ${wy.toFixed(0)} ${r}`);
      };
      canvas.onZoneRemove = (wx, wy) => {
        // ⛯ Режим зон + ПКМ → удаляет ТОЛЬКО опасные зоны обстановки.
        // Зоны внимания мышью не задеваются — они контролируются
        // алгоритмом, а не пользователем.
        sendCmd(`Вега убрать опасную зону ${wx.toFixed(0)} ${wy.toFixed(0)}`);
      };
      canvas.onZoneRadiusChange = (r) => {
        logMsg(`⛯ Радиус зоны: ${r} см.`, 'info');
        // Обновим текст sticky-подсказки в подвале с новым радиусом.
        if (canvas.zoneMode) {
          setStatus(`⛯ Режим зон  •  радиус ${r} см  •  ` +
                    `ЛКМ — поставить, ПКМ — удалить (опасные), ` +
                    `[+/-] — радиус, ESC — выход`, 'hint');
        }
      };
      // Глобальные клавиши: +/-/= меняют радиус, ESC выходит
      document.addEventListener('keydown', (e) => {
        if (!canvas.zoneMode) return;
        // Не перехватываем, если фокус на input/textarea (там +/- — это символы)
        const tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA') return;
        if (e.key === 'Escape') {
          e.preventDefault();
          setZoneModeActive(false);
        } else if (e.key === '+' || e.key === '=') {
          e.preventDefault();
          canvas.changeZoneRadius(+canvas.zoneRadiusStep);
        } else if (e.key === '-' || e.key === '_') {
          e.preventDefault();
          canvas.changeZoneRadius(-canvas.zoneRadiusStep);
        }
      });
    }

    // Очистить журнал
    const btnClearLog = document.getElementById('btn-clear-log');
    if (btnClearLog) {
      btnClearLog.addEventListener('click', () => {
        const log = document.getElementById('cmd-log');
        if (log) log.innerHTML = '';
      });
    }

    // «📋 Задание» — вставляет карточку с описанием активной миссии в
    // журнал. Если миссия не загружена — карточка содержит подсказку,
    // как загрузить миссию из каталога.
    const btnShowMission = document.getElementById('btn-show-mission');
    if (btnShowMission) {
      btnShowMission.addEventListener('click', _appendTaskCard);
    }

    // Голосовое управление
    initVoice();

    // Пинг каждые 30 секунд
    setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 30000);
  });

})();
