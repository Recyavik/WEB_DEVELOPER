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
    ta.addEventListener('input',  syncContent);
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

    // Взаимоисключающая подсветка кнопок «Инспектор» / «Осторожно».
    const btnInsp = document.getElementById('btn-mode-inspector');
    const btnCaut = document.getElementById('btn-mode-cautious');
    if (btnInsp) btnInsp.classList.toggle('is-active', !cautious);
    if (btnCaut) btnCaut.classList.toggle('is-active',  cautious);

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
    // Очистка идет через сервер — он перешлет обновленный текст программы (пустой + шапка).
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
               `ЛКМ ставит, ПКМ удаляет, [+/-] меняет радиус, ESC выход.`, 'info');
      } else {
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
        sendCmd(`Вега убрать зону ${wx.toFixed(0)} ${wy.toFixed(0)}`);
      };
      canvas.onZoneRadiusChange = (r) => {
        logMsg(`⛯ Радиус зоны: ${r} см.`, 'info');
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
