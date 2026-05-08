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
    };

    ws.onmessage = e => {
      try {
        const msg = JSON.parse(e.data);
        handleWsMsg(msg);
      } catch (_) {}
    };

    ws.onclose = () => {
      logMsg('Соединение потеряно. Переподключение…', 'warning');
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
          if (textarea) textarea.value = msg.text;
          // Если модалка открыта — тоже обновляем
          const modal     = document.getElementById('python-code-modal');
          const modalArea = document.getElementById('python-code-modal-area');
          if (modal && !modal.hidden && modalArea) modalArea.value = msg.text;
        }
        break;

      case 'code_exec_result':
        logMsg(msg.output || 'Выполнено.', 'info');
        break;

      case 'pong':
        break;
    }
  }

  // ── Python код команды ─────────────────────────────────────────────────────

  function copyPythonCode() {
    const textarea = document.getElementById('python-code');
    if (!textarea) return;
    navigator.clipboard.writeText(textarea.value).catch(() => {});
  }

  // ── Подсветка Python-кода (комментарии — зелёным) ─────────────────────────
  function escHtmlCode(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function highlightPython(text) {
    // Простой однопроходный лексер: находим '#' вне строк и красим хвост строки.
    return text.split('\n').map(line => {
      let inStr = false, strCh = null, commentIdx = -1;
      for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (inStr) {
          if (ch === strCh && line[i - 1] !== '\\') {
            inStr = false; strCh = null;
          }
        } else {
          if (ch === '"' || ch === "'") { inStr = true; strCh = ch; }
          else if (ch === '#') { commentIdx = i; break; }
        }
      }
      if (commentIdx >= 0) {
        return escHtmlCode(line.slice(0, commentIdx)) +
               '<span class="hl-comment">' +
               escHtmlCode(line.slice(commentIdx)) +
               '</span>';
      }
      return escHtmlCode(line);
    }).join('\n') + '\n';   // финальный \n чтобы overlay не «съедал» нижнюю строку
  }
  function attachCodeHighlight(textareaId, overlayId) {
    const ta = document.getElementById(textareaId);
    const ov = document.getElementById(overlayId);
    if (!ta || !ov) return;
    const sync = () => {
      ov.innerHTML = highlightPython(ta.value || '');
      // Sync scroll
      ov.parentElement.scrollTop = ta.scrollTop;
      ov.scrollTop = ta.scrollTop;
      ov.scrollLeft = ta.scrollLeft;
    };
    ta.addEventListener('input',  sync);
    ta.addEventListener('scroll', () => {
      ov.scrollTop  = ta.scrollTop;
      ov.scrollLeft = ta.scrollLeft;
    });
    // Первоначальная подсветка + наблюдение за программными изменениями value
    sync();
    // Периодически проверяем, не изменился ли value «программно»
    // (например, через msg.code → appendPythonCode). input event для value
    // через .value = ... не выстреливает, так что нужен polling.
    let lastVal = ta.value;
    setInterval(() => {
      if (ta.value !== lastVal) {
        lastVal = ta.value;
        sync();
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
    // Показываем фактическую скорость с учётом просадки от заряда батареи.
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
    //   ≥ 60% — зелёный, 30–60% — жёлто-зелёный, 15–30% — оранжевый, < 15% — красный
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

    // Взаимоисключающая подсветка кнопок «Инспектор» / «Осторожно».
    const btnInsp = document.getElementById('btn-mode-inspector');
    const btnCaut = document.getElementById('btn-mode-cautious');
    if (btnInsp) btnInsp.classList.toggle('is-active', !cautious);
    if (btnCaut) btnCaut.classList.toggle('is-active',  cautious);

    // Дублирующий бейдж больше не нужен — режим уже виден в строке «Режим».
    const caut = document.getElementById('caution-badge');
    if (caut) caut.style.display = 'none';

    // 🖥 «Робот думает» (планировщик в режиме «осторожно»).
    const thBadge = document.getElementById('thinking-badge');
    if (thBadge) {
      const th = s.thinking || 'idle';
      if (th === 'idle') {
        thBadge.style.display = 'none';
      } else {
        thBadge.style.display = '';
        thBadge.classList.toggle('thinking-badge--failed', th === 'failed');
        const txt = thBadge.querySelector('.thinking-badge__text');
        if (txt) txt.textContent = (th === 'failed')
          ? 'Решение не найдено'
          : 'Робот думает…';
      }
    }
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
    if (!log) return;

    const now  = new Date();
    const time = `${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}:${now.getSeconds().toString().padStart(2,'0')}`;

    const entry = document.createElement('div');
    entry.className = `log-entry log-entry--${level}`;
    entry.innerHTML = `<span class="log-time">${time}</span>${escHtml(text)}`;
    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;

    if (log.children.length > 200) log.children[0].remove();
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
      textarea.value = code; // first command: set full code including preamble
      return;
    }

    // Subsequent commands: append only the body (command-specific lines)
    const body = extractCodeBody(code);
    if (!body.trim()) return;

    const existingDefs = Array.from(existing.matchAll(/^def\s+(cmd_[a-zA-Z0-9_]+)\s*\(/gm))
      .map(m => m[1]);

    // Split body into def-blocks and plain lines
    const bodyLines = body.split('\n');
    let merged = existing.trimEnd();
    let inDef = false;
    let defName = null;
    let defLines = [];

    function flushDef() {
      if (!defName) return;
      if (!existingDefs.includes(defName)) {
        merged += '\n\n' + defLines.join('\n');
        existingDefs.push(defName);
      }
      defName = null;
      defLines = [];
    }

    for (const line of bodyLines) {
      const defMatch = line.match(/^def\s+(cmd_[a-zA-Z0-9_]+)\s*\(/);
      if (defMatch) {
        flushDef();
        inDef = true;
        defName = defMatch[1];
        defLines = [line];
      } else if (inDef) {
        if (line.trim() === '' && defLines.length) {
          defLines.push(line);
          flushDef();
          inDef = false;
        } else {
          defLines.push(line);
        }
      } else {
        const trimmed = line.trim();
        if (!trimmed) continue; // skip blank separators between commands
        merged += '\n' + trimmed; // always append plain command lines
      }
    }
    flushDef(); // flush any trailing def without trailing blank line

    textarea.value = merged;
    // Если модалка открыта — отражаем изменения и в ней
    const modal     = document.getElementById('python-code-modal');
    const modalArea = document.getElementById('python-code-modal-area');
    if (modal && !modal.hidden && modalArea) modalArea.value = merged;
  }

  function runPythonCode() {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      logMsg('Нет соединения!', 'error');
      return;
    }
    const textarea = document.getElementById('python-code');
    if (!textarea) return;
    ws.send(JSON.stringify({ type: 'run_python_code', code: textarea.value }));
    logMsg('▶ Выполняется Python-код…', 'info');
  }

  function clearPythonCode() {
    // Очистка идёт через сервер — он перешлёт обновлённый текст программы (пустой + шапка).
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'clear_program' }));
    } else {
      const textarea = document.getElementById('python-code');
      if (textarea) textarea.value = '';
    }
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

    const btnClearPath = document.getElementById('btn-clear-path');
    if (btnClearPath) btnClearPath.addEventListener('click', () => sendCmd('Вега сброс'));

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
      // Передаём серверу — чтобы физика тоже учитывала отключение датчика.
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
      modalArea.focus();
    }
    function closeCodeModal() {
      if (!modal || !modalArea || !sideArea) return;
      sideArea.value = modalArea.value;     // правки из модалки → в боковую
      modal.hidden = true;
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
    // Клик по фону модалки — закрывает (но не на сам редактор и не внутри панели)
    modal?.addEventListener('click', (e) => {
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
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'clear_program' }));
        }
      }
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

    // «📍 Установить зону…» — спросить координаты + (опц.) радиус
    document.getElementById('btn-set-zone')?.addEventListener('click', () => {
      const xy = prompt(
        'Координаты алгоритмической зоны (X Y [радиус_см]),\n' +
        'пример: 100 -50 30',
        '100 100');
      if (xy === null) return;
      const cleaned = xy.trim();
      if (!cleaned) return;
      sendCmd('Вега установи зону ' + cleaned);
    });

    // «⚠ Зона…» — поставить красную зону обстановки В КООРДИНАТЕ (без движения).
    document.getElementById('btn-mark-danger')?.addEventListener('click', () => {
      const xy = prompt(
        'Координаты опасной (красной) зоны обстановки (X Y [радиус_см]).\n' +
        'Робот не поедет туда — зона рисуется на карте как «константа обстановки».\n' +
        'Пусто — поставить под роботом.\n' +
        'Пример: -100 50 30',
        '0 0');
      if (xy === null) return;
      const cleaned = xy.trim();
      if (cleaned) sendCmd('Вега опасная зона ' + cleaned);
      else         sendCmd('Вега опасная зона');
    });

    // «✕ Убрать зону…» — спросить координаты (пусто = текущая позиция робота)
    document.getElementById('btn-remove-zone')?.addEventListener('click', () => {
      const xy = prompt(
        'Координаты точки, в которой убрать зону (X Y).\n' +
        'Оставьте пустым — снимется зона под роботом.',
        '');
      if (xy === null) return;
      const cleaned = xy.trim();
      if (cleaned) sendCmd('Вега убрать зону ' + cleaned);
      else         sendCmd('Вега убрать зону');
    });

    // Очистить журнал
    const btnClearLog = document.getElementById('btn-clear-log');
    if (btnClearLog) {
      btnClearLog.addEventListener('click', () => {
        const log = document.getElementById('cmd-log');
        if (log) log.innerHTML = '';
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
