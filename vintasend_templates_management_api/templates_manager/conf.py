"""Every setting the app reads, with the default it takes when a host leaves it out.

The bundled project's ``settings.py`` defines all of them from the environment, but a host
project that embeds the app defines only what it needs. Each one is read here, through
``getattr`` with a default, and nowhere else, so a setting the host never heard of is a
default rather than an ``AttributeError`` on the first request.

Read on every call rather than captured at import, so a test using ``override_settings`` --
and a deployment that reloads settings -- sees the change.

Required, with an empty default that the system checks report:

- ``MANAGED_TEMPLATE_SERVICE_FACTORY`` -- dotted path to the service factory.
- ``VINTASEND_API_KEY`` -- the shared bearer secret. Not required when an authenticator is set.

Optional:

- ``VINTASEND_API_AUTHENTICATOR`` -- ``(request) -> None``, replacing the shared-key check.
- ``VINTASEND_API_CORS_ORIGINS`` -- browser origins the CORS middleware allows; none by default.
- ``MANAGED_TEMPLATE_BACKEND_NAME`` -- backend name every new template is stored under.
- ``MANAGED_TEMPLATE_ACTOR_RESOLVER`` -- ``(request) -> str | None``, who made a status change.
- ``MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER`` -- ``(exc, request, request_id) -> None``.

The three callables are resolved by ``hooks.configured_hook``, which reads them through
``hook_setting`` below.
"""

from collections.abc import Iterable

from django.conf import settings


AUTHENTICATOR = "VINTASEND_API_AUTHENTICATOR"
API_KEY = "VINTASEND_API_KEY"
SERVICE_FACTORY = "MANAGED_TEMPLATE_SERVICE_FACTORY"
CORS_ORIGINS = "VINTASEND_API_CORS_ORIGINS"
BACKEND_NAME = "MANAGED_TEMPLATE_BACKEND_NAME"
ACTOR_RESOLVER = "MANAGED_TEMPLATE_ACTOR_RESOLVER"
UNHANDLED_ERROR_HANDLER = "MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER"


def _text(name: str) -> str:
    value = getattr(settings, name, "")
    return value if isinstance(value, str) else ""


def api_key() -> str:
    """The shared secret, or ``""`` when there is none -- which no request can match."""
    return _text(API_KEY)


def service_factory() -> str:
    """The dotted path to the service factory, or ``""`` when it is not set."""
    return _text(SERVICE_FACTORY)


def backend_name() -> str:
    """The backend name every new template is stored under, or ``""`` to keep the body's."""
    return _text(BACKEND_NAME)


def cors_origins() -> list[str]:
    """The browser origins allowed to call the API. Empty means none.

    A host's ``settings.py`` may write it as one comma-separated string, the way the
    environment variable carries it, as readily as a list.
    """
    value: str | Iterable[str] | None = getattr(settings, CORS_ORIGINS, None)
    if not value:
        return []
    entries = value.split(",") if isinstance(value, str) else value
    return [entry.strip() for entry in entries if entry.strip()]


def hook_setting(name: str) -> object:
    """A hook setting's raw value -- a callable, a dotted path, or ``None`` when unset."""
    return getattr(settings, name, None)
