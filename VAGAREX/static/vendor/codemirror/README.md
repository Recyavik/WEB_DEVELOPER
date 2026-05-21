# CodeMirror 6 vendored bundle

Бандлы CodeMirror 6 (с self-contained зависимостями), скачаны с esm.sh
с параметром `?bundle`. Используются модулем `/static/cm-editor.js`
для расширенного редактора кода во full-screen модалке.

## Версии (на момент v3.7.6)

| Файл | Пакет npm | Версия |
|---|---|---|
| codemirror.js     | codemirror               | 6.65.7 |
| lang-python.js    | @codemirror/lang-python  | 6.2.1  |
| autocomplete.js   | @codemirror/autocomplete | 6.20.2 |
| theme-one-dark.js | @codemirror/theme-one-dark | 6.1.3 |
| commands.js       | @codemirror/commands     | 6.10.3 |
| state.js          | @codemirror/state        | 6.6.0  |

## Зачем локально (а не esm.sh)

`https://esm.sh/codemirror@6?bundle` периодически недоступен (региональные
блокировки, сетевые проблемы, изменения формата экспортов). Когда импорт
падает — пользователь видит красную ошибку в Console и fallback на
обычный textarea. Локальные бандлы убирают эту неопределённость.

## Как обновить

Запустить из корня `VAGAREX/`:

```bash
python -X utf8 -c "
import urllib.request, re
from pathlib import Path
V = Path('static/vendor/codemirror')
PACKAGES = {
    'codemirror.js':     'https://esm.sh/codemirror@6?bundle',
    'lang-python.js':    'https://esm.sh/@codemirror/lang-python@6?bundle',
    'autocomplete.js':   'https://esm.sh/@codemirror/autocomplete@6?bundle',
    'theme-one-dark.js': 'https://esm.sh/@codemirror/theme-one-dark@6?bundle',
    'commands.js':       'https://esm.sh/@codemirror/commands@6?bundle',
    'state.js':          'https://esm.sh/@codemirror/state@6?bundle',
}
def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8')
for fname, entry in PACKAGES.items():
    text = fetch(entry)
    m = re.search(r'from\\s+\"(/[^\"]+\\.mjs)\"', text)
    if not m: continue
    body = fetch('https://esm.sh' + m.group(1))
    # Заглушка для /node/process.mjs (только в lang-python.js)
    body = re.sub(r'import __Process\\\$ from \"/node/process\\.mjs\";',
                  'var __Process\$={env:{}};', body, count=1)
    (V/fname).write_text(body, encoding='utf-8')
    print(fname, len(body)//1024, 'KB')
"
```

## Патчи

В `lang-python.js` заменён внешний import:

```js
// Было (требует /node/process.mjs с esm.sh):
import __Process$ from "/node/process.mjs";

// Стало (no-op заглушка, используется только для __Process$.env.LOG):
var __Process$={env:{}};
```

## Размер

~1.3 МБ суммарно. Каждый бандл — self-contained (свои копии общих
зависимостей типа `@codemirror/view`). Это решает проблемы дедупа
EditorView между пакетами, но увеличивает суммарный объём.
