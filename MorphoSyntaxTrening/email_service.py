import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtp_settings

SENDER_NAME = "MorphoSyntaxTrening"


def _make_msg(subject: str, from_addr: str, to: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def _connect(s: dict):
    """Return an authenticated SMTP connection. Falls back automatically on SSL errors."""
    host, port = s["smtp_host"], s["smtp_port"]
    tls = s.get("smtp_tls", "ssl")
    ctx = ssl.create_default_context()
    if tls == "ssl":
        try:
            srv = smtplib.SMTP_SSL(host, port, context=ctx, timeout=10)
        except ssl.SSLError:
            # Server may expect STARTTLS despite SSL port — try fallback
            srv = smtplib.SMTP(host, port, timeout=10)
            srv.starttls(context=ctx)
    else:
        srv = smtplib.SMTP(host, port, timeout=10)
        srv.starttls(context=ctx)
    srv.login(s["smtp_user"], s["smtp_password"])
    return srv


def _send(to: str, subject: str, html: str) -> bool:
    s = smtp_settings.load()
    if not (s["smtp_host"] and s["smtp_user"] and s["smtp_password"]):
        return False
    from_addr = f"{SENDER_NAME} <{s['smtp_user']}>"
    try:
        msg = _make_msg(subject, from_addr, to, html)
        with _connect(s) as srv:
            srv.send_message(msg)
        return True
    except Exception:
        return False


def send_test(to: str) -> tuple[bool, str]:
    s = smtp_settings.load()
    if not (s["smtp_host"] and s["smtp_user"] and s["smtp_password"]):
        return False, "SMTP не настроен — заполните настройки почты"
    from_addr = f"{SENDER_NAME} <{s['smtp_user']}>"
    try:
        msg = _make_msg(
            "Тест почты — MorphoSyntaxTrening", from_addr, to,
            "<h2>Тест успешен!</h2><p>Почта MorphoSyntaxTrening настроена корректно.</p>")
        with _connect(s) as srv:
            srv.send_message(msg)
        return True, f"Тестовое письмо отправлено на {to}"
    except smtplib.SMTPAuthenticationError:
        return False, ("Ошибка входа. Для Яндекса нужен пароль приложения: "
                       "id.yandex.ru → Безопасность → Пароли приложений.")
    except smtplib.SMTPConnectError:
        return False, f"Не удалось подключиться к {s['smtp_host']}:{s['smtp_port']}"
    except smtplib.SMTPSenderRefused as e:
        return False, (f"Неверный формат поля «От кого»: {e.sender}. "
                       "Используйте формат: Название <email@domain.ru>")
    except Exception as e:
        return False, f"Ошибка: {e}"


SITE_URL = "https://morpho.appswire.ru"


def send_student_welcome(to: str, student_name: str, teacher_code: str,
                          group_name: str, password: str) -> bool:
    html = f"""
    <h2>Добро пожаловать, {student_name}!</h2>
    <p>Вы добавлены в группу <b>{group_name}</b>.</p>
    <p>Для входа в тренажёр вам понадобится:</p>
    <ul>
      <li><b>Код учителя:</b> {teacher_code}</li>
      <li><b>Ваше имя:</b> {student_name}</li>
      <li><b>Пароль:</b> {password}</li>
    </ul>
    <p>Сайт: <a href="{SITE_URL}">{SITE_URL}</a></p>
    """
    return _send(to, "Ваши данные для входа в MorphoSyntaxTrening", html)


def send_password_reset(to: str, student_name: str, teacher_code: str,
                         new_password: str) -> bool:
    html = f"""
    <h2>Восстановление пароля</h2>
    <p>Здравствуйте, {student_name}!</p>
    <p>Ваш новый пароль для входа в тренажёр:</p>
    <ul>
      <li><b>Код учителя:</b> {teacher_code}</li>
      <li><b>Новый пароль:</b> {new_password}</li>
    </ul>
    <p>Сайт: <a href="{SITE_URL}">{SITE_URL}</a></p>
    """
    return _send(to, "Новый пароль — MorphoSyntaxTrening", html)


def send_teacher_welcome(to: str, teacher_name: str, teacher_code: str,
                          password: str) -> bool:
    html = f"""
    <h2>Добро пожаловать, {teacher_name}!</h2>
    <p>Вам создан аккаунт учителя в системе <b>MorphoSyntaxTrening</b>.</p>
    <p>Данные для входа:</p>
    <ul>
      <li><b>Сайт:</b> <a href="{SITE_URL}">{SITE_URL}</a></li>
      <li><b>Email:</b> {to}</li>
      <li><b>Пароль:</b> {password}</li>
    </ul>
    <p>Ваш код учителя (сообщите ученикам для входа):</p>
    <p style="font-size:28px;font-weight:bold;letter-spacing:6px;color:#1d4ed8;">{teacher_code}</p>
    <p>После входа рекомендуем сменить пароль.</p>
    """
    return _send(to, f"Приглашение в MorphoSyntaxTrening — код {teacher_code}", html)


def send_teacher_password_reset(to: str, teacher_name: str, new_password: str) -> bool:
    html = f"""
    <h2>Смена пароля</h2>
    <p>Здравствуйте, {teacher_name}!</p>
    <p>Администратор сбросил ваш пароль в системе <b>MorphoSyntaxTrening</b>.</p>
    <ul>
      <li><b>Сайт:</b> <a href="{SITE_URL}">{SITE_URL}</a></li>
      <li><b>Новый пароль:</b> {new_password}</li>
    </ul>
    <p>После входа рекомендуем сменить пароль.</p>
    """
    return _send(to, "Новый пароль учителя — MorphoSyntaxTrening", html)


def send_teacher_invite_reminder(to: str, teacher_name: str, teacher_code: str) -> bool:
    html = f"""
    <h2>Напоминание о входе, {teacher_name}!</h2>
    <p>Ваш аккаунт учителя в системе <b>MorphoSyntaxTrening</b> активен.</p>
    <p>Данные для входа:</p>
    <ul>
      <li><b>Сайт:</b> <a href="{SITE_URL}">{SITE_URL}</a></li>
      <li><b>Email:</b> {to}</li>
    </ul>
    <p>Ваш код учителя (сообщите ученикам для входа):</p>
    <p style="font-size:28px;font-weight:bold;letter-spacing:6px;color:#1d4ed8;">{teacher_code}</p>
    <p>Если вы забыли пароль, обратитесь к администратору.</p>
    """
    return _send(to, f"Напоминание о входе — MorphoSyntaxTrening", html)


def send_teacher_status_change(to: str, teacher_name: str, is_active: bool) -> bool:
    status_text = "активирован" if is_active else "деактивирован"
    status_color = "#15803d" if is_active else "#dc2626"
    action = (f'Вы можете войти на сайт: <a href="{SITE_URL}">{SITE_URL}</a>'
              if is_active else "Обратитесь к администратору для восстановления доступа.")
    html = f"""
    <h2>Изменение статуса аккаунта</h2>
    <p>Здравствуйте, {teacher_name}!</p>
    <p>Ваш аккаунт учителя в системе <b>MorphoSyntaxTrening</b> был
       <span style="color:{status_color};font-weight:bold;">{status_text}</span>.</p>
    <p>{action}</p>
    """
    return _send(to, f"Аккаунт {status_text} — MorphoSyntaxTrening", html)
