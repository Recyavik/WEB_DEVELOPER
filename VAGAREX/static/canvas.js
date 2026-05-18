/**
 * canvas.js — визуализация робота на HTML5 Canvas
 *
 * Координатная система: робот начинает в центре, X вправо, Y вверх.
 * Canvas: пиксели, начало в левом верхнем углу — конвертируем worldToCanvas().
 */

class RobotCanvas {
  constructor(canvasEl) {
    this.canvas = canvasEl;
    this.ctx    = canvasEl.getContext('2d');

    // Мировые параметры
    this.worldW        = 500;  // см
    this.worldH        = 500;  // см
    this.wallThickness = 5;    // см
    this.sensorType    = 'laser';
    this.sonarConeDeg  = 30;

    // Анимация эхолокатора
    this._sonarLastDist  = -1;
    this._sonarPulseTime = 0;
    this._sonarAnimating = false;

    // Световая индикация (один индикатор по центру корпуса)
    this._lastSpeed        = 0;
    this._brakeFlashUntil  = 0;     // ms timestamp когда красная вспышка должна погаснуть
    this._lastLightSig     = '';    // сигнатура последнего отрисованного состояния
    this._lightsTickerOn   = false;
    this.robotLengthCm     = 20;
    this.robotWidthCm      = 12;

    // Просмотр (pan + zoom)
    this.scale    = 1.2;   // пикселей на см
    this.originX  = 0;     // пиксели (смещение центра холста)
    this.originY  = 0;

    // Данные
    this.robotState  = { x: 0, y: 0, heading: 0, speed: 0, steer: 0, dist_left: 0, laser_dist: 0, cautious: false, mode: 'normal' };
    this.dangerZones  = [];
    this.pathHistory  = [];
    this.autoSegments = [];

    // Настройки отображения
    this.showGrid  = true;
    this.showLaser = true;
    this.showPath  = true;

    // ── Режим установки опасных зон мышью ────────────────────────────
    // Включается извне через setZoneMode(true), выход — ESC или повторное
    // нажатие кнопки. В режиме:
    //   ЛКМ      — поставить опасную зону
    //   ПКМ      — удалить зону, в которую попадает курсор
    //   +/−      — менять радиус (Shift+колесо тоже)
    //   ESC      — выход
    this.zoneMode        = false;
    this.zoneRadius      = 10;     // см, дефолт
    this.zoneRadiusMin   = 5;
    this.zoneRadiusMax   = 40;
    this.zoneRadiusStep  = 5;
    this.zoneCursor      = null;   // {wx, wy} текущая позиция мыши в мире (или null)
    this.zoneInsideField = false;  // курсор внутри игрового поля?
    // Внешние коллбеки — устанавливаются control.js:
    this.onZonePlace     = null;   // (wx, wy, radius) — ЛКМ
    this.onZoneRemove    = null;   // (wx, wy)         — ПКМ
    this.onZoneRadiusChange = null;// (radius)         — +/- / Shift+колесо

    // Активная миссия (если есть) — для overlay-отрисовки:
    //   - mission.path        — серый пунктир эталонной траектории
    //   - mission.waypoints   — точки-чекпоинты (с номерами)
    //   - mission.danger_zones — красные dashed-круги (предзаданы)
    //   - mission.actions     — обязательные действия с зонами
    //   - mission.progress    — состояние прохождения (visited indices)
    this.mission         = null;
    this.missionProgress = null;

    this._resize();
    this._bindEvents();
  }

  // Активация/деактивация overlay миссии.
  setMission(mission) {
    this.mission = mission || null;
    this.missionProgress = mission ? (mission.progress || null) : null;
    this.draw();
  }
  updateMissionProgress(progress) {
    this.missionProgress = progress || null;
    this.draw();
  }

  // ── Изменение размера ──────────────────────────────────────────────────────

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width  = rect.width  * dpr;
    this.canvas.height = rect.height * dpr;
    this.ctx.scale(dpr, dpr);
    this._cssW = rect.width;
    this._cssH = rect.height;
    this.centerView();
  }

  centerView() {
    this.originX = this._cssW / 2;
    this.originY = this._cssH / 2;
    this.draw();
  }

  // ── Координатные преобразования ────────────────────────────────────────────

  worldToCanvas(wx, wy) {
    return {
      x: this.originX + wx * this.scale,
      y: this.originY - wy * this.scale,
    };
  }

  canvasToWorld(cx, cy) {
    return {
      x: (cx - this.originX) / this.scale,
      y: -(cy - this.originY) / this.scale,
    };
  }

  // ── Events ─────────────────────────────────────────────────────────────────

  _bindEvents() {
    let panning = false, lastMX = 0, lastMY = 0;

    const getMouseWorld = (e) => {
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      return { mx, my, ...this.canvasToWorld(mx, my) };
    };
    const isInsideField = (wx, wy) => {
      const hw = this.worldW / 2, hh = this.worldH / 2;
      return Math.abs(wx) <= hw && Math.abs(wy) <= hh;
    };

    this.canvas.addEventListener('mousedown', e => {
      // ── Режим установки зон — приоритет над панорамированием ────
      if (this.zoneMode) {
        const { x: wx, y: wy } = getMouseWorld(e);
        if (e.button === 0) {                     // ЛКМ — поставить
          e.preventDefault();
          if (isInsideField(wx, wy)) {
            if (this.onZonePlace) this.onZonePlace(wx, wy, this.zoneRadius);
          }
          return;
        }
        if (e.button === 2) {                     // ПКМ — удалить под курсором
          e.preventDefault();
          if (this.onZoneRemove) this.onZoneRemove(wx, wy);
          return;
        }
      }
      // Обычное панорамирование (ЛКМ, не в режиме зон)
      if (e.button === 0) {
        panning = true;
        lastMX = e.clientX; lastMY = e.clientY;
      }
    });
    this.canvas.addEventListener('mousemove', e => {
      if (this.zoneMode) {
        const { x: wx, y: wy } = getMouseWorld(e);
        this.zoneCursor = { wx, wy };
        this.zoneInsideField = isInsideField(wx, wy);
        this.draw();
        return;
      }
      if (!panning) return;
      this.originX += e.clientX - lastMX;
      this.originY += e.clientY - lastMY;
      lastMX = e.clientX; lastMY = e.clientY;
      this.draw();
    });
    this.canvas.addEventListener('mouseup',   () => { panning = false; });
    this.canvas.addEventListener('mouseleave',() => {
      panning = false;
      if (this.zoneMode) {
        this.zoneCursor = null;
        this.draw();
      }
    });

    // В режиме зон — подавляем стандартное контекстное меню браузера,
    // потому что ПКМ используется для удаления зоны (см. mousedown).
    this.canvas.addEventListener('contextmenu', e => {
      if (this.zoneMode) e.preventDefault();
    });

    this.canvas.addEventListener('wheel', e => {
      e.preventDefault();
      // В режиме зон + Shift — меняет радиус
      if (this.zoneMode && e.shiftKey) {
        const delta = e.deltaY < 0 ? +this.zoneRadiusStep : -this.zoneRadiusStep;
        this.changeZoneRadius(delta);
        return;
      }
      // Обычный зум
      const factor = e.deltaY < 0 ? 1.1 : 0.9;
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      this.originX = mx + (this.originX - mx) * factor;
      this.originY = my + (this.originY - my) * factor;
      this.scale  *= factor;
      this.draw();
    }, { passive: false });

    // ResizeObserver точнее window.resize — срабатывает после layout
    if (window.ResizeObserver) {
      new ResizeObserver(() => this._resize()).observe(this.canvas);
    } else {
      window.addEventListener('resize', () => this._resize());
    }
  }

  // ── Публичный API для режима установки зон ───────────────────────────
  setZoneMode(on) {
    this.zoneMode = !!on;
    this.canvas.style.cursor = this.zoneMode ? 'crosshair' : '';
    if (!this.zoneMode) this.zoneCursor = null;
    this.draw();
  }
  changeZoneRadius(delta) {
    const r = Math.max(this.zoneRadiusMin,
              Math.min(this.zoneRadiusMax, this.zoneRadius + delta));
    if (r === this.zoneRadius) return;
    this.zoneRadius = r;
    if (this.onZoneRadiusChange) this.onZoneRadiusChange(r);
    this.draw();
  }

  // ── Обновление данных ──────────────────────────────────────────────────────

  update(robotState, world) {
    this.robotState    = robotState;
    this.dangerZones   = world.danger_zones   || [];
    this.pathHistory   = world.path_history   || [];
    this.autoSegments  = world.auto_segments  || [];
    this.worldW        = world.width          || 500;
    this.worldH        = world.height         || 500;
    this.wallThickness = world.wall_thickness || 5;
    this.sensorType    = world.sensor_type    || 'laser';
    this.sonarConeDeg  = world.sonar_cone_deg || 30;
    this.startX        = world.start_x_cm        ?? 0;
    this.startY        = world.start_y_cm        ?? 0;
    this.startHeading  = world.start_heading_deg ?? 0;
    this.robotLengthCm = world.robot_length_cm   ?? this.robotLengthCm;
    this.robotWidthCm  = world.robot_width_cm    ?? this.robotWidthCm;
    this._noteSpeedTransition(robotState);
    this._startLightsTicker();
    this._lastLightSig = this._lightSignature();
    this.draw();
  }

  updateRobot(robotState) {
    this._noteSpeedTransition(robotState);
    this.robotState = robotState;
    this._startLightsTicker();
    this._lastLightSig = this._lightSignature();
    this.draw();
  }

  updateWorld(world) {
    this.dangerZones   = world.danger_zones   || [];
    this.pathHistory   = world.path_history   || [];
    this.autoSegments  = world.auto_segments  || [];
    this.worldW        = world.width          || this.worldW;
    this.worldH        = world.height         || this.worldH;
    this.wallThickness = world.wall_thickness || this.wallThickness;
    this.sensorType    = world.sensor_type    || 'laser';
    this.sonarConeDeg  = world.sonar_cone_deg || 30;
    if (world.start_x_cm        !== undefined) this.startX       = world.start_x_cm;
    if (world.start_y_cm        !== undefined) this.startY       = world.start_y_cm;
    if (world.start_heading_deg !== undefined) this.startHeading = world.start_heading_deg;
    if (world.robot_length_cm   !== undefined) this.robotLengthCm = world.robot_length_cm;
    if (world.robot_width_cm    !== undefined) this.robotWidthCm  = world.robot_width_cm;
    this.draw();
  }

  _noteSpeedTransition(newState) {
    if (!newState) return;
    const wasMoving = Math.abs(this._lastSpeed) > 0.5;
    const nowMoving = Math.abs(newState.speed) > 0.5;
    if (wasMoving && !nowMoving) {
      this._brakeFlashUntil = Date.now() + 700; // короткая красная вспышка ~0.7 с
    }
    this._lastSpeed = newState.speed || 0;
  }

  // Сигнатура текущего желаемого состояния индикатора. Если она меняется —
  // перерисовываем. Если не меняется — экономим кадры.
  _lightSignature() {
    const s = this.robotState;
    if (!s) return 'none';
    const now = Date.now();
    if (now < this._brakeFlashUntil) {
      // фаза затухания, шаг ~100мс
      return 'red:' + Math.floor((this._brakeFlashUntil - now) / 100);
    }
    const steer = s.steer || 0;
    if (steer !== 0) {
      // мигание 250 мс on / 250 мс off
      return 'yellow:' + (Math.floor(now / 250) % 2);
    }
    if (Math.abs(s.speed || 0) < 0.5) return 'green';
    return 'off';
  }

  _startLightsTicker() {
    if (this._lightsTickerOn) return;
    this._lightsTickerOn = true;
    const tick = () => {
      const sig = this._lightSignature();
      if (sig !== this._lastLightSig) {
        this._lastLightSig = sig;
        this.draw();
      }
      setTimeout(tick, 80); // 80 мс — достаточно частый опрос для глаза
    };
    setTimeout(tick, 80);
  }

  // ── Рисование ─────────────────────────────────────────────────────────────

  draw() {
    const ctx = this.ctx;
    const W   = this._cssW;
    const H   = this._cssH;

    ctx.clearRect(0, 0, W, H);

    // Фон
    ctx.fillStyle = '#0d1117';
    ctx.fillRect(0, 0, W, H);

    if (this.showGrid)  this._drawGrid();

    this._drawAxes();
    this._drawWalls();
    this._drawStartPoint();
    // Mission overlay рисуем ПОД обычными зонами/path, чтобы реальный
    // путь робота и его зоны были видны поверх «эталона миссии».
    if (this.mission) this._drawMissionOverlay();
    this._drawDangerZones();
    if (this.showPath) this._drawAutoSegments();   // фиолетовый план — поверх зон, под пройденным следом
    if (this.showPath) this._drawPath();
    if (this.showLaser) this._drawLaser();
    this._drawRobot();
    this._drawLights();
    // Поверх всего — превью зоны под курсором + статус-плашка режима
    this._drawZoneModeOverlay();
  }

  _drawMissionOverlay() {
    const ctx = this.ctx;
    const m   = this.mission;
    const p   = this.missionProgress || {};
    const visited = new Set(p.waypoints_visited || []);

    // 1) Серая пунктирная траектория-эталон.
    const path = m.path || [];
    if (path.length > 1) {
      ctx.save();
      ctx.beginPath();
      const p0 = this.worldToCanvas(path[0][0], path[0][1]);
      ctx.moveTo(p0.x, p0.y);
      for (let i = 1; i < path.length; i++) {
        const pi = this.worldToCanvas(path[i][0], path[i][1]);
        ctx.lineTo(pi.x, pi.y);
      }
      ctx.strokeStyle = 'rgba(150,150,150,0.55)';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
    }

    // 2) Опасные зоны миссии НЕ рисуем здесь: при старте миссии
    //    start_mission кладёт их как настоящие зоны в world, и их уже
    //    рисует _drawZones (красные пронумерованные). Рисовать их ещё
    //    раз тут — дублирование (две концентричные окружности).

    // 3) Зоны внимания из задания — ЦЕЛЬ «установить зону»: жёлтый
    //    пунктирный контур + штриховка. Когда игрок установил зону
    //    правильно (action засчитан) — цель-оверлей убираем СОВСЕМ:
    //    штриховка исчезает, на поле остаётся только реальная жёлтая
    //    зона игрока (её рисует _drawDangerZones). Так нет путаницы
    //    «две окружности».
    (m.actions || []).forEach((a, idx) => {
      if (a.type !== 'place_attention') return;
      if ((p.actions_done || []).includes(idx)) return;   // установлено — цель убрана
      const c = this.worldToCanvas(a.x, a.y);
      const r = (a.r || 15) * this.scale;
      ctx.save();
      ctx.strokeStyle = '#ffd700';
      ctx.lineWidth = 1.4;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      // Диагональная штриховка — «здесь нужно установить зону внимания».
      this._hatchCircle(c.x, c.y, r);
      ctx.restore();
    });

    // 4) Маркеры контрольных точек: серые → зелёные при посещении.
    (m.waypoints || []).forEach(([x, y], i) => {
      const c = this.worldToCanvas(x, y);
      const ok = visited.has(i);
      ctx.save();
      ctx.beginPath();
      ctx.arc(c.x, c.y, 7, 0, Math.PI * 2);
      ctx.fillStyle = ok ? '#2ea043' : '#1c2330';
      ctx.fill();
      ctx.strokeStyle = ok ? '#3fb950' : '#8b949e';
      ctx.lineWidth = 1.5;
      ctx.stroke();
      // Номер точки
      ctx.fillStyle = ok ? '#fff' : '#c9d1d9';
      ctx.font = 'bold 10px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(String(i + 1), c.x, c.y);
      // Координаты — серым полупрозрачным под маркером,
      // чтобы не нужно было каждый раз открывать «условие задачи».
      ctx.fillStyle = 'rgba(150, 162, 175, 0.7)';
      ctx.font = '9px sans-serif';
      ctx.textBaseline = 'top';
      ctx.fillText(`(${Math.round(x)}, ${Math.round(y)})`, c.x, c.y + 10);
      ctx.restore();
    });

    // 5) Прогресс-плашка сверху холста.
    this._drawMissionProgressChip();
  }

  _drawMissionProgressChip() {
    const ctx = this.ctx;
    const m   = this.mission;
    const p   = this.missionProgress || {};
    const stars = p.stars_now ?? 0;
    const wpDone = (p.waypoints_visited || []).length;
    const wpTotal = (m.waypoints || []).length;
    const ac = p.action_counts || {};
    const quality = Math.round((p.quality ?? 0) * 100);
    const precision = Math.round((p.coefficient ?? 1) * 100);
    let text = `⭐ ${stars}  ·  точки ${wpDone}/${wpTotal}`;
    if (ac.place_total)
      text += `  ·  установлено ${ac.place_done || 0}/${ac.place_total}`;
    if (ac.remove_total)
      text += `  ·  удалено ${ac.remove_done || 0}/${ac.remove_total}`;
    text += `  ·  качество ${quality}%  ·  точность ${precision}%`;
    if (!p.in_margin && p.in_margin !== undefined) text += '  ·  ⚠ отклонение';

    ctx.save();
    ctx.font = 'bold 12px sans-serif';
    const w = ctx.measureText(text).width + 18;
    const h = 22;
    const x = (this._cssW - w) / 2;
    const y = 8;
    ctx.fillStyle = 'rgba(13, 17, 23, 0.88)';
    ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = (p.complete) ? '#2ea043'
                    : (!p.in_margin && p.in_margin !== undefined) ? '#f85149'
                    : '#30363d';
    ctx.lineWidth = 1;
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = '#c9d1d9';
    ctx.fillText(text, x + 9, y + 15);
    ctx.restore();
  }

  _drawLights() {
    const s = this.robotState;
    if (!s) return;
    const ctx = this.ctx;
    const now = Date.now();

    const Lcm = this.robotLengthCm || 20;
    const rad = (s.heading || 0) * Math.PI / 180;

    // Один индикатор по центру корпуса (в мировых координатах: nose - L/2 * forward)
    const pos = this.worldToCanvas(s.x, s.y);
    const cx  = pos.x - (Lcm / 2) * this.scale * Math.sin(rad);
    const cy  = pos.y + (Lcm / 2) * this.scale * Math.cos(rad);

    const inBrakeFlash = now < this._brakeFlashUntil;
    const isMoving     = Math.abs(s.speed || 0) > 0.5;
    const steer        = s.steer || 0;
    const blinkOn      = Math.floor(now / 250) % 2 === 0; // 500 мс цикл

    let color = null;

    if (inBrakeFlash) {
      // Красный, плавно затухает к концу окна
      const remaining = (this._brakeFlashUntil - now) / 700;
      const alpha = Math.max(0.3, remaining);
      color = {
        fill: `rgba(255,59,48,${alpha.toFixed(2)})`,
        glow: `rgba(255,59,48,${(alpha * 0.5).toFixed(2)})`,
      };
    } else if (steer !== 0) {
      // Желтый поворотник, мигает
      if (blinkOn) color = { fill: '#ffcc00', glow: 'rgba(255,204,0,0.55)' };
    } else if (!isMoving) {
      // Зеленый — готов выполнять команду
      color = { fill: '#34c759', glow: 'rgba(52,199,89,0.4)' };
    }
    // иначе (едет прямо) — индикатор погашен

    if (!color) return;

    const lampR = Math.max(3.5, Math.min(7, 2.2 * this.scale));

    // Внешнее свечение
    ctx.beginPath();
    ctx.arc(cx, cy, lampR * 2.4, 0, Math.PI * 2);
    ctx.fillStyle = color.glow;
    ctx.fill();

    // Лампа
    ctx.beginPath();
    ctx.arc(cx, cy, lampR, 0, Math.PI * 2);
    ctx.fillStyle = color.fill;
    ctx.fill();
  }

  _drawStartPoint() {
    if (this.startX === undefined || this.startY === undefined) return;
    if (this.startX === 0 && this.startY === 0 && (this.startHeading || 0) === 0) return; // совпадает с началом координат — не дублируем
    const ctx = this.ctx;
    const p = this.worldToCanvas(this.startX, this.startY);
    const rad = (this.startHeading || 0) * Math.PI / 180;
    const r = 7;

    // Зеленый круг
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(46, 160, 67, 0.9)';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.fillStyle = 'rgba(46, 160, 67, 0.18)';
    ctx.fill();

    // Стрелка курса
    const ex = p.x + Math.sin(rad) * (r + 6);
    const ey = p.y - Math.cos(rad) * (r + 6);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    ctx.lineTo(ex, ey);
    ctx.strokeStyle = 'rgba(46, 160, 67, 0.9)';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Подпись
    ctx.fillStyle = 'rgba(46, 160, 67, 0.85)';
    ctx.font = '10px sans-serif';
    ctx.fillText('старт', p.x + r + 2, p.y - r - 2);
  }

  _drawWalls() {
    const ctx   = this.ctx;
    const halfW = this.worldW / 2;
    const halfH = this.worldH / 2;
    const p1 = this.worldToCanvas(-halfW, -halfH);
    const p2 = this.worldToCanvas( halfW, -halfH);
    const p3 = this.worldToCanvas( halfW,  halfH);
    const p4 = this.worldToCanvas(-halfW,  halfH);

    const wallPx = Math.max(2, this.wallThickness * this.scale);
    ctx.strokeStyle = 'rgba(220,220,220,0.95)';
    ctx.lineWidth   = wallPx;
    ctx.lineJoin    = 'miter';
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.lineTo(p3.x, p3.y);
    ctx.lineTo(p4.x, p4.y);
    ctx.closePath();
    ctx.stroke();
  }

  _drawGrid() {
    const ctx  = this.ctx;
    const step = 50 * this.scale;  // 50 см
    if (step < 5) return;          // слишком мелко — не рисуем

    ctx.strokeStyle = 'rgba(48,54,61,0.8)';
    ctx.lineWidth   = 0.5;

    const W = this._cssW, H = this._cssH;

    // Вертикальные линии
    const startX = ((this.originX % step) + step) % step;
    for (let x = startX; x < W; x += step) {
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }
    // Горизонтальные линии
    const startY = ((this.originY % step) + step) % step;
    for (let y = startY; y < H; y += step) {
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }

    // Метки сетки (мельче и прозрачней — не должны конкурировать с
    // координатами waypoints и трассой)
    ctx.fillStyle = 'rgba(139,148,158,0.28)';
    ctx.font = '9px monospace';
    for (let x = startX; x < W; x += step) {
      const wx = Math.round((x - this.originX) / this.scale);
      ctx.fillText(wx, x + 2, this.originY - 3);
    }
    for (let y = startY; y < H; y += step) {
      const wy = Math.round(-(y - this.originY) / this.scale);
      ctx.fillText(wy, this.originX + 3, y - 3);
    }
  }

  _drawAxes() {
    const ctx = this.ctx;
    const W = this._cssW, H = this._cssH;
    const ox = this.originX, oy = this.originY;

    ctx.strokeStyle = 'rgba(88,166,255,0.3)';
    ctx.lineWidth   = 1;

    // X ось
    ctx.beginPath(); ctx.moveTo(0, oy); ctx.lineTo(W, oy); ctx.stroke();
    // Y ось
    ctx.beginPath(); ctx.moveTo(ox, 0); ctx.lineTo(ox, H); ctx.stroke();

    // Метки осей
    ctx.fillStyle = 'rgba(88,166,255,0.6)';
    ctx.font = '11px sans-serif';
    ctx.fillText('X', W - 15, oy - 5);
    ctx.fillText('Y', ox + 5, 15);

    // Начало координат
    ctx.beginPath();
    ctx.arc(ox, oy, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#58a6ff';
    ctx.fill();
  }

  // Диагональная штриховка внутри круга радиуса r (контур рисует
  // вызывающий). Цвет — текущий ctx.strokeStyle.
  _hatchCircle(cx, cy, r) {
    const ctx = this.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.clip();
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.28;        // полупрозрачно — штриховка не «кричит»
    ctx.setLineDash([]);
    // Линии под 45°; off — сдвиг диагонали. Диагональный размах круга
    // — r·√2 ≈ 1.41r, берём ±1.5r с запасом, чтобы заштриховать его
    // целиком (не «до половины»). Шаг 11 px — не слишком плотно.
    for (let off = -1.5 * r; off <= 1.5 * r; off += 11) {
      ctx.beginPath();
      ctx.moveTo(cx - r + off, cy - r);
      ctx.lineTo(cx + r + off, cy + r);
      ctx.stroke();
    }
    ctx.restore();
  }

  // Опасная зона — задача «удалить» (есть парный remove_danger в миссии)?
  // Такие зоны не штрафуются: их рисуем штриховкой.
  _isRemovableZone(z) {
    if (!this.mission || z.kind === 'algorithm') return false;
    const acts = this.mission.actions || [];
    for (const a of acts) {
      if (a.type !== 'remove_danger') continue;
      if (Math.hypot(z.x - (a.x || 0), z.y - (a.y || 0))
          <= Math.max(z.radius, 5)) return true;
    }
    return false;
  }

  _drawDangerZones() {
    const ctx = this.ctx;
    // Резервные счётчики по типам — только для legacy-зон без display_no.
    // Источник истины — серверный z.display_no (компактная нумерация 1..N
    // в пределах своего kind, см. _renumber_zones в session.py).
    let dangerNum = 0, algoNum = 0;
    // Если курсор в режиме установки зон — определим, над какой зоной
    // он сейчас находится (для подсветки кандидата на удаление).
    const cur = this.zoneMode ? this.zoneCursor : null;
    for (const z of this.dangerZones) {
      const c = this.worldToCanvas(z.x, z.y);
      const r = z.radius * this.scale;
      const isAlgo = z.kind === 'algorithm';
      const isRemovable = this._isRemovableZone(z);
      const fallback = isAlgo ? (++algoNum) : (++dangerNum);
      const num = (typeof z.display_no === 'number' && z.display_no > 0)
                  ? z.display_no : fallback;
      const isHovered = cur &&
        Math.hypot(cur.wx - z.x, cur.wy - z.y) <= z.radius;

      // Оба типа: тонкий пунктир, заливка 10% соответствующим цветом.
      // Под курсором в режиме зон — заливка ярче (подсказка «можно удалить»).
      const baseFill   = isAlgo ? 'rgba(255, 215, 0, 0.10)'
                                 : 'rgba(248,  81, 73, 0.10)';
      const hoverFill  = isAlgo ? 'rgba(255, 215, 0, 0.25)'
                                 : 'rgba(248,  81, 73, 0.30)';
      const fill   = isHovered ? hoverFill : baseFill;
      const stroke = isAlgo ? 'rgba(255, 215, 0, 0.95)'
                             : 'rgba(248,  81, 73, 0.95)';
      const labelColor = isAlgo ? '#ffd700' : '#f85149';

      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.fillStyle = fill;
      ctx.fill();

      ctx.strokeStyle = stroke;
      ctx.lineWidth   = isHovered ? 2.0 : 1.2;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);

      // Зона-задача «удалить» — диагональная штриховка: визуально
      // отличает её от нетронутой зоны-препятствия (наезд не штрафуется).
      if (isRemovable) {
        ctx.strokeStyle = stroke;
        this._hatchCircle(c.x, c.y, r);
      }

      // ── Нумерация зоны: крупная цифра в центре, белая обводка ─────
      // Размер от scale, но в разумных пределах, чтобы не перекрыть.
      const numFontPx = Math.max(11, Math.min(22, Math.round(r * 0.55)));
      ctx.font = `bold ${numFontPx}px sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(0, 0, 0, 0.7)';
      ctx.strokeText(String(num), c.x, c.y);
      ctx.fillStyle = labelColor;
      ctx.fillText(String(num), c.x, c.y);
      ctx.textBaseline = 'alphabetic';

      // Текстовая подпись под зоной (как раньше)
      ctx.fillStyle = labelColor;
      ctx.font = '11px sans-serif';
      ctx.fillText(z.label, c.x, c.y + r + 14);
      ctx.textAlign = 'left';
    }
  }

  // ── Превью-кружок будущей зоны под курсором + статус-плашка ─────────
  _drawZoneModeOverlay() {
    if (!this.zoneMode) return;
    const ctx = this.ctx;

    // 1) Превью под курсором (если курсор на холсте)
    if (this.zoneCursor) {
      const { wx, wy } = this.zoneCursor;
      const c = this.worldToCanvas(wx, wy);
      const r = this.zoneRadius * this.scale;
      const allowed = this.zoneInsideField;

      ctx.save();
      // Заливка
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.fillStyle = allowed ? 'rgba(248, 81, 73, 0.20)'
                              : 'rgba(140, 140, 140, 0.20)';
      ctx.fill();
      // Контур
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.strokeStyle = allowed ? 'rgba(248, 81, 73, 1.0)'
                                : 'rgba(140, 140, 140, 1.0)';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.stroke();
      ctx.setLineDash([]);
      // Перекрестье в центре
      ctx.beginPath();
      ctx.moveTo(c.x - 6, c.y); ctx.lineTo(c.x + 6, c.y);
      ctx.moveTo(c.x, c.y - 6); ctx.lineTo(c.x, c.y + 6);
      ctx.strokeStyle = allowed ? '#f85149' : '#888';
      ctx.lineWidth = 1;
      ctx.stroke();
      // Лейбл с координатами и радиусом
      const label = `x=${wx.toFixed(0)}, y=${wy.toFixed(0)}, r=${this.zoneRadius}`;
      ctx.font = '12px sans-serif';
      const labelPad = 4;
      const labelW = ctx.measureText(label).width + labelPad * 2;
      const labelH = 16;
      const lx = c.x + 12;
      const ly = c.y + 12;
      ctx.fillStyle = 'rgba(13, 17, 23, 0.85)';
      ctx.fillRect(lx, ly, labelW, labelH);
      ctx.strokeStyle = allowed ? '#f85149' : '#888';
      ctx.lineWidth = 1;
      ctx.strokeRect(lx, ly, labelW, labelH);
      ctx.fillStyle = '#e6edf3';
      ctx.fillText(label, lx + labelPad, ly + 12);

      if (!allowed) {
        ctx.fillStyle = '#f85149';
        ctx.font = 'bold 12px sans-serif';
        ctx.fillText('за пределами поля', lx, ly + labelH + 14);
      }
      ctx.restore();
    }
    // Статус-подсказка о режиме теперь не рисуется поверх холста —
    // её показывает подвал страницы (.appbar), см. control.js.
  }

  _drawPath() {
    const ctx  = this.ctx;
    const path = this.pathHistory;
    if (path.length < 2) return;

    ctx.beginPath();
    const p0 = this.worldToCanvas(path[0][0], path[0][1]);
    ctx.moveTo(p0.x, p0.y);
    for (let i = 1; i < path.length; i++) {
      const p = this.worldToCanvas(path[i][0], path[i][1]);
      ctx.lineTo(p.x, p.y);
    }
    // Зеленый, на 10% светлее var(--accent2) #3fb950 (rgb 63,185,80)
    // = смешан с белым 10%: rgb(82, 192, 97).
    ctx.strokeStyle = 'rgba(82, 192, 97, 0.55)';
    ctx.lineWidth   = 1.5;
    ctx.setLineDash([3, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  _drawAutoSegments() {
    // Сегменты, найденные планировщиком в режиме «осторожно».
    // Едва заметный фиолетовый пунктир — фоновая подсказка, не отвлекающая
    // от основной зеленой траектории движения.
    const ctx  = this.ctx;
    const segs = this.autoSegments || [];
    if (!segs.length) return;
    ctx.strokeStyle = 'rgba(180, 140, 255, 0.35)';   // прозрачный фиолет
    ctx.lineWidth   = 0.8;                            // очень тонкий
    ctx.setLineDash([4, 5]);
    for (const seg of segs) {
      if (!seg || seg.length < 2) continue;
      ctx.beginPath();
      const p0 = this.worldToCanvas(seg[0][0], seg[0][1]);
      ctx.moveTo(p0.x, p0.y);
      for (let i = 1; i < seg.length; i++) {
        const p = this.worldToCanvas(seg[i][0], seg[i][1]);
        ctx.lineTo(p.x, p.y);
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  _drawLaser() {
    if (this.sensorType === 'sonar') {
      this._drawSonar();
    } else {
      this._drawLaserBeam();
    }
  }

  _laserEndpoint(s, worldDist) {
    // Датчик смотрит в направлении движения: вперед или назад
    const headingDeg = s.speed < 0 ? (s.heading + 180) % 360 : s.heading;
    const rad = headingDeg * Math.PI / 180;
    const hw  = this.worldW / 2;
    const hh  = this.worldH / 2;
    const wt  = this.wallThickness || 0;
    const wx = Math.max(-(hw - wt), Math.min(hw - wt, s.x + Math.sin(rad) * worldDist));
    const wy = Math.max(-(hh - wt), Math.min(hh - wt, s.y + Math.cos(rad) * worldDist));
    return { wx, wy, rad };
  }

  _drawLaserBeam() {
    const s   = this.robotState;
    const ctx = this.ctx;
    const pos = this.worldToCanvas(s.x, s.y);
    const worldDist = (s.laser_dist > 0) ? s.laser_dist : 200;
    const { wx, wy } = this._laserEndpoint(s, worldDist);
    const ep = this.worldToCanvas(wx, wy);

    ctx.beginPath();
    ctx.moveTo(pos.x, pos.y);
    ctx.lineTo(ep.x, ep.y);
    ctx.strokeStyle = 'rgba(210,153,34,0.6)';
    ctx.lineWidth   = 1;
    ctx.setLineDash([2, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    if (s.laser_dist > 0) {
      ctx.beginPath();
      ctx.arc(ep.x, ep.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = '#d29922';
      ctx.fill();
    }
  }

  _drawSonar() {
    const s   = this.robotState;
    const ctx = this.ctx;
    const now = Date.now();

    // Детектируем новый импульс по значимой смене показания (>0.5 см)
    // Не сбрасываем таймер пока идет анимация — иначе мерцание при быстрых импульсах
    if (Math.abs(s.laser_dist - this._sonarLastDist) > 0.5) {
      this._sonarLastDist = s.laser_dist;
      if (!this._sonarAnimating) {
        this._sonarPulseTime = now;
        this._startSonarAnim();
      }
    }

    const pos       = this.worldToCanvas(s.x, s.y);
    const worldDist = (s.laser_dist > 0) ? s.laser_dist : 200;
    const { wx, wy, rad } = this._laserEndpoint(s, worldDist);
    const ep  = this.worldToCanvas(wx, wy);
    const dpx = Math.hypot(ep.x - pos.x, ep.y - pos.y);

    const canvasDir = rad - Math.PI / 2;
    const coneHalf  = (this.sonarConeDeg / 2) * Math.PI / 180;

    // Возраст последнего импульса (0 = только что, 1 = старый)
    const PULSE_MS = 350;
    const age = Math.min(1, (now - this._sonarPulseTime) / PULSE_MS);

    // Конус — яркий при новом импульсе, тускнеет к следующему
    const fillA   = 0.28 * (1 - age) + 0.05;
    const strokeA = 0.65 * (1 - age) + 0.18;

    ctx.beginPath();
    ctx.moveTo(pos.x, pos.y);
    ctx.arc(pos.x, pos.y, dpx, canvasDir - coneHalf, canvasDir + coneHalf);
    ctx.closePath();
    ctx.fillStyle = `rgba(210,153,34,${fillA.toFixed(2)})`;
    ctx.fill();

    ctx.strokeStyle = `rgba(210,153,34,${strokeA.toFixed(2)})`;
    ctx.lineWidth   = 1;
    ctx.setLineDash([3, 4]);
    ctx.stroke();
    ctx.setLineDash([]);

    // Центральный луч
    ctx.beginPath();
    ctx.moveTo(pos.x, pos.y);
    ctx.lineTo(ep.x, ep.y);
    ctx.strokeStyle = `rgba(210,153,34,${(strokeA * 0.85).toFixed(2)})`;
    ctx.lineWidth   = 1;
    ctx.setLineDash([2, 4]);
    ctx.stroke();
    ctx.setLineDash([]);

    // Фронт волны — расширяющаяся дуга от робота до стены (первые 60% времени)
    if (age < 0.6) {
      const waveAge  = age / 0.6;
      const waveR    = waveAge * dpx;
      const waveA    = (1 - waveAge) * 0.85;
      ctx.beginPath();
      ctx.arc(pos.x, pos.y, waveR, canvasDir - coneHalf, canvasDir + coneHalf);
      ctx.strokeStyle = `rgba(210,153,34,${waveA.toFixed(2)})`;
      ctx.lineWidth   = 1.5;
      ctx.setLineDash([]);
      ctx.stroke();
    }

    // Вспышка точки отражения при прилете эха (60–100% времени)
    if (s.laser_dist > 0) {
      const echoAge   = age < 0.6 ? 0 : (age - 0.6) / 0.4;
      const dotR      = 3 + (1 - echoAge) * 3;     // 6px → 3px
      const dotA      = 0.4 + (1 - echoAge) * 0.6; // 1.0 → 0.4
      ctx.beginPath();
      ctx.arc(ep.x, ep.y, dotR, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(210,153,34,${dotA.toFixed(2)})`;
      ctx.fill();
    }
  }

  _startSonarAnim() {
    if (this._sonarAnimating) return;
    this._sonarAnimating = true;
    const PULSE_MS = 350;
    const tick = () => {
      if (Date.now() - this._sonarPulseTime < PULSE_MS) {
        this.draw();
        requestAnimationFrame(tick);
      } else {
        this._sonarAnimating = false;
        this.draw(); // финальный кадр с потухшим конусом
      }
    };
    requestAnimationFrame(tick);
  }

  _drawRobot() {
    const s      = this.robotState;
    const ctx    = this.ctx;
    const pos    = this.worldToCanvas(s.x, s.y);
    const moving   = Math.abs(s.speed) > 0;
    const backward = s.speed < 0;

    const color = moving         ? '#8957e5'
                : s.cautious     ? '#ff7b72'
                : s.mode === 'marker' ? '#d29922'
                : '#58a6ff';

    // Реальные размеры робота в пикселях (с минимумом для видимости при малом zoom)
    const Lcm = this.robotLengthCm || 20;
    const Wcm = this.robotWidthCm  || 12;
    const bh  = Math.max(10, Lcm * this.scale);          // длина в пикселях
    const bw  = Math.max(5,  (Wcm / 2) * this.scale);    // полуширина в пикселях

    // pos = canvas-позиция НОСА (мировые координаты робота = кончик носа)
    const rad = s.heading * Math.PI / 180;

    // Центр корпуса смещен назад от носа на bh/2
    const cx = pos.x - (bh / 2) * Math.sin(rad);
    const cy = pos.y + (bh / 2) * Math.cos(rad);

    // Задняя точка корпуса (для стрелки назад)
    const rearX = pos.x - bh * Math.sin(rad);
    const rearY = pos.y + bh * Math.cos(rad);

    // ── Стрелка направления движения ──────────────────────────────────────────
    if (moving) {
      const sx = backward ? rearX : pos.x;
      const sy = backward ? rearY : pos.y;
      const len = Math.max(20, bh * 1.2);
      const sgn = backward ? -1 : 1;
      const ex = sx + Math.sin(rad) * len * sgn;
      const ey = sy - Math.cos(rad) * len * sgn;

      ctx.save();
      ctx.strokeStyle = color + '66';
      ctx.lineWidth   = 1.5;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(sx, sy);
      ctx.lineTo(ex, ey);
      ctx.stroke();
      ctx.setLineDash([]);

      const ang = Math.atan2(ey - sy, ex - sx);
      ctx.fillStyle = color + '66';
      ctx.beginPath();
      ctx.moveTo(ex, ey);
      ctx.lineTo(ex - 8 * Math.cos(ang - 0.4), ey - 8 * Math.sin(ang - 0.4));
      ctx.lineTo(ex - 8 * Math.cos(ang + 0.4), ey - 8 * Math.sin(ang + 0.4));
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }

    // Цвет корпуса зависит от светового индикатора (только если задан явно через NLU)
    const lightColor = Array.isArray(s.light_color) ? s.light_color : null;
    const hasLight = lightColor && lightColor.length === 3 && lightColor.some(v => v > 0);
    const bodyStroke = hasLight ? `rgb(${lightColor[0]}, ${lightColor[1]}, ${lightColor[2]})` : color;
    const bodyFill   = hasLight ? `rgba(${lightColor[0]}, ${lightColor[1]}, ${lightColor[2]}, 0.25)` : color + '33';

    // ── Корпус — центрирован на (0,0) в локальной системе ────────────────────
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(rad);

    ctx.fillStyle   = bodyFill;
    ctx.strokeStyle = bodyStroke;
    ctx.lineWidth   = 1.5;
    ctx.beginPath();
    ctx.rect(-bw, -bh / 2, bw * 2, bh);
    ctx.fill();
    ctx.stroke();

    // Нос — треугольник на переднем краю корпуса
    const noseSize = Math.min(bw * 0.7, 6);
    ctx.beginPath();
    ctx.moveTo(0, -bh / 2 - noseSize);
    ctx.lineTo(-noseSize, -bh / 2 + 1);
    ctx.lineTo(noseSize,  -bh / 2 + 1);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();

    // ── Колеса ─────────────────────────────────────────────────────────────────
    const wheelW = Math.max(2.5, bw * 0.35);
    const wheelH = Math.max(4,   bh * 0.18);
    const wyRear  = bh * 0.30;
    const wyFront = -bh * 0.30;
    const wx = [-bw - wheelW * 0.5, bw + wheelW * 0.5];

    ctx.fillStyle = moving ? color + '55' : '#8b949e';
    for (const wyi of [wyRear, wyFront]) {
      for (const wxi of wx) {
        ctx.fillRect(wxi - wheelW / 2, wyi - wheelH / 2, wheelW, wheelH);
      }
    }

    // Линии направления колес
    ctx.lineWidth = 1.5;
    const halfH = wheelH / 2;
    if (moving) {
      ctx.strokeStyle = color + 'cc';
      // Задние — прямо
      for (const wxi of wx) {
        ctx.beginPath();
        ctx.moveTo(wxi, wyRear - halfH); ctx.lineTo(wxi, wyRear + halfH);
        ctx.stroke();
      }
      // Передние — по углу руля
      const steerRad = s.steer * Math.PI / 180;
      for (const wxi of wx) {
        ctx.save();
        ctx.translate(wxi, wyFront);
        ctx.rotate(steerRad);
        ctx.beginPath();
        ctx.moveTo(0, -halfH); ctx.lineTo(0, halfH);
        ctx.stroke();
        ctx.restore();
      }
    } else if (s.steer !== 0) {
      // Руль повернут, машина стоит
      ctx.strokeStyle = '#d29922';
      ctx.lineWidth   = 2;
      const steerRad  = s.steer * Math.PI / 180;
      for (const wxi of wx) {
        ctx.save();
        ctx.translate(wxi, wyFront);
        ctx.rotate(steerRad);
        ctx.beginPath();
        ctx.moveTo(0, -halfH); ctx.lineTo(0, halfH);
        ctx.stroke();
        ctx.restore();
      }
    }

    ctx.restore();

    // ── Подпись (рядом с носом) ────────────────────────────────────────────────
    ctx.fillStyle = 'rgba(201,209,217,0.85)';
    ctx.font = '10px monospace';
    const lx = pos.x + bw + 6;
    ctx.fillText(`(${s.x.toFixed(0)}, ${s.y.toFixed(0)})`, lx, pos.y - 10);
    ctx.fillText(`курс ${s.heading.toFixed(0)}°`, lx, pos.y + 2);
    if (s.steer !== 0) {
      ctx.fillStyle = '#d29922';
      ctx.fillText(`руль ${s.steer > 0 ? '+' : ''}${s.steer.toFixed(0)}°`, lx, pos.y + 14);
    }
    if (s.dist_left > 0) {
      ctx.fillStyle = '#3fb950';
      ctx.fillText(`${s.dist_left.toFixed(0)} см`, lx, pos.y + 26);
    }
  }
}

// Экспорт глобального экземпляра
let robotCanvas = null;

function initCanvas() {
  const el = document.getElementById('robot-canvas');
  if (!el) return;
  robotCanvas = new RobotCanvas(el);
  return robotCanvas;
}
