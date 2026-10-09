"""The two host hooks: who made a status change, and what happens to an unexpected error.

Both are settings naming a callable, as a dotted path (what an environment variable can carry)
or as the callable itself (what a ``settings.py`` can assign). ``configured_hook`` resolves
them, and ``VINTASEND_API_AUTHENTICATOR`` too (see ``auth.py``), so all three fail the system
checks the same way when they cannot be used:

``MANAGED_TEMPLATE_ACTOR_RESOLVER`` -- ``(request) -> str | None``
    Who is making the request, for the status audit trail. When set, its answer is what every
    status route records as ``changedBy``, and any ``changedBy`` in the request body is ignored,
    so a caller holding the API key cannot write someone else's identity into the trail.
    ``None`` records the change as unattributed. It may be ``async``. When it is not set,
    ``changedBy`` comes from the body, which is only safe when everyone holding the API key is
    trusted to attribute honestly.

``MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER`` -- ``(exc, request, request_id) -> None``
    Receives every error the API does not map to a contract error, before the generic 500 is
    sent. It may be ``async``. It gets the exception itself, so keeping health data out of
    wherever it sends it is the host's responsibility. When it is not set, or when it raises,
    ``log_unhandled_error`` writes one redacted line instead.

Unexpected errors are not logged whole by default because an error from the template store or
the template engine can carry template content or values from a preview's context, and the
applications this API serves handle health data.
"""

import inspect
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import cast

from django.http import HttpRequest
from django.utils.module_loading import import_string

from asgiref.sync import async_to_sync

from . import conf


logger = logging.getLogger(__name__)

ActorResolver = Callable[[HttpRequest], "str | None | Awaitable[str | None]"]
UnhandledErrorHandler = Callable[[Exception, HttpRequest, str], "None | Awaitable[None]"]

REQUEST_ID_HEADER = "X-Request-Id"

# Only an id that cannot break a log line out of its field is taken from the client. Matched
# with ``fullmatch``: ``$`` would also accept a trailing newline.
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def configured_hook(setting_name: str) -> Callable[..., object] | None:
    """The callable a hook setting names, or None when it is unset.

    raises ImportError: if a dotted path cannot be imported.
    raises TypeError: if the setting names something that is not callable.
    """
    value = conf.hook_setting(setting_name)
    if not value:
        return None
    hook = import_string(value) if isinstance(value, str) else value
    if not callable(hook):
        raise TypeError(f"{setting_name} must name a callable, not {type(hook).__name__}.")
    return cast(Callable[..., object], hook)


# --- attribution ------------------------------------------------------------------------


def resolve_changed_by(request: HttpRequest, body_changed_by: str | None) -> str | None:
    """What a status change records as ``changedBy``.

    The configured resolver's answer when there is one, ``None`` included; the body's value
    only when no resolver is configured.
    """
    resolver = configured_hook(conf.ACTOR_RESOLVER)
    if resolver is None:
        return body_changed_by
    actor = resolver(request)
    if inspect.isawaitable(actor):
        actor = async_to_sync(_awaited)(actor)
    return cast("str | None", actor)


async def _awaited(awaitable: Awaitable[object]) -> object:
    return await awaitable


# --- unexpected errors ------------------------------------------------------------------


def request_id_for(request: HttpRequest) -> str:
    """The caller's ``X-Request-Id`` when it is safe to log, otherwise a fresh UUID."""
    supplied = request.META.get("HTTP_X_REQUEST_ID", "")
    if isinstance(supplied, str) and _SAFE_REQUEST_ID.fullmatch(supplied):
        return supplied
    return str(uuid.uuid4())


def route_pattern(request: HttpRequest) -> str:
    """The URL pattern the request matched, with no path values filled in."""
    match = request.resolver_match
    return match.route if match is not None and match.route else "<unresolved route>"


def log_unhandled_error(exc: Exception, request: HttpRequest, request_id: str) -> None:
    """The default handler: one line, with no message, traceback, body or preview context.

    The error's class name, the request id the 500 carries in its ``X-Request-Id`` header, the
    method and the matched route pattern -- enough to find the request, and nothing that came
    from the template store, the template engine or the caller's payload.
    """
    logger.error(
        "Unhandled %s (request %s) on %s %s",
        type(exc).__name__,
        request_id,
        request.method,
        route_pattern(request),
    )


def report_unhandled_error(exc: Exception, request: HttpRequest, request_id: str) -> None:
    """Hand ``exc`` to the configured handler, or to ``log_unhandled_error``.

    A handler that raises, or a handler setting that cannot be resolved, falls back to the
    default line, so the error is still recorded somewhere. What a failing handler raised is
    never logged: it is no safer than the error it was handling, and it must not turn a 500
    into a crash.
    """
    try:
        handler = configured_hook(conf.UNHANDLED_ERROR_HANDLER)
    except Exception:
        handler = None
    if handler is not None:
        try:
            outcome = handler(exc, request, request_id)
            if inspect.isawaitable(outcome):
                async_to_sync(_awaited)(outcome)
            return
        except Exception:  # noqa: S110 - its own error is not logged, see the docstring
            pass
    try:
        log_unhandled_error(exc, request, request_id)
    except Exception:  # noqa: S110 - a broken logging setup must not turn a 500 into a crash
        pass
