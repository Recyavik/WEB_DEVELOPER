from __future__ import annotations

import datetime as dt
import os
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "Занятие_01_Конспект.docx"
OUTPUT = ROOT / "Урок_01_HTML_База_структуры_страницы.docx"
ASSETS_DIR = ROOT / "_generated_html_doc_assets"


EMU_PER_PX = 9525


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if bold:
        candidates.extend(
            [
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/calibrib.ttf",
                "C:/Windows/Fonts/segoeuib.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/calibri.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
            ]
        )
    for candidate in candidates:
        if os.path.exists(candidate):
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def draw_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: str, outline: str, radius: int = 24) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=3)


def centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str = "#102030",
) -> None:
    left, top, right, bottom = box
    lines = wrap_text(draw, text, font, right - left - 30)
    line_height = font.size + 8 if hasattr(font, "size") else 26
    block_h = len(lines) * line_height
    y = top + (bottom - top - block_h) // 2
    for line in lines:
        width = draw.textlength(line, font=font)
        x = left + (right - left - width) / 2
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height


def save_image(name: str, size: tuple[int, int], painter) -> Path:
    ensure_dir(ASSETS_DIR)
    path = ASSETS_DIR / name
    image = Image.new("RGB", size, "#F7FAFC")
    draw = ImageDraw.Draw(image)
    painter(image, draw)
    image.save(path)
    return path


def build_structure_image() -> Path:
    def painter(image: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        title_font = load_font(34, bold=True)
        label_font = load_font(24, bold=True)
        small_font = load_font(20)

        draw.rectangle((0, 0, image.width, image.height), fill="#EFF6FF")
        draw.text((40, 28), "Минимальный каркас HTML-документа", font=title_font, fill="#0F3D66")

        outer = (70, 90, 1130, 620)
        head = (130, 160, 1070, 290)
        body = (130, 330, 1070, 560)
        draw_card(draw, outer, "#FFFFFF", "#1D4ED8", 30)
        draw_card(draw, head, "#DBEAFE", "#3B82F6")
        draw_card(draw, body, "#DCFCE7", "#22C55E")
        centered_text(draw, outer, "<html> ... </html>", label_font, "#1E3A8A")
        centered_text(draw, head, "<head> meta, title, link, script </head>", label_font, "#1E40AF")
        centered_text(draw, body, "<body> весь видимый интерфейс страницы </body>", label_font, "#166534")

        notes = [
            ("HEAD", "Служебная часть: кодировка, заголовок вкладки, подключение CSS и JS"),
            ("BODY", "Всё, что пользователь видит: текст, меню, кнопки, формы, карточки и секции"),
        ]
        y = 650
        for title, desc in notes:
            draw.rounded_rectangle((70, y, 1130, y + 72), radius=20, fill="#FFFFFF", outline="#CBD5E1", width=2)
            draw.text((100, y + 18), title, font=label_font, fill="#0F172A")
            draw.text((250, y + 22), desc, font=small_font, fill="#334155")
            y += 92

    return save_image("html_structure.png", (1200, 860), painter)


def build_layout_image() -> Path:
    def painter(image: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        title_font = load_font(34, bold=True)
        label_font = load_font(24, bold=True)
        small_font = load_font(18)

        draw.rectangle((0, 0, image.width, image.height), fill="#FFF7ED")
        draw.text((40, 24), "Семантическая раскладка страницы", font=title_font, fill="#9A3412")

        blocks = [
            ((70, 90, 1130, 170), "#FED7AA", "#F97316", "header"),
            ((70, 190, 1130, 250), "#FFEDD5", "#FB923C", "nav"),
            ((70, 280, 760, 610), "#DCFCE7", "#22C55E", "main"),
            ((790, 280, 1130, 610), "#E0F2FE", "#0EA5E9", "aside"),
            ((110, 340, 720, 410), "#FFFFFF", "#4ADE80", "section"),
            ((110, 440, 720, 520), "#F0FDF4", "#16A34A", "article"),
            ((70, 640, 1130, 720), "#E2E8F0", "#64748B", "footer"),
        ]
        for box, fill, outline, label in blocks:
            draw_card(draw, box, fill, outline, 24)
            centered_text(draw, box, f"<{label}>", label_font)

        draw.text(
            (70, 760),
            "Идея: header/nav/main/aside/footer описывают смысл блока, а не только его размер или цвет.",
            font=small_font,
            fill="#7C2D12",
        )

    return save_image("page_layout.png", (1200, 860), painter)


def build_tag_image() -> Path:
    def painter(image: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        title_font = load_font(34, bold=True)
        code_font = load_font(34, bold=True)
        label_font = load_font(22, bold=True)
        small_font = load_font(18)

        draw.rectangle((0, 0, image.width, image.height), fill="#F5F3FF")
        draw.text((40, 30), "Анатомия HTML-тега", font=title_font, fill="#5B21B6")

        draw.rounded_rectangle((80, 120, 1120, 260), radius=24, fill="#FFFFFF", outline="#8B5CF6", width=3)
        code = '<a href="catalog.html" class="menu-link">Каталог</a>'
        draw.text((110, 170), code, font=code_font, fill="#1F2937")

        items = [
            ((90, 340, 340, 460), "#EDE9FE", "#7C3AED", "Открывающий тег", "Имя элемента и атрибуты"),
            ((370, 340, 830, 460), "#DDD6FE", "#6D28D9", "Содержимое", "Текст, который увидит пользователь"),
            ((860, 340, 1110, 460), "#EDE9FE", "#7C3AED", "Закрывающий тег", "Показывает конец элемента"),
        ]
        for box, fill, outline, title, desc in items:
            draw_card(draw, box, fill, outline, 22)
            centered_text(draw, box, title, label_font, "#4C1D95")
            draw.text((box[0] + 16, box[1] + 78), desc, font=small_font, fill="#312E81")

        draw.text(
            (80, 540),
            "Часть тегов парные: <p>...</p>, <div>...</div>. Некоторые одиночные: <img>, <br>, <meta>.",
            font=small_font,
            fill="#4C1D95",
        )

    return save_image("tag_anatomy.png", (1200, 700), painter)


def build_form_image() -> Path:
    def painter(image: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        title_font = load_font(34, bold=True)
        label_font = load_font(24, bold=True)
        text_font = load_font(20)

        draw.rectangle((0, 0, image.width, image.height), fill="#F0FDF4")
        draw.text((40, 24), "Как форма превращается в GET-запрос", font=title_font, fill="#166534")

        left = (60, 110, 470, 560)
        center = (520, 215, 680, 315)
        right = (730, 110, 1140, 560)
        draw_card(draw, left, "#FFFFFF", "#22C55E", 26)
        draw_card(draw, center, "#DCFCE7", "#16A34A", 26)
        draw_card(draw, right, "#FFFFFF", "#22C55E", 26)

        draw.text((100, 150), "<form method=\"get\">", font=label_font, fill="#14532D")
        draw.text((100, 220), "name=\"search\"", font=text_font, fill="#166534")
        draw.rounded_rectangle((100, 255, 420, 320), radius=14, fill="#F8FAFC", outline="#94A3B8", width=2)
        draw.text((120, 275), "html basics", font=text_font, fill="#334155")
        draw.rounded_rectangle((100, 360, 250, 420), radius=14, fill="#16A34A", outline="#15803D", width=2)
        draw.text((135, 380), "Отправить", font=text_font, fill="#FFFFFF")

        centered_text(draw, center, "браузер\nсобирает\nпараметры", label_font, "#166534")

        draw.text((770, 170), "URL после отправки:", font=label_font, fill="#14532D")
        draw.rounded_rectangle((770, 230, 1100, 350), radius=18, fill="#ECFDF5", outline="#4ADE80", width=2)
        draw.text((790, 275), "/search?search=html+basics", font=text_font, fill="#065F46")
        draw.text((770, 390), "Так появляются query-параметры.", font=text_font, fill="#166534")
        draw.text((770, 425), "Это запрос к серверу, а не цикл или условие.", font=text_font, fill="#166534")

    return save_image("form_query.png", (1200, 660), painter)


def xml_text(text: str) -> str:
    if text.startswith(" ") or text.endswith(" ") or "  " in text:
        return f'<w:t xml:space="preserve">{escape(text)}</w:t>'
    return f"<w:t>{escape(text)}</w:t>"


def make_run(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
    size: int = 24,
    font: str | None = None,
    highlight: str | None = None,
) -> str:
    props: list[str] = []
    if bold:
        props.append("<w:b/><w:bCs/>")
    if italic:
        props.append("<w:i/><w:iCs/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    if font:
        props.append(
            f'<w:rFonts w:ascii="{escape(font)}" w:eastAsia="{escape(font)}" '
            f'w:hAnsi="{escape(font)}" w:cs="{escape(font)}"/>'
        )
    if highlight:
        props.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{highlight}"/>')
    return f"<w:r><w:rPr>{''.join(props)}</w:rPr>{xml_text(text)}</w:r>"


def make_paragraph(
    runs: list[str],
    *,
    align: str | None = None,
    before: int = 40,
    after: int = 100,
    left_indent: int | None = None,
    right_indent: int | None = None,
    border_left: str | None = None,
    border_bottom: str | None = None,
) -> str:
    props: list[str] = [f'<w:spacing w:before="{before}" w:after="{after}"/>']
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    if left_indent is not None or right_indent is not None:
        left = left_indent or 0
        right = right_indent or 0
        props.append(f'<w:ind w:left="{left}" w:right="{right}"/>')
    borders: list[str] = []
    if border_left:
        borders.append(f'<w:left w:val="single" w:sz="12" w:space="1" w:color="{border_left}"/>')
    if border_bottom:
        borders.append(f'<w:bottom w:val="single" w:sz="8" w:space="3" w:color="{border_bottom}"/>')
    if borders:
        props.append(f"<w:pBdr>{''.join(borders)}</w:pBdr>")
    return f"<w:p><w:pPr>{''.join(props)}</w:pPr>{''.join(runs)}</w:p>"


def paragraph_text(text: str, **kwargs) -> str:
    return make_paragraph([make_run(text)], **kwargs)


def heading(text: str, level: int = 1) -> str:
    if level == 1:
        return make_paragraph(
            [make_run(text, bold=True, color="1A4F72", size=30)],
            before=260,
            after=120,
            border_bottom="1A4F72",
        )
    if level == 2:
        return make_paragraph(
            [make_run(text, bold=True, color="2563EB", size=26)],
            before=180,
            after=80,
        )
    return make_paragraph([make_run(text, bold=True, color="0F172A", size=24)], before=120, after=60)


def bullet(text: str) -> str:
    return make_paragraph(
        [make_run("• ", bold=True, color="1D4ED8"), make_run(text)],
        before=20,
        after=40,
        left_indent=360,
        right_indent=120,
    )


def code_line(text: str) -> str:
    return make_paragraph(
        [make_run(text, font="Courier New", color="1A1A2E", size=20, highlight="F3F4F6")],
        before=10,
        after=10,
        left_indent=720,
        right_indent=360,
        border_left="2E86C1",
    )


def note(text: str) -> str:
    return make_paragraph(
        [make_run(text, italic=True, color="5D4037", size=21)],
        before=60,
        after=60,
        left_indent=400,
        border_left="F59E0B",
    )


def image_paragraph(rid: str, name: str, width_px: int, height_px: int, doc_pr_id: int) -> str:
    cx = width_px * EMU_PER_PX
    cy = height_px * EMU_PER_PX
    drawing = f"""
<w:r>
  <w:drawing>
    <wp:inline distT="0" distB="0" distL="0" distR="0">
      <wp:extent cx="{cx}" cy="{cy}"/>
      <wp:docPr id="{doc_pr_id}" name="{escape(name)}"/>
      <wp:cNvGraphicFramePr>
        <a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/>
      </wp:cNvGraphicFramePr>
      <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
        <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
          <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:nvPicPr>
              <pic:cNvPr id="{doc_pr_id}" name="{escape(name)}"/>
              <pic:cNvPicPr/>
            </pic:nvPicPr>
            <pic:blipFill>
              <a:blip r:embed="{rid}"/>
              <a:stretch><a:fillRect/></a:stretch>
            </pic:blipFill>
            <pic:spPr>
              <a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
              <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
            </pic:spPr>
          </pic:pic>
        </a:graphicData>
      </a:graphic>
    </wp:inline>
  </w:drawing>
</w:r>""".strip()
    return make_paragraph([drawing], align="center", before=80, after=80)


def build_document_xml(image_specs: list[tuple[str, Path]]) -> str:
    parts: list[str] = []

    parts.append(make_paragraph([make_run("Конспект лекции", bold=True, color="1A4F72", size=44)], align="center", before=200, after=80))
    parts.append(make_paragraph([make_run("Урок 1 — HTML: база структуры и размещения элементов", color="2E86C1", size=30)], align="center", before=20, after=60))
    parts.append(make_paragraph([make_run("Фокус урока: как собирается любая страница и где на ней живут основные виджеты", italic=True, color="555555", size=20)], align="center", before=20, after=120))

    parts.append(heading("1. Что такое HTML и зачем он нужен"))
    parts.append(paragraph_text("HTML — это язык разметки, который описывает структуру веб-страницы. Он не отвечает за красивое оформление и не выполняет бизнес-логику: его задача — сообщить браузеру, где заголовок, где меню, где основной контент, где форма, где кнопка, где картинка и в каком порядке всё расположено."))
    parts.append(paragraph_text("Если представить сайт как дом, то HTML — это план комнат и стен. CSS отвечает за цвет, отступы и внешний вид, а JavaScript добавляет поведение: клики, проверки, динамическую подгрузку и интерактивность."))
    parts.append(bullet("Без HTML не существует ни одной страницы: браузеру просто не из чего строить DOM-дерево."))
    parts.append(bullet("Любой интерфейс начинается с каркаса страницы, а уже потом оформляется и оживляется."))
    parts.append(note("Важно: HTML определяет смысл и структуру элементов. Даже если блок пока выглядит просто, лучше сразу размечать его правильно."))

    parts.append(heading("2. Минимальный каркас любого HTML-документа"))
    parts.append(paragraph_text("У любой нормальной страницы есть минимальный обязательный скелет: декларация типа документа, корневой тег html, служебная секция head и видимая секция body."))
    parts.append(image_paragraph("rId12", "Структура HTML", 1200, 860, 1201))
    parts.append(code_line("<!DOCTYPE html>"))
    parts.append(code_line('<html lang="ru">'))
    parts.append(code_line("  <head>"))
    parts.append(code_line('    <meta charset="UTF-8">'))
    parts.append(code_line('    <meta name="viewport" content="width=device-width, initial-scale=1.0">'))
    parts.append(code_line("    <title>Моя страница</title>"))
    parts.append(code_line("  </head>"))
    parts.append(code_line("  <body>"))
    parts.append(code_line("    <!-- здесь размещаются все видимые элементы -->"))
    parts.append(code_line("  </body>"))
    parts.append(code_line("</html>"))
    parts.append(bullet("`<!DOCTYPE html>` сообщает браузеру, что документ нужно обрабатывать как современный HTML5."))
    parts.append(bullet("`<head>` содержит метаданные: кодировку, заголовок вкладки, SEO-описания, подключения стилей и скриптов."))
    parts.append(bullet("`<body>` содержит всё, что попадает в окно браузера: тексты, секции, изображения, формы, карточки, кнопки и таблицы."))

    parts.append(heading("3. Как размещать основные блоки страницы"))
    parts.append(paragraph_text("Чтобы элементы на странице не лежали хаотично, сначала выделяют крупные области интерфейса. Это основа размещения всех будущих виджетов: шапка, навигация, основная зона, боковая колонка и подвал."))
    parts.append(image_paragraph("rId13", "Семантическая раскладка", 1200, 860, 1202))
    parts.append(code_line("<body>"))
    parts.append(code_line("  <header>Логотип, название, контакты</header>"))
    parts.append(code_line("  <nav>Главное меню сайта</nav>"))
    parts.append(code_line("  <main>"))
    parts.append(code_line("    <section>Основной смысловой блок</section>"))
    parts.append(code_line("    <article>Отдельная статья или карточка</article>"))
    parts.append(code_line("  </main>"))
    parts.append(code_line("  <aside>Дополнительная информация</aside>"))
    parts.append(code_line("  <footer>Копирайт, ссылки, контакты</footer>"))
    parts.append(code_line("</body>"))
    parts.append(bullet("`header` — верхняя часть страницы или отдельного блока."))
    parts.append(bullet("`nav` — зона навигации: меню, ссылки по разделам, хлебные крошки."))
    parts.append(bullet("`main` — главная смысловая часть страницы; на странице должен быть один основной `main`."))
    parts.append(bullet("`section` — крупный тематический раздел."))
    parts.append(bullet("`article` — самостоятельный материал: новость, пост, карточка товара."))
    parts.append(bullet("`aside` — дополнительный контент: фильтры, подсказки, баннеры, блоки справа."))
    parts.append(bullet("`footer` — нижняя часть страницы или секции."))
    parts.append(note("Частая ошибка новичка: размещать всё подряд только в `div`. `div` полезен, но сначала стоит подумать о смысле блока и использовать семантические теги там, где они подходят."))

    parts.append(heading("4. Самые важные HTML-элементы, без которых не обходится страница"))
    parts.append(heading("4.1. Контейнеры и структура", level=2))
    parts.append(bullet("`div` — универсальный контейнер без собственного смысла. Часто используется для сеток, колонок и группировки."))
    parts.append(bullet("`span` — строчный контейнер для небольшой части текста или иконки внутри строки."))
    parts.append(heading("4.2. Текстовые элементы", level=2))
    parts.append(bullet("`h1`–`h6` — заголовки. Обычно на странице один главный `h1`."))
    parts.append(bullet("`p` — абзац текста."))
    parts.append(bullet("`strong` и `em` — смысловое выделение текста."))
    parts.append(bullet("`br` — перенос строки, когда он действительно нужен внутри текста."))
    parts.append(heading("4.3. Ссылки и навигация", level=2))
    parts.append(bullet("`a` — ссылка на другую страницу, раздел сайта, файл или внешний ресурс."))
    parts.append(bullet("Меню почти всегда строится на ссылках внутри `nav`."))
    parts.append(heading("4.4. Медиа", level=2))
    parts.append(bullet("`img` — изображение. У него обязательно должен быть `alt`, чтобы описать картинку."))
    parts.append(bullet("`figure` и `figcaption` — изображение с подписью, если подпись важна по смыслу."))
    parts.append(heading("4.5. Списки", level=2))
    parts.append(bullet("`ul` + `li` — маркированный список."))
    parts.append(bullet("`ol` + `li` — нумерованный список."))
    parts.append(bullet("Списки используются не только для текста, но и для меню, шагов, преимуществ, фильтров."))
    parts.append(heading("4.6. Формы и виджеты ввода", level=2))
    parts.append(bullet("`form` — контейнер формы."))
    parts.append(bullet("`label` — подпись к полю ввода; улучшает доступность и удобство."))
    parts.append(bullet("`input`, `textarea`, `select`, `option`, `button` — базовые виджеты для общения пользователя с сайтом."))
    parts.append(heading("4.7. Таблицы", level=2))
    parts.append(bullet("`table`, `tr`, `th`, `td` нужны, когда данные действительно табличные: расписание, прайс, отчёт, статистика."))

    parts.append(heading("5. Анатомия тега и вложенность элементов"))
    parts.append(paragraph_text("HTML состоит из тегов. Часть тегов парные: у них есть начало и конец. Элемент может содержать текст, другие элементы и атрибуты."))
    parts.append(image_paragraph("rId14", "Анатомия тега", 1200, 700, 1203))
    parts.append(code_line('<a href="catalog.html" class="menu-link">Каталог</a>'))
    parts.append(bullet("`a` — имя тега."))
    parts.append(bullet("`href` и `class` — атрибуты. Они добавляют свойства и связи."))
    parts.append(bullet("`Каталог` — содержимое элемента."))
    parts.append(bullet("`</a>` — закрывающий тег."))
    parts.append(paragraph_text("Элементы можно вкладывать друг в друга, но важно соблюдать иерархию: если тег открыт внутри другого, закрываться он должен тоже внутри него."))
    parts.append(code_line("<nav>"))
    parts.append(code_line('  <ul>'))
    parts.append(code_line('    <li><a href="/">Главная</a></li>'))
    parts.append(code_line('    <li><a href="/catalog">Каталог</a></li>'))
    parts.append(code_line("  </ul>"))
    parts.append(code_line("</nav>"))

    parts.append(heading("6. Списки, карточки и повторяющиеся блоки"))
    parts.append(paragraph_text("Во многих интерфейсах встречаются повторяющиеся элементы: меню, карточки товаров, новости, шаги инструкций, преимущества. В чистом HTML такой повтор описывается вручную, элемент за элементом."))
    parts.append(code_line("<section>"))
    parts.append(code_line('  <h2>Преимущества курса</h2>'))
    parts.append(code_line("  <ul>"))
    parts.append(code_line("    <li>Понятная структура занятий</li>"))
    parts.append(code_line("    <li>Практика после каждой темы</li>"))
    parts.append(code_line("    <li>Постепенный переход к реальным проектам</li>"))
    parts.append(code_line("  </ul>"))
    parts.append(code_line("</section>"))
    parts.append(note("Сам список создаётся HTML-разметкой. Настоящие циклы обычно появляются позже: в JavaScript, шаблонизаторах или серверной генерации HTML."))

    parts.append(heading("7. Формы, кнопки и запросы"))
    parts.append(paragraph_text("Страница почти всегда содержит хотя бы один способ взаимодействия с пользователем: поиск, авторизация, подписка, фильтр, форма обратной связи. Для этого в HTML есть форма и элементы ввода."))
    parts.append(image_paragraph("rId15", "Форма и GET-запрос", 1200, 660, 1204))
    parts.append(code_line('<form action="/search" method="get">'))
    parts.append(code_line('  <label for="search">Поиск</label>'))
    parts.append(code_line('  <input id="search" name="search" type="text" placeholder="Введите запрос">'))
    parts.append(code_line('  <button type="submit">Найти</button>'))
    parts.append(code_line("</form>"))
    parts.append(bullet("Если `method=\"get\"`, значения полей попадают в адресную строку как query-параметры."))
    parts.append(bullet("Если `method=\"post\"`, данные отправляются в теле запроса."))
    parts.append(bullet("Кнопка `button` сама по себе не магия: она либо отправляет форму, либо запускает логику через JavaScript."))

    parts.append(heading("8. Есть ли в HTML циклы, условия и запросы"))
    parts.append(paragraph_text("Это важный вопрос, потому что новички часто смешивают HTML, CSS, JavaScript и шаблонизаторы."))
    parts.append(bullet("В чистом HTML нет циклов. Нельзя написать \"повтори карточку 10 раз\" средствами стандартного HTML."))
    parts.append(bullet("В чистом HTML нет условий. Нельзя сделать `if/else`, чтобы один блок появился, а другой нет, без помощи JavaScript или шаблонного движка."))
    parts.append(bullet("В HTML есть формы, ссылки и атрибуты, которые участвуют в запросах браузера к серверу."))
    parts.append(bullet("Медиа-запросы `@media` относятся уже к CSS, а не к HTML."))
    parts.append(note("Когда позже ты будешь работать с Jinja2, React, Vue или серверной генерацией страниц, тогда появятся циклы и условия внутри шаблонов. Но основа структуры всё равно останется HTML."))

    parts.append(heading("9. Пример страницы, где собраны главные элементы"))
    parts.append(code_line("<body>"))
    parts.append(code_line('  <header>'))
    parts.append(code_line('    <h1>Учебный портал</h1>'))
    parts.append(code_line('    <nav>'))
    parts.append(code_line('      <a href=\"#about\">О курсе</a>'))
    parts.append(code_line('      <a href=\"#program\">Программа</a>'))
    parts.append(code_line('      <a href=\"#contact\">Контакты</a>'))
    parts.append(code_line('    </nav>'))
    parts.append(code_line('  </header>'))
    parts.append(code_line('  <main>'))
    parts.append(code_line('    <section id=\"about\">'))
    parts.append(code_line('      <h2>О курсе</h2>'))
    parts.append(code_line('      <p>Изучаем HTML, CSS и JavaScript шаг за шагом.</p>'))
    parts.append(code_line('      <img src=\"cover.jpg\" alt=\"Обложка курса\">'))
    parts.append(code_line('    </section>'))
    parts.append(code_line('    <section id=\"program\">'))
    parts.append(code_line('      <h2>Что внутри</h2>'))
    parts.append(code_line('      <ul>'))
    parts.append(code_line('        <li>Структура страницы</li>'))
    parts.append(code_line('        <li>Формы и элементы ввода</li>'))
    parts.append(code_line('        <li>Базовая семантика</li>'))
    parts.append(code_line('      </ul>'))
    parts.append(code_line('    </section>'))
    parts.append(code_line('    <section id=\"contact\">'))
    parts.append(code_line('      <h2>Связаться</h2>'))
    parts.append(code_line('      <form>'))
    parts.append(code_line('        <label for=\"email\">Email</label>'))
    parts.append(code_line('        <input id=\"email\" type=\"email\">'))
    parts.append(code_line('        <button type=\"submit\">Отправить</button>'))
    parts.append(code_line('      </form>'))
    parts.append(code_line('    </section>'))
    parts.append(code_line('  </main>'))
    parts.append(code_line('  <footer>© 2026 Учебный портал</footer>'))
    parts.append(code_line("</body>"))

    parts.append(heading("10. Что нужно запомнить после урока"))
    parts.append(bullet("Любая страница строится вокруг `html`, `head` и `body`."))
    parts.append(bullet("Размещение виджетов начинается с крупных смысловых областей: `header`, `nav`, `main`, `section`, `aside`, `footer`."))
    parts.append(bullet("Самые базовые элементы страницы: заголовки, абзацы, ссылки, изображения, списки, формы и кнопки."))
    parts.append(bullet("HTML задаёт структуру, CSS оформляет, JavaScript добавляет поведение."))
    parts.append(bullet("В чистом HTML нет циклов и условий, но есть формы и механизмы отправки запросов."))
    parts.append(note("Следующий логичный шаг после этого документа — собрать простую страницу и руками разложить на ней header, menu, content, cards, form и footer."))

    body = "".join(parts)
    sect = """
<w:sectPr>
  <w:headerReference w:type="default" r:id="rId8"/>
  <w:footerReference w:type="default" r:id="rId9"/>
  <w:pgSz w:w="11906" w:h="16838"/>
  <w:pgMar w:top="1200" w:right="1080" w:bottom="1200" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
  <w:cols w:space="720"/>
  <w:docGrid w:linePitch="360"/>
</w:sectPr>
""".strip()
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
  xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
  xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
  xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  mc:Ignorable="wp14">
  <w:body>
    {body}
    {sect}
  </w:body>
</w:document>
"""


def build_rels_xml(image_specs: list[tuple[str, Path]]) -> str:
    base = [
        ('rId1', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering", "numbering.xml"),
        ('rId2', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles", "styles.xml"),
        ('rId3', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings", "settings.xml"),
        ('rId4', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/webSettings", "webSettings.xml"),
        ('rId5', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes", "footnotes.xml"),
        ('rId6', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes", "endnotes.xml"),
        ('rId8', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header", "header1.xml"),
        ('rId9', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer", "footer1.xml"),
        ('rId10', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable", "fontTable.xml"),
        ('rId11', "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme", "theme/theme1.xml"),
    ]
    for rid, path in image_specs:
        base.append((rid, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", f"media/{path.name}"))
    rels = "".join(
        f'<Relationship Id="{rid}" Type="{rel_type}" Target="{target}"/>'
        for rid, rel_type, target in base
    )
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>'


def build_core_xml() -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Урок 1 — HTML: база структуры и размещения элементов</dc:title>
  <dc:subject>HTML</dc:subject>
  <dc:creator>Codex</dc:creator>
  <cp:lastModifiedBy>Codex</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>
"""


def generate_docx() -> None:
    ensure_dir(ASSETS_DIR)
    image_specs = [
        ("rId12", build_structure_image()),
        ("rId13", build_layout_image()),
        ("rId14", build_tag_image()),
        ("rId15", build_form_image()),
    ]

    document_xml = build_document_xml(image_specs)
    rels_xml = build_rels_xml(image_specs)
    core_xml = build_core_xml()

    with zipfile.ZipFile(TEMPLATE, "r") as src, zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        skip = {
            "word/document.xml",
            "word/_rels/document.xml.rels",
            "docProps/core.xml",
        }
        for info in src.infolist():
            if info.filename in skip:
                continue
            dst.writestr(info, src.read(info.filename))

        dst.writestr("word/document.xml", document_xml.encode("utf-8"))
        dst.writestr("word/_rels/document.xml.rels", rels_xml.encode("utf-8"))
        dst.writestr("docProps/core.xml", core_xml.encode("utf-8"))
        for _, path in image_specs:
            dst.write(path, f"word/media/{path.name}")


if __name__ == "__main__":
    generate_docx()
    print(OUTPUT)
