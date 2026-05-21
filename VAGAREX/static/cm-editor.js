/**
 * cm-editor.js — CodeMirror 6 редактор для развёрнутого окна кода.
 *
 * Грузим CodeMirror из локальной папки /static/vendor/codemirror/ —
 * бандлы скачаны с esm.sh с `?bundle` (все зависимости в одном файле,
 * без проблем с дедупом instance-ов EditorView между пакетами).
 * Версии зафиксированы: codemirror@6.65.7, lang-python@6.2.1,
 * autocomplete@6.20.2, theme-one-dark@6.1.3, commands@6.10.3,
 * state@6.6.0. Чтобы обновить — перекачать тем же скриптом
 * (см. историю v3.7.6).
 *
 * Если по какой-то причине бандлы не загрузились — control.js это
 * увидит (window.cmEditor.isReady() = false) и оставит fallback-textarea.
 */
console.log('[cm-editor] загружаю CodeMirror 6 из /static/vendor/codemirror…');
import {EditorView, basicSetup, keymap, lineNumbers}
                                   from '/static/vendor/codemirror/codemirror.js';
import {python}                    from '/static/vendor/codemirror/lang-python.js';
import {autocompletion}            from '/static/vendor/codemirror/autocomplete.js';
import {oneDark}                   from '/static/vendor/codemirror/theme-one-dark.js';
import {indentWithTab}             from '/static/vendor/codemirror/commands.js';
import {Compartment}               from '/static/vendor/codemirror/state.js';
console.log('[cm-editor] импорты OK, EditorView:', typeof EditorView);

// ── Каталог robot.X ────────────────────────────────────────────────────
// Один источник истины — `/static/robot-catalog.js` (обычный <script>,
// грузится даже если esm.sh отвалился). Этот модуль и control.js берут
// каталог отсюда, не дублируя список.
const _CATALOG = (window.ROBOT_API_CATALOG || {methods: [], properties: []});
const ROBOT_METHODS    = _CATALOG.methods;
const ROBOT_PROPERTIES = _CATALOG.properties;

// ── Completion provider: после `robot.` показываем каталог ─────────────
function robotCompletion(context) {
  // Ловим выражение вида `robot.` или `robot.foo` перед курсором.
  const word = context.matchBefore(/robot\.\w*/);
  if (!word) return null;
  // Не показываем меню, если пользователь не печатает (только Ctrl+Space).
  if (word.from === word.to && !context.explicit) return null;
  const afterDot = word.from + 6;  // длина 'robot.'
  const options = [
    ...ROBOT_METHODS.map(([name, sig, info]) => ({
      label:  name,
      type:   'method',
      detail: sig,
      info:   info,
      // Вставляем имя + скобки; курсор в конец чтобы пользователь продолжил.
      apply:  (view, completion, from, to) => {
        view.dispatch({
          changes: {from, to, insert: name + '('},
          selection: {anchor: from + name.length + 1},
        });
      },
    })),
    ...ROBOT_PROPERTIES.map(([name, info]) => ({
      label:  name,
      type:   'property',
      info:   info,
    })),
  ];
  return {from: afterDot, options, validFor: /^\w*$/};
}

// ── Темa и font-size в compartment'е чтобы можно было менять на лету ──
const fontSizeCompartment = new Compartment();

function makeFontSizeTheme(px) {
  return EditorView.theme({
    '&': {fontSize: `${px}px`, height: '100%'},
    '.cm-content, .cm-gutters': {fontSize: `${px}px`},
    '.cm-scroller': {fontFamily: '"Consolas", "Menlo", monospace'},
  });
}

// ── Один общий инстанс редактора (модалка одна) ───────────────────────
let view = null;
let currentFontSize = 15;
let onChangeCb = null;

function mount(container, initialText, options = {}) {
  try {
    if (view) view.destroy();
    if (options.onChange) onChangeCb = options.onChange;
    currentFontSize = options.fontSize || 15;
    view = new EditorView({
      doc: initialText || '',
      extensions: [
        basicSetup,                      // line numbers, history, brackets, и пр.
        python(),                        // подсветка + умный отступ Python
        autocompletion({override: [robotCompletion]}),
        keymap.of([indentWithTab]),      // Tab = 4 пробела / умный отступ
        oneDark,                         // тёмная тема как у нас
        fontSizeCompartment.of(makeFontSizeTheme(currentFontSize)),
        EditorView.updateListener.of(u => {
          if (u.docChanged && onChangeCb) onChangeCb(view.state.doc.toString());
        }),
      ],
      parent: container,
    });
    console.log('[cm-editor] mount OK, EditorView создан');
  } catch (e) {
    console.error('[cm-editor] FAILED mount:', e);
    view = null;
    throw e;
  }
}

function getValue() {
  return view ? view.state.doc.toString() : '';
}

function setValue(text) {
  if (!view) return;
  view.dispatch({
    changes: {from: 0, to: view.state.doc.length, insert: text || ''},
  });
}

function appendLine(text) {
  if (!view) return;
  const end = view.state.doc.length;
  // Если документ не кончается переносом — добавим его.
  const needsNL = view.state.doc.length > 0
                  && view.state.doc.sliceString(end - 1) !== '\n';
  view.dispatch({
    changes: {from: end, insert: (needsNL ? '\n' : '') + text + '\n'},
    selection: {anchor: end + text.length + (needsNL ? 1 : 0) + 1},
    scrollIntoView: true,
  });
}

function focus() {
  if (view) view.focus();
}

function setFontSize(px) {
  if (!view) return;
  const clamped = Math.max(9, Math.min(32, Math.round(px)));
  currentFontSize = clamped;
  view.dispatch({
    effects: fontSizeCompartment.reconfigure(makeFontSizeTheme(clamped)),
  });
}

function getFontSize() { return currentFontSize; }

function destroy() {
  if (view) { view.destroy(); view = null; }
}

function isReady() { return view !== null; }

// Экспорт в глобал — control.js обычный (не-module) скрипт.
window.cmEditor = {
  mount, getValue, setValue, appendLine, focus,
  setFontSize, getFontSize, destroy, isReady,
};

// Сигнализируем control.js что модуль готов (на случай если он ждёт).
window.dispatchEvent(new Event('cm-editor-ready'));
