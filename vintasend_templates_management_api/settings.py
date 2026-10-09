"""Django settings for the VintaSend managed-templates API.

This is a deliberately thin Django project. It has no models, no migrations, no admin
and no user accounts: every template it serves comes from whichever
``ManagedTemplateService`` the operator configures, and any UI in front of it handles
its own user auth ahead of the shared API key checked here.

Everything is read from the environment and validated at import time, so a
misconfigured deployment fails on startup rather than on the first request.
"""

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

# Development convenience only. Production deployments set real environment variables,
# and a missing .env is not an error.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is an optional convenience
    pass
else:
    load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_list(name: str) -> list[str]:
    return [entry.strip() for entry in _env(name).split(",") if entry.strip()]


# --- Django plumbing ---------------------------------------------------------------

# Django refuses to start without a SECRET_KEY, but this project signs nothing: it has
# no sessions, no cookies, no password reset and no CSRF-protected forms. The fallback
# keeps `manage.py` usable in development without pretending the value is a secret.
SECRET_KEY = _env("DJANGO_SECRET_KEY") or "not-a-secret-this-api-signs-nothing"

DEBUG = _env("DJANGO_DEBUG").lower() in {"1", "true", "yes"}

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS") or ["*"]

ROOT_URLCONF = "vintasend_templates_management_api.urls"

WSGI_APPLICATION = "vintasend_templates_management_api.wsgi.application"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "vintasend_templates_management_api.templates_manager.apps.TemplatesManagerConfig",
]

# No sessions, no auth middleware, no CSRF: this API is authenticated by a bearer token
# and serves JSON only. CommonMiddleware is kept for its standard header handling.
MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "vintasend_templates_management_api.templates_manager.cors.CorsMiddleware",
]

# The API itself never touches the ORM. A database is only configured because
# `django.contrib.auth` is installed, which some template-manager backends
# (notably `vintasend-django-templates-manager`) need in order to resolve their models.
DATABASES = {
    "default": {
        "ENGINE": _env("DJANGO_DB_ENGINE") or "django.db.backends.sqlite3",
        "NAME": _env("DJANGO_DB_NAME") or str(BASE_DIR / "db.sqlite3"),
        "USER": _env("DJANGO_DB_USER"),
        "PASSWORD": _env("DJANGO_DB_PASSWORD"),
        "HOST": _env("DJANGO_DB_HOST"),
        "PORT": _env("DJANGO_DB_PORT"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "vintasend_templates_management_api": {
            "handlers": ["console"],
            "level": _env("DJANGO_LOG_LEVEL") or "INFO",
        },
    },
}


# --- API configuration -------------------------------------------------------------

# Shared secret every /api/v1 request must present as `Authorization: Bearer <key>`.
VINTASEND_API_KEY = _env("VINTASEND_API_KEY")

# Browser origins allowed to call the API. Empty means "server-side clients only".
VINTASEND_API_CORS_ORIGINS = _env_list("VINTASEND_API_CORS_ORIGINS")

# Dotted path to a callable returning a configured ManagedTemplateService.
MANAGED_TEMPLATE_SERVICE_FACTORY = _env("MANAGED_TEMPLATE_SERVICE_FACTORY")

# Optional. The backend name every new template is stored under. When set, it replaces the
# `templateManagedBackend` in a create request body, so a host serving one backend does not let
# a caller label a template with another. The field stays required in the request either way.
MANAGED_TEMPLATE_BACKEND_NAME = _env("MANAGED_TEMPLATE_BACKEND_NAME")

# Optional. Dotted path to a callable `(request) -> str | None` naming who made a status change.
# When set, it replaces any `changedBy` in the request body. See templates_manager/hooks.py.
MANAGED_TEMPLATE_ACTOR_RESOLVER = _env("MANAGED_TEMPLATE_ACTOR_RESOLVER")

# Optional. Dotted path to a callable `(exc, request, request_id) -> None` that receives every
# unexpected error. Unset, one redacted line is logged. See templates_manager/hooks.py.
MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER = _env("MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER")
