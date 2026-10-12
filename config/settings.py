"""Environment-driven settings for local development and PostgreSQL deployment."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "local-development-only-change-me")
if not DEBUG and SECRET_KEY == "local-development-only-change-me":
    raise RuntimeError("DJANGO_SECRET_KEY must be set when DEBUG is false")

ALLOWED_HOSTS = [
    host.strip() for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()
]
INSTALLED_APPS = ["django.contrib.contenttypes", "inventory"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware"]
ROOT_URLCONF = "config.urls"
TEMPLATES = []
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

if os.getenv("DB_ENGINE", "sqlite") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "inventory"),
            "USER": os.getenv("DB_USER", "inventory"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"connect_timeout": 5},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("SQLITE_PATH", str(BASE_DIR / "data" / "inventory.sqlite3")),
        }
    }

SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "false").lower() == "true"
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if os.getenv("TRUST_PROXY_SSL_HEADER") == "true" else None
)

MAX_REQUEST_BYTES = 64_000
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_REQUEST_BYTES
MAX_HOLD_SECONDS = 900
DEFAULT_HOLD_SECONDS = 300
OUTBOX_MAX_ATTEMPTS = 8
OUTBOX_LEASE_SECONDS = 60
WEBHOOK_TIMEOUT_SECONDS = 5
WEBHOOK_SIGNING_SECRET = os.getenv("WEBHOOK_SIGNING_SECRET", "")
WEBHOOK_ALLOW_HTTP = DEBUG and os.getenv("WEBHOOK_ALLOW_HTTP", "false").lower() == "true"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "inventory.logging.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
