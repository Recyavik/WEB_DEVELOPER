import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
(INSTANCE_DIR / "books").mkdir(exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "morphosyntax-2026-xK9pQr")
DATABASE_URL = f"sqlite:///{INSTANCE_DIR / 'morpho.db'}"

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

EMAIL_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)
