import json
import os
from pathlib import Path

_SETTINGS_FILE = Path(__file__).parent / "instance" / "smtp_settings.json"

_DEFAULTS = {
    "smtp_host": os.environ.get("SMTP_HOST", ""),
    "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
    "smtp_user": os.environ.get("SMTP_USER", ""),
    "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
    "smtp_from": os.environ.get("SMTP_FROM", ""),
    "smtp_tls": os.environ.get("SMTP_TLS", "ssl"),  # "ssl" | "starttls"
}


def load() -> dict:
    if _SETTINGS_FILE.exists():
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            merged = dict(_DEFAULTS)
            merged.update({k: v for k, v in data.items() if v != ""})
            return merged
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(host: str, port: int, user: str, password: str,
         from_addr: str, tls: str = "ssl") -> None:
    _SETTINGS_FILE.parent.mkdir(exist_ok=True)
    data = {
        "smtp_host": host.strip(),
        "smtp_port": port,
        "smtp_user": user.strip(),
        "smtp_password": password,
        "smtp_from": from_addr.strip() or user.strip(),
        "smtp_tls": tls if tls in ("ssl", "starttls") else "ssl",
    }
    _SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")


def is_enabled() -> bool:
    s = load()
    return bool(s["smtp_host"] and s["smtp_user"] and s["smtp_password"])
