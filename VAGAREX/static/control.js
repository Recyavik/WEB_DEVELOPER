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
      // Применяем сохранённый множитель скорости визуализации (если он
      // не 1×) — без этого после reconnect сервер откатится на 1×.
      try {
        const v = parseFloat(localStorage.getItem('vegarex.sim_speed') || '1');
        if ([1, 1.5, 2, 4].includes(v) && v !== 1) {
          ws.send(JSON.stringify({ type: 'set_sim_speed', value: v }));
        }
      } catch (_) {}
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
        // С v4.10.6+ код приходит ОТДЕЛЬНЫМ сообщением 'code_append' ДО
        // выполнения, но fallback для совместимости остаётся.
        if (msg.code) {
          appendPythonCode(msg.description || 'Python-код команды', msg.code);
        }
        logMsg(msg.text, msg.level || 'info');
        updateVoiceStatus(msg.text);
        break;

      case 'code_append':
        // Сервер пушит код ДО выполнения команды (v4.10.6+) — чтобы
        // ребёнок видел запись в коде, а потом наблюдал как робот её
        // исполняет. В журнал НЕ пишется (логирование — после exec'a).
        if (msg.code) {
          appendPythonCode(msg.description || 'Python-код команды', msg.code);
        }
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
        }
        break;

      case 'code_exec_result':
        // Уровень приходит с сервера: 'error' для traceback'ов, 'ok' для успеха.
        // Многострочный traceback (`File "..."`, `NameError: ...`) рендерим
        // переносами, чтобы было читаемо как в обычной IDE-консоли.
        logMsg(msg.output || 'Выполнено.', msg.level || 'info');
        // Программа запущена ради сохранения миссии (траектории не было).
        // code_exec_result приходит ДВАЖДЫ: при старте (level 'info') и
        // при завершении (level 'ok'/'error'). Досохраняем ТОЛЬКО по
        // завершению — иначе POST уйдёт, пока программа ещё едет, и
        // в миссию попадёт лишь часть траектории.
        if (window._missionSavePending && msg.level !== 'info') {
          window._missionSavePending = false;
          if (msg.level === 'error') {
            logMsg('Миссия не сохранена: программа завершилась с ошибкой — '
                   + 'исправьте код и сохраните снова.', 'warning');
          } else {
            saveMission(window._missionSaveTitle || '', false);
          }
        }
        break;

      case 'obstacle_block':
        // Сервер прислал обновлённый блок «Опасные зоны обстановки»
        // (выход из ⛯ Режим зон). Surgical replace между сентинелями
        // в обеих textarea (боковая + развёрнутый редактор).
        if (typeof msg.block === 'string') {
          updateObstacleBlockInTextareas(msg.block);
        }
        break;

      case 'obstacles_line':
        // Сменился набор препятствий (галочки/голос) — переписываем
        // строку OBSTACLES в textarea (код — источник истины при ▶).
        if (Array.isArray(msg.obstacles)) {
          updateObstaclesLineInTextareas(msg.obstacles);
        }
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
          // Во время ЛЮБОЙ миссии набор препятствий зафиксирован
          // (только стены — режим WM): зоны не тормозят робота, наезды
          // на них штрафуют аккуратность. Галочки «Препятствия» блокируем.
          _setModeButtonsLocked(true,
            'Во время миссии препятствия зафиксированы (только стены). '
            + 'Завершите или остановите миссию, чтобы менять.');
          // Запрещаем читы: «📍 В точку», «🧭 Автопилот», «⛯ Обстановка».
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
    const fin = document.getElementById('btn-mission-finalize');
    // style.display, а НЕ .hidden: у .btn задан display:inline-flex,
    // который перебивает атрибут hidden (равная специфичность, авторский
    // стиль > UA). Inline-style надёжно управляет видимостью.
    if (fin) fin.style.display = '';   // «🏁 Проверка» видна, пока идёт миссия
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
    const fin = document.getElementById('btn-mission-finalize');
    if (fin) fin.style.display = 'none';
  }

  async function _finalizeMission() {
    if (!confirm('Запустить контрольную проверку задания?\n\n'
        + 'Алгоритм будет прогнан целиком автоматически, затем\n'
        + 'зафиксирован результат и начислены звёзды.\n'
        + 'После этого миссию можно будет запустить заново через каталог.')) return;
    const textareaEl = document.getElementById('python-code');
    const codeText = textareaEl ? textareaEl.value : '';
    logMsg('🧪 Контрольный проход алгоритма…', 'info');
    await fetch('/missions/active/finalize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: codeText}),
    });
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
    // Блокирует галочки «Препятствия» во время миссии с опасными
    // зонами: набор зафиксирован до stop/finalize. Сервер тоже
    // отвергает смену — это просто UX-индикация.
    const chkD = document.getElementById('chk-obst-danger');
    const chkA = document.getElementById('chk-obst-attention');
    [chkD, chkA].forEach(c => {
      if (!c) return;
      c.disabled = !!locked;
      const lbl = c.closest('.toggle');
      if (lbl) lbl.classList.toggle('is-locked', !!locked);
      if (locked && reason) c.title = reason;
    });
  }

  function _setMissionShortcutsLocked(locked) {
    // Блокирует «читы», обходящие честное прохождение миссии:
    //   • «📍 В точку…»  — телепорт-подобный goto;
    //   • «🧭 Автопилот» — авто-объезд зон;
    //   • «⛯ Обстановка» — мышиная правка/снятие зон.
    // Сервер тоже отвергает эти команды; кнопки гасим для ясности
    // (.is-locked — серый вид + not-allowed).
    const items = [
      ['btn-goto',
       'Во время миссии «в точку» нельзя — составьте маршрут из '
       + 'forward/поворотов в коде.'],
      ['btn-autopilot',
       'Во время миссии автопилот недоступен — пройдите маршрут сами.'],
      ['btn-zone-mode',
       'Во время миссии правка зон мышью («Обстановка») запрещена.'],
    ];
    items.forEach(([id, lockedTitle]) => {
      const btn = document.getElementById(id);
      if (!btn) return;
      btn.disabled = !!locked;
      btn.classList.toggle('is-locked', !!locked);
      if (locked) {
        if (!btn.dataset._titleOrig) btn.dataset._titleOrig = btn.title || '';
        btn.title = lockedTitle;
      } else if (btn.dataset._titleOrig !== undefined) {
        btn.title = btn.dataset._titleOrig;
        delete btn.dataset._titleOrig;
      }
    });
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
        `</div>`;
      card.querySelector('.js-mission-hint').addEventListener('click', _requestMissionHint);
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
    // «⭐ N · точки X/Y · установлено/удалено · аккуратность %».
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
    const quality    = (msg.quality_pct != null)
                       ? msg.quality_pct
                       : Math.round((msg.quality || 0) * 100);
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
        `  За аккуратность: <strong>${trackStars}</strong>` +
        (timeStars != null ? `<br>  За скорость прохождения: <strong>${timeStars}</strong>` : '') +
        `</div>`;
    }
    modal.querySelector('.mission-result-body').innerHTML =
      `<div style="text-align:center; font-size:1.4rem; margin-bottom:0.6rem">${starsStr || '—'}</div>` +
      breakdown +
      `<div style="margin-top:0.5rem">Аккуратность: <strong>${quality}%</strong></div>` +
      (timeStr ? `<div>Время задания: <strong>${timeStr}</strong></div>` : '') +
      (algoStr ? `<div>Время алгоритма: <strong>${algoStr}</strong></div>` : '');
    modal.hidden = false;
  }

  // ── Пикер «📂 Загрузить» — маршруты из Хранилища ──────────────────────────
  function _escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Загрузка выбранного маршрута в живое окно: сервер применяет обстановку
  // и возвращает код, клиент кладёт его в редактор (и в localStorage-черновик
  // через событие input — код переживёт перезагрузку страницы).
  async function _loadRoute(kind, id, title) {
    if (!confirm('Загрузить «' + title + '»?\n\n'
        + 'Текущий код в редакторе будет заменён. Если он нужен — '
        + 'сначала сохраните его кнопкой 💾.')) return;
    try {
      const r = await fetch('/library/' + kind + '/' + id + '/load_inplace',
                            { method: 'POST' });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        if (e.error === 'mission_active')
          logMsg('Нельзя загружать маршрут во время прохождения миссии.', 'warning');
        else
          logMsg('Не удалось загрузить маршрут.', 'error');
        return;
      }
      const data = await r.json();
      if (typeof data.code === 'string') {
        // Кладём код во все редакторы: боковой и развёрнутый (fallback).
        // input → подсветка + сохранение черновика в localStorage.
        ['python-code', 'python-code-modal-area'].forEach(eid => {
          const ta = document.getElementById(eid);
          if (ta) {
            ta.value = data.code;
            ta.dispatchEvent(new Event('input', { bubbles: true }));
          }
        });
      }
      const m = document.getElementById('load-route-modal');
      if (m) m.hidden = true;
      logMsg('📂 Загружено: «' + title + '». Код в редакторе, '
           + 'обстановка (старт и зоны) на поле.', 'success');
    } catch (e) {
      logMsg('Не удалось загрузить маршрут.', 'error');
    }
  }

  function _renderRouteList(items, kind) {
    if (!items.length) {
      return '<p class="hint" style="padding:.6rem 0">Пусто.</p>';
    }
    return items.map(it => {
      const meta = (kind === 'published')
        ? ('👤 ' + _escHtml(it.author) + ' · 🧩 ' + it.cmd_count)
        : ('🧩 ' + it.cmd_count + ' · 🕒 ' + _escHtml(it.when));
      return '<button type="button" class="btn load-route-item" '
           + 'data-kind="' + kind + '" data-id="' + it.id + '" '
           + 'data-title="' + _escHtml(it.title) + '" '
           + 'style="display:block;width:100%;text-align:left;margin:.25rem 0;'
           + 'font-weight:400">'
           + _escHtml(it.title)
           + '<span class="hint" style="margin-left:.5rem">' + meta + '</span>'
           + '</button>';
    }).join('');
  }

  async function _openLoadRouteModal() {
    let data;
    try {
      const r = await fetch('/library/routes.json');
      data = await r.json();
    } catch (e) {
      logMsg('Не удалось получить список Хранилища.', 'error');
      return;
    }
    let modal = document.getElementById('load-route-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'load-route-modal';
      modal.className = 'warn-modal';
      modal.innerHTML =
        '<div class="warn-modal__panel" style="max-width:560px">'
        + '<div class="warn-modal__header">'
        +   '<span class="warn-modal__icon">📂</span>'
        +   '<h3 class="warn-modal__title">Загрузить маршрут из Хранилища</h3>'
        + '</div>'
        + '<div class="warn-modal__body load-route-body"></div>'
        + '<div class="warn-modal__footer">'
        +   '<button type="button" class="btn warn-modal__close">Отмена</button>'
        + '</div></div>';
      document.body.appendChild(modal);
      modal.querySelector('.warn-modal__close')
           .addEventListener('click', () => { modal.hidden = true; });
      modal.addEventListener('mousedown', (e) => {
        if (e.target === modal) modal.hidden = true;
      });
      // Делегирование клика по строке маршрута.
      modal.querySelector('.load-route-body').addEventListener('click', (e) => {
        const item = e.target.closest('.load-route-item');
        if (!item) return;
        _loadRoute(item.dataset.kind, item.dataset.id, item.dataset.title);
      });
    }
    // Две панельки, у каждой свой скролл — длинные списки не растягивают
    // модалку и не наезжают друг на друга.
    const saved = data.saved || [];
    const pub   = data.published || [];
    const panel = 'max-height:30vh;overflow-y:auto;padding:.3rem;'
                + 'border:1px solid var(--border);border-radius:6px';
    modal.querySelector('.load-route-body').innerHTML =
        '<h4 style="margin:.2rem 0 .3rem">💾 Мои сохранения (' + saved.length + ')</h4>'
      + '<div style="' + panel + '">' + _renderRouteList(saved, 'saved') + '</div>'
      + '<h4 style="margin:.9rem 0 .3rem">🌐 Опубликованные (' + pub.length + ')</h4>'
      + '<div style="' + panel + '">' + _renderRouteList(pub, 'published') + '</div>';
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
  // ── Surgical update блока «Опасные зоны обстановки» в textarea ────────
  // Сентинели должны точно совпадать со строками из session.py
  // (OBSTACLE_BLOCK_TOP/BOT). При выходе из ⛯ Режим зон сервер шлёт
  // obstacle_block; находим блок между сентинелями и заменяем.
  // Если блока ещё нет — вставляем перед «=== НАЧАЛО ПРОГРАММЫ ===».
  const _OBST_TOP    = '# === Опасные зоны обстановки ===';
  const _OBST_BOT    = '# === Конец опасных зон обстановки ===';
  const _START_ANCHOR = '# === НАЧАЛО ПРОГРАММЫ ===';

  function _replaceObstacleBlock(text, newBlock) {
    if (typeof text !== 'string') return text;
    // newBlock уже содержит обе сентинели и завершающий \n.
    const topIdx = text.indexOf(_OBST_TOP);
    if (topIdx >= 0) {
      // Блок есть — заменяем от topIdx до конца строки с _OBST_BOT.
      const botIdx = text.indexOf(_OBST_BOT, topIdx);
      if (botIdx < 0) return text;       // повреждённый блок — не трогаем
      const endOfLine = text.indexOf('\n', botIdx);
      const endIdx = (endOfLine === -1) ? text.length : endOfLine + 1;
      return text.slice(0, topIdx) + newBlock + text.slice(endIdx);
    }
    // Блока нет — вставляем перед НАЧАЛО ПРОГРАММЫ.
    const anchorIdx = text.indexOf(_START_ANCHOR);
    if (anchorIdx >= 0) {
      return text.slice(0, anchorIdx) + newBlock + '\n' + text.slice(anchorIdx);
    }
    // Нет и anchor — приклеиваем сверху (вряд ли произойдёт).
    return newBlock + '\n' + text;
  }

  function updateObstacleBlockInTextareas(newBlock) {
    const ids = ['python-code', 'python-code-modal-area'];
    for (const id of ids) {
      const ta = document.getElementById(id);
      if (!ta) continue;
      const updated = _replaceObstacleBlock(ta.value || '', newBlock);
      if (updated !== ta.value) {
        ta.value = updated;
        // input → overlay/гуттер/draft-backup пересчитываются автоматом.
        ta.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
    logMsg('🗺 Блок «Опасные зоны обстановки» обновлён.', 'info');
  }

  // ── Surgical update строки OBSTACLES (галочки «Препятствия») ─────────
  // Сервер шлёт obstacles_line при смене галочек/голосом. Код — источник
  // истины: при ▶ Запуске набор читается из этой строки (как START_X).
  const _OBST_LINE_RE = /^OBSTACLES\s*=\s*\[[^\]]*\][^\n]*$/m;

  function _obstaclesLiteral(obs) {
    const order = ['danger', 'attention'];
    const items = order.filter(k => obs.includes(k));
    return '[' + items.map(k => '"' + k + '"').join(', ') + ']';
  }

  function _replaceObstaclesLine(text, obs) {
    if (typeof text !== 'string') return text;
    const line = 'OBSTACLES = ' + _obstaclesLiteral(obs);
    if (_OBST_LINE_RE.test(text)) return text.replace(_OBST_LINE_RE, line);
    // Строки нет (загруженный извне код) — вставляем перед блоком
    // обстановки либо перед началом программы.
    const block = '# ── Препятствия: "danger" / "attention" (стены — всегда) ──\n'
                + line + '\n\n';
    const obstIdx = text.indexOf(_OBST_TOP);
    if (obstIdx >= 0) return text.slice(0, obstIdx) + block + text.slice(obstIdx);
    const anchorIdx = text.indexOf(_START_ANCHOR);
    if (anchorIdx >= 0) return text.slice(0, anchorIdx) + block + text.slice(anchorIdx);
    return block + text;
  }

  function updateObstaclesLineInTextareas(obs) {
    const ids = ['python-code', 'python-code-modal-area'];
    for (const id of ids) {
      const ta = document.getElementById(id);
      if (!ta) continue;
      const updated = _replaceObstaclesLine(ta.value || '', obs);
      if (updated !== ta.value) {
        ta.value = updated;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
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

    // ── Tab / Shift+Tab — отступы для Python ──────────────────────────
    // Без этого Tab уводит фокус с textarea (browser default). Здесь:
    //   • Tab без выделения → вставить 4 пробела в каретку;
    //   • Tab с многострочным выделением → отступить каждую строку;
    //   • Shift+Tab → убрать до 4 ведущих пробелов / 1 \t (dedent).
    const INDENT = '    ';
    ta.addEventListener('keydown', (e) => {
      if (e.key !== 'Tab') return;
      e.preventDefault();
      const start = ta.selectionStart;
      const end   = ta.selectionEnd;
      const v     = ta.value;
      const multilineSel = (start !== end) && v.slice(start, end).includes('\n');
      if (multilineSel) {
        const lineStart = v.lastIndexOf('\n', start - 1) + 1;
        const block = v.slice(lineStart, end);
        let newBlock;
        if (e.shiftKey) {
          newBlock = block.split('\n').map(line => {
            if (line.startsWith(INDENT)) return line.slice(4);
            if (line.startsWith('\t'))   return line.slice(1);
            return line.replace(/^ {1,3}/, '');
          }).join('\n');
        } else {
          newBlock = block.split('\n').map(line => INDENT + line).join('\n');
        }
        ta.value = v.slice(0, lineStart) + newBlock + v.slice(end);
        ta.selectionStart = lineStart;
        ta.selectionEnd   = lineStart + newBlock.length;
      } else if (e.shiftKey) {
        // Shift+Tab без выделения — dedent текущей строки.
        const lineStart = v.lastIndexOf('\n', start - 1) + 1;
        const m = v.slice(lineStart).match(/^( {1,4}|\t)/);
        if (m) {
          const cut = m[0].length;
          ta.value = v.slice(0, lineStart) + v.slice(lineStart + cut);
          ta.selectionStart = ta.selectionEnd = Math.max(lineStart, start - cut);
        }
      } else {
        // Tab без выделения — вставить 4 пробела.
        ta.value = v.slice(0, start) + INDENT + v.slice(end);
        ta.selectionStart = ta.selectionEnd = start + INDENT.length;
      }
      ta.dispatchEvent(new Event('input', {bubbles: true}));
    });
    // Первоначальная подсветка
    syncContent();

    // ── Гуттер с номерами строк (только модальный редактор) ──────────
    // Помогает быстро найти строку из traceback: «Ошибка в строке 12»
    // — пользователь видит цифру 12 в столбце слева.
    const gutterId = textareaId.replace(/-area$/, '-gutter');
    const gutter   = document.getElementById(gutterId);
    if (gutter) {
      const renderGutter = () => {
        const lines = (ta.value || '').split('\n').length;
        // Не плодим лишние ноды, просто текстом.
        let out = '';
        for (let i = 1; i <= lines; i++) out += i + '\n';
        gutter.textContent = out;
      };
      const syncGutterScroll = () => { gutter.scrollTop = ta.scrollTop; };
      ta.addEventListener('input',  renderGutter);
      ta.addEventListener('scroll', syncGutterScroll);
      renderGutter();
    }

    // ── Shift+колесо мыши — масштаб шрифта редактора ──────────────────
    // CSS-переменная --code-font-size ставится на общий .code-wrap--modal
    // и каскадом наследуется в textarea + overlay + gutter (так как все
    // их font-size использует var(--code-font-size, 15px)). Если ставить
    // на textarea отдельно — overlay/gutter останутся со старым значением,
    // потому что CSS-переменные распространяются ВНИЗ по DOM, а они —
    // сиблинги textarea, не его потомки.
    const wrap = ta.closest('.code-wrap--modal');
    if (wrap) {
      const FONT_KEY = `cm:font:${textareaId}`;
      const saved = parseInt(localStorage.getItem(FONT_KEY) || '0', 10);
      if (saved >= 9 && saved <= 32) {
        wrap.style.setProperty('--code-font-size', saved + 'px');
      }
      wrap.addEventListener('wheel', (e) => {
        if (!e.shiftKey) return;
        e.preventDefault();
        const cur = parseInt(getComputedStyle(ta).fontSize, 10) || 15;
        const delta = e.deltaY > 0 ? -1 : +1;
        const next = Math.max(9, Math.min(32, cur + delta));
        wrap.style.setProperty('--code-font-size', next + 'px');
        localStorage.setItem(FONT_KEY, String(next));
      }, {passive: false});
    }

    // Программные изменения value (например, server append) не дают input —
    // дублирующий polling 200мс.
    let lastVal = ta.value;
    setInterval(() => {
      if (ta.value !== lastVal) {
        lastVal = ta.value;
        syncContent();
        if (gutter) {
          // Пересчитываем гуттер по тому же эвенту, что и подсветка.
          const lines = (ta.value || '').split('\n').length;
          let out = '';
          for (let i = 1; i <= lines; i++) out += i + '\n';
          gutter.textContent = out;
        }
      }
    }, 200);
  }

  // ── Popup-автодополнение для textarea ────────────────────────────────
  // Показывает список robot.X методов/свойств когда юзер печатает «robot.»
  // в любой textarea — и в боковом поле кода, и в развёрнутом редакторе.
  //
  // Один общий popup-элемент на странице (`#robot-autocomplete-popup`),
  // переиспользуется между textarea. Текущая «привязка» хранится в
  // замыкании attachRobotAutocomplete (своё состояние на textarea).
  function _ensurePopup() {
    let pop = document.getElementById('robot-autocomplete-popup');
    if (pop) return pop;
    pop = document.createElement('div');
    pop.id = 'robot-autocomplete-popup';
    pop.className = 'robot-autocomplete';
    pop.hidden = true;
    document.body.appendChild(pop);
    return pop;
  }

  // Координаты каретки в textarea (для позиционирования popup'а).
  // Считаем приблизительно: монопространный шрифт + lineHeight из CSS.
  function _getCaretCoords(ta, pos) {
    const text = ta.value.substring(0, pos);
    const lines = text.split('\n');
    const lineNum = lines.length - 1;
    const colNum  = lines[lineNum].length;
    const cs = window.getComputedStyle(ta);
    const fontSize = parseFloat(cs.fontSize) || 15;
    const lh = parseFloat(cs.lineHeight) || fontSize * 1.4;
    // Ширина символа: измеряем 'M' для текущего шрифта через canvas.
    if (!_getCaretCoords._ctx) {
      _getCaretCoords._ctx = document.createElement('canvas').getContext('2d');
    }
    const ctx = _getCaretCoords._ctx;
    ctx.font = `${cs.fontStyle || 'normal'} ${cs.fontWeight || 'normal'} `
             + `${cs.fontSize} ${cs.fontFamily}`;
    const charW = ctx.measureText('M').width || (fontSize * 0.6);
    const rect = ta.getBoundingClientRect();
    const padTop  = parseFloat(cs.paddingTop)  || 0;
    const padLeft = parseFloat(cs.paddingLeft) || 0;
    const x = rect.left + padLeft + colNum * charW - ta.scrollLeft;
    const y = rect.top  + padTop  + lineNum * lh   - ta.scrollTop;
    return {x, y, lh};
  }

  function _escHTML(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;',
    }[c]));
  }

  // Каталог методов/свойств → один плоский массив объектов с полями
  // {name, sig, info, type}. type ∈ {'method', 'property'}.
  function _flatCatalog() {
    const c = window.ROBOT_API_CATALOG;
    if (!c) return [];
    return [
      ...c.methods.map(([name, sig, info]) =>
        ({name, sig, info, type: 'method'})),
      ...c.properties.map(([name, info]) =>
        ({name, sig: '', info, type: 'property'})),
    ];
  }

  function attachRobotAutocomplete(textareaId) {
    const ta = document.getElementById(textareaId);
    if (!ta) return;
    if (ta.dataset._acWired) return;       // идемпотентно: можно звать дважды
    ta.dataset._acWired = '1';
    if (!window.ROBOT_API_CATALOG) return; // каталог не загрузился — нечего показывать

    const popup = _ensurePopup();
    let items = [];
    let selIdx = 0;
    let active = false;
    let triggerStart = -1;  // абсолютный индекс символа после «robot.»

    const close = () => {
      if (!active) return;
      popup.hidden = true;
      active = false;
      items = [];
      selIdx = 0;
      triggerStart = -1;
    };

    // Список с фиксированной высотой строк + отдельная полоса описания
    // внизу popup'а. Прокрутка стрелками меняет ТОЛЬКО класс выделения
    // и текст описания — DOM-строки не перерисовываются, прыжков нет.
    const renderList = () => {
      const listHtml = items.map((it, i) => {
        const klass = 'robot-autocomplete__item'
                   + (i === selIdx ? ' robot-autocomplete__item--sel' : '');
        const icon  = it.type === 'method' ? 'ƒ' : '•';
        return `<div class="${klass}" data-idx="${i}">`
             +   `<span class="robot-autocomplete__icon robot-autocomplete__icon--${it.type}">${icon}</span>`
             +   `<span class="robot-autocomplete__name">${_escHTML(it.name)}</span>`
             +   `<span class="robot-autocomplete__sig">${_escHTML(it.sig)}</span>`
             + `</div>`;
      }).join('');
      popup.innerHTML =
          `<div class="robot-autocomplete__list">${listHtml}</div>`
        + `<div class="robot-autocomplete__doc"></div>`;
      renderDoc();
      scrollSelIntoView();
    };
    const renderDoc = () => {
      const doc = popup.querySelector('.robot-autocomplete__doc');
      if (!doc) return;
      const it = items[selIdx];
      doc.textContent = it ? (it.info || '') : '';
    };
    const scrollSelIntoView = () => {
      const sel = popup.querySelector('.robot-autocomplete__item--sel');
      if (sel) sel.scrollIntoView({block: 'nearest'});
    };
    // Меняем выделение без перерисовки списка — только классы + описание.
    const setSel = (newIdx) => {
      if (!items.length) return;
      const list = popup.querySelector('.robot-autocomplete__list');
      if (!list) { selIdx = newIdx; return; }
      const oldEl = list.children[selIdx];
      if (oldEl) oldEl.classList.remove('robot-autocomplete__item--sel');
      selIdx = ((newIdx % items.length) + items.length) % items.length;
      const newEl = list.children[selIdx];
      if (newEl) {
        newEl.classList.add('robot-autocomplete__item--sel');
        newEl.scrollIntoView({block: 'nearest'});
      }
      renderDoc();
    };

    const insert = (item) => {
      if (!item) return;
      const caret = ta.selectionStart;
      const before = ta.value.slice(0, triggerStart);
      const after  = ta.value.slice(caret);
      const ins = item.type === 'method' ? item.name + '(' : item.name;
      ta.value = before + ins + after;
      const newCaret = before.length + ins.length;
      ta.setSelectionRange(newCaret, newCaret);
      close();
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.focus();
    };

    const update = () => {
      if (document.activeElement !== ta) { close(); return; }
      const caret = ta.selectionStart;
      // Сканируем 60 символов перед кареткой — этого хватит на «robot.».
      const lookback = Math.max(0, caret - 60);
      const slice = ta.value.slice(lookback, caret);
      const m = slice.match(/robot\.(\w*)$/);
      if (!m) { close(); return; }
      const partial = m[1].toLowerCase();
      const afterDot = caret - m[1].length;
      const matches = _flatCatalog()
        .filter(it => it.name.toLowerCase().startsWith(partial));
      if (matches.length === 0) { close(); return; }
      items = matches;
      // Сохраняем выделение если оно осталось валидным.
      const prevName = active && items[selIdx] ? items[selIdx].name : null;
      selIdx = 0;
      if (prevName) {
        const idx = matches.findIndex(it => it.name === prevName);
        if (idx >= 0) selIdx = idx;
      }
      triggerStart = afterDot;
      renderList();
      const c = _getCaretCoords(ta, caret);
      // Размещаем popup ПОД строкой (caret.y + lineHeight). Если не лезет
      // внизу экрана — над строкой.
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const popW = 420;
      const popH = Math.min(popup.scrollHeight || 240, 320);
      let x = c.x;
      let y = c.y + c.lh + 2;
      if (x + popW > vw - 8) x = Math.max(8, vw - popW - 8);
      if (y + popH > vh - 8) y = Math.max(8, c.y - popH - 2);
      popup.style.left = x + 'px';
      popup.style.top  = y + 'px';
      popup.hidden = false;
      active = true;
    };

    ta.addEventListener('input',  update);
    ta.addEventListener('keyup',  (e) => {
      // input уже триггернул update, но keyup ловит ArrowLeft/Right/Home/End,
      // которые двигают каретку без изменения текста.
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight'
          || e.key === 'Home'   || e.key === 'End') update();
    });
    ta.addEventListener('click',  update);
    ta.addEventListener('keydown',(e) => {
      if (!active) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSel(selIdx + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSel(selIdx - 1);
      } else if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        insert(items[selIdx]);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close();
      }
    });
    ta.addEventListener('blur', () => {
      // Задержка чтобы успел сработать mousedown на popup'е.
      setTimeout(() => {
        if (document.activeElement !== ta) close();
      }, 150);
    });

    // Mousedown по элементу (НЕ click), чтобы textarea не успел потерять
    // фокус до вставки — иначе selectionStart обнулится.
    popup.addEventListener('mousedown', (e) => {
      const item = e.target.closest('.robot-autocomplete__item');
      if (!item) return;
      e.preventDefault();
      const idx = parseInt(item.dataset.idx, 10);
      if (!isNaN(idx)) insert(items[idx]);
    });
    // Hover двигает выделение (как в IDE).
    popup.addEventListener('mousemove', (e) => {
      const item = e.target.closest('.robot-autocomplete__item');
      if (!item) return;
      const idx = parseInt(item.dataset.idx, 10);
      if (!isNaN(idx) && idx !== selIdx) setSel(idx);
    });
    // Закрываем popup при resize/прокрутке СТРАНИЦЫ.
    window.addEventListener('resize', close);
    // capture=true ловит scroll любого элемента — но прокрутка ВНУТРИ
    // самого popup'а (списка автодополнения при навигации стрелками)
    // не должна его закрывать. Закрываем только при скролле страницы.
    window.addEventListener('scroll', (e) => {
      const t = e.target;
      if (t && t.nodeType === 1 && (t === popup || popup.contains(t))) return;
      close();
    }, true);
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

    // Препятствия (набор) + взаимоисключающий ⛯ Режим зон.
    // Статус — компактный код по первым буквам: W=стены (всегда),
    // D=опасные зоны (danger), A=зоны внимания (attention).
    // Напр. "W", "WD", "WDA", "WA". Полное название — в title.
    const zoneMode  = !!s.zone_mode;
    const obstacles = Array.isArray(s.obstacles) ? s.obstacles : [];
    const cautious  = obstacles.length > 0;   // активен хоть один тип зон
    let modeText, modeTitle;
    if (window._currentMission) {
      // Идёт проверка миссии — препятствия только стены (W), миссия (M).
      modeText  = 'WM';
      modeTitle = 'Проверка миссии: препятствия — только стены, '
                + 'зоны не тормозят робота (наезд — штраф аккуратности).';
    } else if (zoneMode) {
      modeText  = '⛯ Обстановка';
      modeTitle = 'Обстановка — расстановка зон мышью';
    } else {
      let code = 'W';
      const full = ['стены'];
      if (obstacles.includes('danger'))    { code += 'D'; full.push('опасные зоны'); }
      if (obstacles.includes('attention')) { code += 'A'; full.push('зоны внимания'); }
      modeText  = code;
      modeTitle = 'Препятствия: ' + full.join(', ');
    }
    const stMode = document.getElementById('st-mode');
    if (stMode) {
      stMode.textContent = modeText;
      stMode.title = modeTitle;
      stMode.classList.toggle('state-value--cautious', cautious);
      stMode.classList.toggle('state-value--zone', zoneMode);
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

    // Синхронизация галочек «Препятствия» и подсветки ⛯ Режима зон.
    // Кэшируем сигнатуру набора и обновляем только при реальной смене —
    // иначе DOM дёргается на каждом push_state (10/с во время движения).
    const obstSig = obstacles.slice().sort().join(',') + '|' + zoneMode;
    if (window._lastObstSig !== obstSig) {
      window._lastObstSig = obstSig;
      // Галочки следуют за серверным набором (echo подтверждает клик).
      const chkD = document.getElementById('chk-obst-danger');
      const chkA = document.getElementById('chk-obst-attention');
      if (chkD) chkD.checked = obstacles.includes('danger');
      if (chkA) chkA.checked = obstacles.includes('attention');
      // «🟡 Установить зону…» недоступна, когда «Зоны внимания» включены
      // в препятствия: поставленная зона заперла бы робота внутри себя
      // (сервер такую команду и так отклоняет — кнопку гасим для ясности).
      const btnSetZone = document.getElementById('btn-set-zone');
      if (btnSetZone) {
        const attnObstacle = obstacles.includes('attention');
        btnSetZone.disabled = attnObstacle;
        // .is-locked — визуальная блокировка (серый вид + not-allowed);
        // без неё disabled-кнопка .btn выглядит как обычная активная.
        btnSetZone.classList.toggle('is-locked', attnObstacle);
        btnSetZone.title = attnObstacle
          ? '«⚠ Зоны внимания» включены в «Препятствия» — установка '
            + 'запрещена: зона заперла бы робота внутри. Снимите галочку '
            + '«⚠ Зоны внимания».'
          : 'Поставить жёлтую зону внимания в ТЕКУЩЕЙ позиции робота '
            + '(только радиус — без поездки)';
      }
      const btnZone = document.getElementById('btn-zone-mode');
      if (btnZone) btnZone.classList.toggle('is-active', zoneMode);
      const appbar = document.querySelector('.appbar');
      if (appbar) appbar.classList.toggle('appbar--cautious', cautious);
      // body.mode-cautious — глобальный сигнал «реагируем на зоны»
      // для action-кнопок (.btn--mode-action перекрашиваются под warn).
      document.body.classList.toggle('mode-cautious', cautious);
      document.body.classList.toggle('mode-zone', zoneMode);
      // Синхронизуем canvas-режим зон и подвал-хинт. canvas / setStatus
      // лежат в той же module-scope.
      if (canvas) canvas.setZoneMode(zoneMode);
      if (zoneMode) {
        const r = (canvas && canvas.zoneRadius) || 30;
        setStatus(`⛯ Обстановка  •  радиус${r} см  •  ` +
                  `ЛКМ — поставить, ПКМ — удалить (опасные), ` +
                  `[+/-] — радиус, ESC — выход`, 'hint');
      } else {
        setStatus(null, 'hint:clear');
      }
    }

    // Дублирующий бейдж больше не нужен — режим уже виден в строке «Режим».
    const caut = document.getElementById('caution-badge');
    if (caut) caut.style.display = 'none';

    // ⏸ Пауза / ▶ Продолжить — обе всегда видны, чередуется только
    // disabled-флаг по серверному s.program_paused: программа идёт →
    // активна Пауза; на паузе → активно Продолжить.
    const paused = !!s.program_paused;
    const btnPause = document.getElementById('btn-program-pause');
    const btnResumeProg = document.getElementById('btn-program-resume');
    if (btnPause)      btnPause.disabled      = paused;
    if (btnResumeProg) btnResumeProg.disabled = !paused;
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
    const now  = new Date();
    const time = `${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}:${now.getSeconds().toString().padStart(2,'0')}`;
    // Дублируем сообщение в основной журнал И в журнал развёрнутого
    // редактора — чтобы при открытой модалке (превью закрыто) ошибки
    // и print() оставались видны рядом с кодом.
    const targets = [
      document.getElementById('cmd-log'),
      document.getElementById('code-modal-journal'),
    ];
    for (const log of targets) {
      if (!log) continue;
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
      // Программное value= не триггерит input — диспатчим явно, чтобы
      // _saveCodeDraft (на input) сохранил в localStorage. Иначе после F5
      // только что добавленная команда пропадает.
      textarea.dispatchEvent(new Event('input', {bubbles: true}));
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
    // Программное value= не триггерит input → draft в localStorage
    // не сохраняется. Диспатчим вручную, чтобы при F5 команды остались.
    textarea.dispatchEvent(new Event('input', {bubbles: true}));
  }

  // Сохранение кастомной миссии. Если на поле нет траектории — сервер
  // вернёт no_trajectory; тогда (allowAutoRun) сами запускаем программу
  // (▶) и после её завершения сохраняем повторно. Так миссия всегда
  // сохраняется с траекторией, а из диалогов — только запрос названия.
  async function saveMission(title, allowAutoRun) {
    let data;
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
      data = await r.json();
    } catch (e) {
      logMsg(`Ошибка сохранения миссии: ${e.message}`, 'error');
      return;
    }
    if (data.error === 'no_trajectory') {
      if (allowAutoRun) {
        logMsg('Перед сохранением прогоняю программу (▶) — '
               + 'после прогона запишутся контрольные точки маршрута…',
               'info');
        window._missionSaveTitle   = title;
        window._missionSavePending = true;
        runPythonCode();
      } else {
        logMsg('Миссия не сохранена: программа не оставила траектории. '
               + 'Проверьте код — в нём должны быть команды движения '
               + 'робота.', 'warning');
      }
      return;
    }
    if (data.error) {
      logMsg(`Ошибка сохранения миссии: ${data.error}`, 'error');
      return;
    }
    // Успех: запись «💾 Миссия #N «…» сохранена в Каталог» в журнал
    // делает сервер (push_message) — здесь не дублируем.
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
    // Триггерим input-событие для overlay боковой панели.
    main.dispatchEvent(new Event('input', {bubbles: true}));
    logMsg('🧹 Код упорядочен: функции собраны вверху, вызовы внизу.', 'info');
  }

  // ── «Сборка кода для робота» (модалка </>) ─────────────────────────────
  // Click </> → tidyPythonCode → POST /code/export → показать в редактируемом
  // textarea с гуттером. Источник правды для Копировать / Скачать / Выгрузить —
  // текущее содержимое textarea, не отдельная переменная (учащийся может
  // править прямо в превью).

  function _getCurrentEditorText() {
    // Источник правды — боковой textarea. Если открыт развёрнутый редактор —
    // берём его (там могут быть несохранённые правки модального окна).
    const modal   = document.getElementById('python-code-modal');
    const modalTa = document.getElementById('python-code-modal-area');
    if (modal && !modal.hidden && modalTa) return modalTa.value;
    const main = document.getElementById('python-code');
    return main ? main.value : '';
  }

  function _updateExportGutter() {
    const ta  = document.getElementById('code-export-text');
    const gut = document.getElementById('code-export-gutter');
    if (!ta || !gut) return;
    const n = (ta.value.match(/\n/g) || []).length + 1;
    const lines = [];
    for (let i = 1; i <= n; i++) lines.push(i);
    gut.textContent = lines.join('\n');
    gut.scrollTop = ta.scrollTop;
  }

  function _getExportedCode() {
    const ta = document.getElementById('code-export-text');
    return ta ? ta.value : '';
  }

  function _setExportedCode(text) {
    const ta = document.getElementById('code-export-text');
    if (!ta) return;
    ta.value = text;
    _updateExportGutter();
  }

  async function openExportModal() {
    const exportModal = document.getElementById('code-export-modal');
    if (!exportModal) return;

    const userCode = _getCurrentEditorText() || '';
    const tidied = tidyPythonCode(userCode);
    _setExportedCode('Готовится…');
    exportModal.hidden = false;

    try {
      const r = await fetch('/code/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: tidied}),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      _setExportedCode(j.code || '');
    } catch (e) {
      _setExportedCode('# Не удалось собрать код: ' + e.message);
    }
  }

  function closeExportModal() {
    const m = document.getElementById('code-export-modal');
    if (m) m.hidden = true;
  }

  function _downloadExportedCode() {
    const code = _getExportedCode();
    if (!code) return;
    const blob = new Blob([code], {type: 'text/x-python;charset=utf-8'});
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    // Имя файла — vegarex_program_YYYY-MM-DD.py
    const d = new Date();
    const pad = n => String(n).padStart(2, '0');
    a.download = `vegarex_program_${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}.py`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    logMsg('⬇ Скачан ' + a.download, 'info');
  }

  function _copyExportedCode() {
    const code = _getExportedCode();
    if (!code) return;
    navigator.clipboard?.writeText(code).then(
      () => logMsg('⎘ Скопировано в буфер.', 'info'),
      () => logMsg('⚠ Не удалось скопировать.', 'warning')
    );
  }

  function _uploadToRobot() {
    // На реальный 1Т REX код заливается через server.py (мост браузер⇄
    // Python⇄ESP32). Браузер не может из соображений безопасности
    // запускать Python на ПК пользователя, поэтому даём чёткую
    // инструкцию. Алгоритм работы — на ПК, который общается с
    // роботом через server.py:41235 (см. класс _NetworkRobot в файле).
    alert('Чтобы запустить программу на 1Т REX:\n\n' +
          '1. Скачай .py-файл («⬇ Скачать .py»)\n' +
          '2. Запусти server.py (мост браузер⇄ESP32)\n' +
          '3. Подключи робота: WiFi или USB-Serial (выбор в GUI server.py)\n' +
          '4. В скачанном файле раскомментируй строку:\n' +
          '       robot = _NetworkRobot()\n' +
          '   (и закомментируй robot = _LiveRobot())\n' +
          '5. На ПК: pip install websocket-client\n' +
          '6. Запусти программу: python vegarex_program_*.py');
  }

  function _triggerLoadFromFile() {
    document.getElementById('export-load-input')?.click();
  }

  function _handleLoadedFile(evt) {
    const file = evt.target.files && evt.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {
      _setExportedCode(String(e.target.result || ''));
      logMsg(`📁 Загружен ${file.name}.`, 'info');
    };
    reader.onerror = () => {
      logMsg('⚠ Не удалось прочитать файл.', 'warning');
    };
    reader.readAsText(file, 'utf-8');
    // Сбрасываем value, чтобы повторный выбор того же файла тоже сработал.
    evt.target.value = '';
  }

  function _wireExportEditor() {
    const ta = document.getElementById('code-export-text');
    if (!ta) return;
    // Sync гуттера на input + scroll.
    ta.addEventListener('input', _updateExportGutter);
    ta.addEventListener('scroll', () => {
      const gut = document.getElementById('code-export-gutter');
      if (gut) gut.scrollTop = ta.scrollTop;
    });
    // Tab → 4 пробела (Shift+Tab → dedent).
    ta.addEventListener('keydown', (e) => {
      if (e.key !== 'Tab') return;
      e.preventDefault();
      const s = ta.selectionStart, en = ta.selectionEnd, v = ta.value;
      const multiline = (s !== en) && v.slice(s, en).includes('\n');
      if (multiline) {
        const ls = v.lastIndexOf('\n', s - 1) + 1;
        const block = v.slice(ls, en);
        const newBlock = e.shiftKey
          ? block.split('\n').map(l => l.startsWith('    ') ? l.slice(4) : l.replace(/^ {1,3}/, '')).join('\n')
          : block.split('\n').map(l => '    ' + l).join('\n');
        ta.value = v.slice(0, ls) + newBlock + v.slice(en);
        ta.selectionStart = ls;
        ta.selectionEnd   = ls + newBlock.length;
      } else if (!e.shiftKey) {
        ta.value = v.slice(0, s) + '    ' + v.slice(en);
        ta.selectionStart = ta.selectionEnd = s + 4;
      }
      _updateExportGutter();
    });
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
      btn.addEventListener('click', () => {
        // Перед «↺ Поле» (Вега новое поле) синхронизируем текст
        // редактора с сервером — _run_reset прочитает START_X/Y/HEADING_DEG
        // из КОДА (он главнее настроек). Иначе после правки констант
        // и нажатия «↺ Поле» робот вставал бы в старые координаты.
        const cmd = btn.dataset.cmd || '';
        if (cmd.includes('новое поле') && ws && ws.readyState === WebSocket.OPEN) {
          const ta = document.getElementById('python-code');
          ws.send(JSON.stringify({type: 'sync_code', code: ta ? ta.value : ''}));
        }
        sendCmd(cmd);
      });
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

    // Галочки «Препятствия» — на change шлём серверу актуальный набор.
    // Сервер применит, перепишет строку OBSTACLES в коде и вернёт state.
    function _sendObstacles() {
      const obs = [];
      const chkD = document.getElementById('chk-obst-danger');
      const chkA = document.getElementById('chk-obst-attention');
      if (chkD && chkD.checked) obs.push('danger');
      if (chkA && chkA.checked) obs.push('attention');
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'set_obstacles', obstacles: obs }));
      }
    }
    document.getElementById('chk-obst-danger')
            ?.addEventListener('change', _sendObstacles);
    document.getElementById('chk-obst-attention')
            ?.addEventListener('change', _sendObstacles);

    // 🧭 Автопилот — спрашиваем координаты цели и шлём команду.
    // Робот сам построит маршрут в обход препятствий и поедет.
    document.getElementById('btn-autopilot')?.addEventListener('click', () => {
      const ans = prompt('Автопилот — координаты цели «X Y»:', '0 0');
      if (ans === null) return;
      const m = ans.trim().match(/^(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)$/);
      if (!m) {
        logMsg('🧭 Не понял координаты. Пример: 100 -150', 'error');
        return;
      }
      sendCmd(`Вега автопилот ${m[1]} ${m[2]}`);
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
    document.getElementById('btn-tidy-python-code')?.addEventListener('click', openExportModal);

    // ── Кнопки модалки «Сборка кода для робота» (</>) ─────────────────
    document.getElementById('btn-export-copy')?.addEventListener('click', _copyExportedCode);
    document.getElementById('btn-export-download')?.addEventListener('click', _downloadExportedCode);
    document.getElementById('btn-export-upload')?.addEventListener('click', _uploadToRobot);
    document.getElementById('btn-export-load')?.addEventListener('click', _triggerLoadFromFile);
    document.getElementById('export-load-input')?.addEventListener('change', _handleLoadedFile);
    _wireExportEditor();
    document.querySelectorAll('[data-close-export-modal]').forEach(el => {
      el.addEventListener('click', closeExportModal);
    });
    document.addEventListener('keydown', (e) => {
      // Esc закрывает только если export-модалка открыта (sim-speed-меню
      // ловят Esc раньше, но они hidden — это безопасно).
      const m = document.getElementById('code-export-modal');
      if (e.key === 'Escape' && m && !m.hidden) {
        closeExportModal();
      }
    });

    // Подсветка комментариев в боковом редакторе. Подсветка модального
    // редактора навешивается в activateModalEditor() при открытии.
    attachCodeHighlight('python-code',            'python-code-overlay');

    // Popup-автодополнение robot.X в боковом поле кода. Для модального
    // редактора автодополнение прикрутится в activateModalEditor().
    attachRobotAutocomplete('python-code');

    // ── Модальное окно «Python код во весь экран» ─────────────────────
    const modal      = document.getElementById('python-code-modal');
    const modalArea  = document.getElementById('python-code-modal-area');
    const sideArea   = document.getElementById('python-code');

    function activateModalEditor() {
      // Модальный редактор — textarea с overlay-подсветкой и автодополнением.
      const fb = document.getElementById('cm-modal-fallback');
      if (fb) fb.hidden = false;
      const fbArea = document.getElementById('python-code-modal-area');
      if (fbArea && sideArea) {
        fbArea.value = sideArea.value || '';
        // Прикрутим подсветку/Tab/автодополнение если ещё не было.
        if (!fbArea.dataset._wired) {
          fbArea.dataset._wired = '1';
          attachCodeHighlight('python-code-modal-area', 'python-code-modal-overlay');
          attachRobotAutocomplete('python-code-modal-area');
        }
        fbArea.focus();
      }
    }

    function openCodeModal() {
      if (!modal || !sideArea) return;
      modal.hidden = false;
      activateModalEditor();
      // Журнал в модалке: на открытии заливаем все существующие записи.
      const mainLog = document.getElementById('cmd-log');
      const modalJournal = document.getElementById('code-modal-journal');
      if (mainLog && modalJournal) {
        modalJournal.innerHTML = mainLog.innerHTML;
        modalJournal.scrollTop = modalJournal.scrollHeight;
      }
    }
    function closeCodeModal() {
      if (!modal || !sideArea) return;
      modal.hidden = true;
    }
    document.getElementById('btn-expand-python-code')?.addEventListener('click', openCodeModal);
    document.getElementById('btn-collapse-python-code')?.addEventListener('click', closeCodeModal);

    // ── Шестерёнка скорости визуализации ──────────────────────────────
    // Множитель применяется ТОЛЬКО к физике движения (см. session.py:
    // update_physics масштабирует dt × sim_speed, _sim_sleep делит паузы
    // между манёврами). Mission-таймер использует sim_clock — звёзды за
    // скорость считаются по виртуальным секундам, 4× не даёт бонуса.
    // Значение хранится в localStorage и при WS-reconnect присылается
    // серверу в ws.onopen.
    (function wireSimSpeed() {
      const STORAGE_KEY = 'vegarex.sim_speed';
      const VALID = [1, 1.5, 2, 4];
      let current = parseFloat(localStorage.getItem(STORAGE_KEY) || '1');
      if (!VALID.includes(current)) current = 1;

      const labels = document.querySelectorAll('[data-sim-speed-label]');
      const wraps  = document.querySelectorAll('.sim-speed-wrap');
      const menus  = [
        document.getElementById('sim-speed-menu'),
        document.getElementById('sim-speed-menu-modal'),
      ].filter(Boolean);
      const triggers = [
        document.getElementById('btn-sim-speed'),
        document.getElementById('btn-modal-sim-speed'),
      ].filter(Boolean);

      const fmt = v => (Number.isInteger(v) ? v + '×' : v + '×');
      const render = () => {
        labels.forEach(el => el.textContent = fmt(current));
        wraps.forEach(w => w.classList.toggle('sim-speed-wrap--active', current !== 1));
        menus.forEach(m => {
          m.querySelectorAll('.sim-speed-menu__item').forEach(it => {
            const v = parseFloat(it.dataset.simSpeed);
            it.classList.toggle('sim-speed-menu__item--active', v === current);
          });
        });
      };
      const closeAll = () => menus.forEach(m => m.hidden = true);

      triggers.forEach((btn, i) => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const menu = menus[i];
          if (!menu) return;
          const wasHidden = menu.hidden;
          closeAll();
          menu.hidden = !wasHidden;
        });
      });
      menus.forEach(menu => {
        menu.addEventListener('click', (e) => {
          const item = e.target.closest('[data-sim-speed]');
          if (!item) return;
          const v = parseFloat(item.dataset.simSpeed);
          if (!VALID.includes(v)) return;
          current = v;
          try { localStorage.setItem(STORAGE_KEY, String(v)); } catch (_) {}
          try {
            if (ws && ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'set_sim_speed', value: v }));
            }
          } catch (_) {}
          render();
          closeAll();
        });
      });
      // Закрытие при клике вне попапа / по Esc.
      document.addEventListener('click', (e) => {
        if (e.target.closest('.sim-speed-wrap')) return;
        closeAll();
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeAll();
      });

      render();
    })();

    // ── Toggle «на весь экран» — растягиваем модалку на 100% × 100% viewport
    // (убираем padding 2rem и max-width 1200px). Браузерный хром сохраняется —
    // это НЕ Fullscreen API. Сохраняем состояние в localStorage, чтобы
    // следующее открытие модалки помнило предпочтение пользователя.
    const btnFs = document.getElementById('btn-modal-fullscreen');
    if (btnFs && modal) {
      const KEY = 'cm:maximized';
      const applyMaxState = (maxed) => {
        modal.classList.toggle('code-modal--maximized', maxed);
        btnFs.textContent = maxed ? '🗗 Окно' : '⛶ Весь экран';
        btnFs.title = maxed
          ? 'Вернуть панель в обычный размер (с отступами и max-width)'
          : 'Растянуть панель на всю ширину и высоту экрана';
      };
      // Применяем сохранённое состояние при загрузке.
      applyMaxState(localStorage.getItem(KEY) === '1');
      btnFs.addEventListener('click', () => {
        const next = !modal.classList.contains('code-modal--maximized');
        applyMaxState(next);
        localStorage.setItem(KEY, next ? '1' : '0');
      });
    }

    // ── Shift+колесо мыши над журналом модалки — масштаб шрифта ──────
    // Независимо от кода. Min 10px, max 28px, сохраняется в localStorage.
    const modalJournalWrap = document.getElementById('code-modal-journal-wrap');
    if (modalJournalWrap) {
      const KEY = 'cm:journal-font';
      const saved = parseInt(localStorage.getItem(KEY) || '0', 10);
      if (saved >= 10 && saved <= 28) {
        modalJournalWrap.style.setProperty('--journal-font-size', saved + 'px');
      }
      modalJournalWrap.addEventListener('wheel', (e) => {
        if (!e.shiftKey) return;
        e.preventDefault();
        const body = document.getElementById('code-modal-journal');
        if (!body) return;
        const cur = parseInt(getComputedStyle(body).fontSize, 10) || 13;
        const delta = e.deltaY > 0 ? -1 : +1;
        const next = Math.max(10, Math.min(28, cur + delta));
        modalJournalWrap.style.setProperty('--journal-font-size', next + 'px');
        localStorage.setItem(KEY, String(next));
      }, {passive: false});
    }
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
    // Кнопки в модалке читают/пишут модальную textarea, зеркало — в боковую.
    function modalText() { return modalArea ? modalArea.value : ''; }
    function setModalText(t) {
      if (!modalArea) return;
      modalArea.value = t;
      modalArea.dispatchEvent(new Event('input', { bubbles: true }));  // overlay/draft refresh
    }
    document.getElementById('btn-modal-copy')?.addEventListener('click', () => {
      navigator.clipboard.writeText(modalText()).catch(() => {});
    });
    document.getElementById('btn-modal-clear')?.addEventListener('click', () => {
      if (confirm('Очистить весь код?')) {
        setModalText('');
        if (sideArea) sideArea.value = '';
        _clearCodeDraft();
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'clear_program' }));
        }
      }
    });
    document.getElementById('btn-modal-tidy')?.addEventListener('click', () => {
      // Открываем модалку «Сборка кода для робота». Сначала синкаем
      // модальный редактор → боковой textarea (источник правды для экспорта).
      if (sideArea) sideArea.value = modalText();
      openExportModal();
    });
    document.getElementById('btn-modal-run')?.addEventListener('click', () => {
      // Sync модал → side, потом запускаем (runPythonCode читает sideArea).
      if (sideArea) sideArea.value = modalText();
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

    // «⌒ Дуга…» — спросить угол (и опционально направление). По умолчанию
    // против часовой; «-N» или приставка «по часовой» = CW. Сервер кидает
    // через NLU intent «arc», который дёргает _run_arc(N, dir).
    // ⏸ Пауза / ▶ Продолжить (образовательная). Шлём WS-сообщение,
    // сервер сделает _do_program_pause/_do_program_resume и пришлёт
    // обновлённый s.program_paused в push_state.
    document.getElementById('btn-program-pause')?.addEventListener('click', () => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({type: 'program_pause'}));
      }
    });
    document.getElementById('btn-program-resume')?.addEventListener('click', () => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({type: 'program_resume'}));
      }
    });

    document.getElementById('btn-arc')?.addEventListener('click', () => {
      const raw = prompt(
        'Угол дуги в градусах.\n'
        + 'По умолчанию — ПРОТИВ часовой. Для дуги ПО часовой добавьте\n'
        + '«-» (например «-180») или префикс «по часовой» (например\n'
        + '«по часовой 90»). arc(360) = полный круг.',
        '90');
      if (raw === null) return;
      let v = raw.trim();
      if (!v) return;
      // Отрицательное число → «по часовой N»; знак убираем.
      if (/^-\d/.test(v)) {
        v = 'по часовой ' + v.slice(1);
      }
      sendCmd('Вега дуга ' + v);
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
        // Узнаём реальный следующий номер миссии — чтобы в подсказке
        // показать «Кастомная #6», а не плейсхолдер «#N».
        let hint = 'Кастомная';
        try {
          const r = await fetch('/missions/next_id');
          if (r.ok) {
            const d = await r.json();
            if (d && d.next_id != null) hint = 'Кастомная #' + d.next_id;
          }
        } catch (e) { /* подсказка не критична */ }
        const raw = window.prompt(
          'Название кастомной миссии:\n' +
          '(оставьте пустым — будет «' + hint + '»)',
          ''
        );
        if (raw === null) return;        // нажал Отмена — не сохраняем
        saveMission(raw.trim(), true);
      });

    // ── ⛯ Режим установки опасных зон мышью ───────────────────────────
    // Взаимоисключающий с «Инспектор» и «Осторожно» (server-side mutex):
    // только один из трёх режимов активен. Источник истины — серверный
    // флаг s.zone_mode, клиент только отрисовывает по push_state.
    const btnZoneMode = document.getElementById('btn-zone-mode');
    // Локальная UI-функция: применяет визуальное состояние (canvas,
    // подсветка кнопки, подвал-хинт). НЕ шлёт ничего на сервер.
    function applyZoneModeUI(on) {
      if (!canvas) return;
      canvas.setZoneMode(on);
      if (btnZoneMode) btnZoneMode.classList.toggle('is-active', on);
      if (on) {
        setStatus(`⛯ Обстановка  •  радиус${canvas.zoneRadius} см  •  ` +
                  `ЛКМ — поставить, ПКМ — удалить (опасные), ` +
                  `[+/-] — радиус, ESC — выход`, 'hint');
      } else {
        setStatus(null, 'hint:clear');
      }
    }
    // Toggle: отправляем серверу намерение. Сервер выставит s.zone_mode
    // и пришлёт state-обновление, по нему applyZoneModeUI отрендерит.
    function requestZoneModeToggle(on) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'set_zone_mode', active: !!on }));
      }
    }
    if (btnZoneMode && canvas) {
      btnZoneMode.addEventListener('click', () => {
        requestZoneModeToggle(!canvas.zoneMode);
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
          setStatus(`⛯ Обстановка  •  радиус${r} см  •  ` +
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
          requestZoneModeToggle(false);
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

    // «🏁 Проверка» в шапке журнала — фиксирует результат активной миссии.
    const btnFinalize = document.getElementById('btn-mission-finalize');
    if (btnFinalize) {
      btnFinalize.addEventListener('click', _finalizeMission);
    }

    // «📂 Загрузить» — пикер маршрутов из Хранилища (свои + опубликованные).
    // Кнопка есть и в боковой панели, и в развёрнутом редакторе кода.
    ['btn-load-route', 'btn-modal-load-route'].forEach(id => {
      const b = document.getElementById(id);
      if (b) b.addEventListener('click', _openLoadRouteModal);
    });

    // «Пульт управления» — кнопка показывает/скрывает команды. По
    // умолчанию свёрнут; состояние запоминается в localStorage.
    const btnToggleCtrl = document.getElementById('btn-toggle-ctrl');
    const ctrlBody      = document.getElementById('ctrl-body');
    if (btnToggleCtrl && ctrlBody) {
      const CTRL_KEY = 'vegarex.ctrl_expanded';
      const applyCtrl = (expanded) => {
        ctrlBody.style.display = expanded ? '' : 'none';
        btnToggleCtrl.title = expanded
          ? 'Скрыть команды пульта управления'
          : 'Показать команды пульта управления';
      };
      let ctrlExpanded = false;   // по умолчанию — свёрнут
      try { ctrlExpanded = localStorage.getItem(CTRL_KEY) === '1'; }
      catch (e) {}
      applyCtrl(ctrlExpanded);
      btnToggleCtrl.addEventListener('click', () => {
        ctrlExpanded = ctrlBody.style.display === 'none';
        applyCtrl(ctrlExpanded);
        try { localStorage.setItem(CTRL_KEY, ctrlExpanded ? '1' : '0'); }
        catch (e) {}
      });
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
