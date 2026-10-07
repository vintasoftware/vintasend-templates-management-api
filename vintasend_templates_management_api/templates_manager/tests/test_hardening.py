"""Attribution resolved by the host, redacted logging of unexpected errors, refused deletes.

* ``MANAGED_TEMPLATE_ACTOR_RESOLVER`` -- when set, what every status route records as
  ``changedBy``, replacing anything the request body claims.
* ``MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER`` -- receives every error the API does not map to a
  contract error. The default logs one line with no message, traceback, body or context.
* A delete the library refuses (the version was published) is a 409 ``CONFLICT``.
"""

import logging
import uuid
from typing import TYPE_CHECKING, NoReturn

from django.http import HttpRequest

import pytest
from vintasend_managed_templates.constants import ManagedTemplateStatus

from .conftest import ReadRequest, WriteRequest
from .fakes import InMemoryTemplateManagerBackend


if TYPE_CHECKING:
    from django.conf import LazySettings


SECRET = "patient-diagnosis-that-must-never-be-logged"
LOGGER = "vintasend_templates_management_api"


# --- hooks referenced by dotted path ----------------------------------------------------


def resolve_from_header(request: HttpRequest) -> str | None:
    return request.headers.get("X-Test-Actor")


def resolve_nobody(request: HttpRequest) -> None:
    return None


async def resolve_asynchronously(request: HttpRequest) -> str:
    return "async-actor"


HANDLED: list[tuple[str, str, str]] = []


def record_unhandled(exc: Exception, request: HttpRequest, request_id: str) -> None:
    HANDLED.append((type(exc).__name__, request.method or "", request_id))


async def record_unhandled_asynchronously(
    exc: Exception, request: HttpRequest, request_id: str
) -> None:
    HANDLED.append((type(exc).__name__, "async", request_id))


def failing_handler(exc: Exception, request: HttpRequest, request_id: str) -> NoReturn:
    raise ValueError(f"the handler itself failed while handling {exc}")


@pytest.fixture(autouse=True)
def _clear_handled() -> None:
    HANDLED.clear()


def _published(backend: InMemoryTemplateManagerBackend, version: int = 1) -> None:
    backend.add(key="welcome-email", version=version)
    backend.create_template_status_update("welcome-email", version, ManagedTemplateStatus.ACTIVE)


def _recorded_actors(backend: InMemoryTemplateManagerBackend) -> list[str | None]:
    return [record.created_by for record in backend.history]


# --- actor resolution -------------------------------------------------------------------


STATUS_ROUTES = [
    pytest.param("/api/v1/templates/welcome-email/activate", {}, id="activate"),
    pytest.param("/api/v1/templates/welcome-email/archive", {}, id="archive"),
    pytest.param("/api/v1/templates/welcome-email/status", {"status": "active"}, id="status"),
]


@pytest.mark.parametrize(("path", "body"), STATUS_ROUTES)
def test_a_configured_resolver_replaces_the_changed_by_in_the_body(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    settings: "LazySettings",
    path: str,
    body: dict[str, str],
) -> None:
    settings.MANAGED_TEMPLATE_ACTOR_RESOLVER = f"{__name__}.resolve_from_header"
    backend.add(key="welcome-email", version=1)

    response = post(
        path, {**body, "changedBy": "someone-else"}, headers={"X-Test-Actor": "real-user"}
    )

    assert response.status_code == 200
    assert _recorded_actors(backend) == ["real-user"]


def test_the_resolver_applies_to_deactivate_too(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend, settings: "LazySettings"
) -> None:
    settings.MANAGED_TEMPLATE_ACTOR_RESOLVER = resolve_from_header
    _published(backend)

    response = post(
        "/api/v1/templates/welcome-email/deactivate",
        {"changedBy": "someone-else"},
        headers={"X-Test-Actor": "real-user"},
    )

    assert response.status_code == 200
    assert _recorded_actors(backend)[-1] == "real-user"


def test_a_resolver_answering_none_records_the_change_as_unattributed(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend, settings: "LazySettings"
) -> None:
    settings.MANAGED_TEMPLATE_ACTOR_RESOLVER = f"{__name__}.resolve_nobody"
    backend.add(key="welcome-email", version=1)

    post("/api/v1/templates/welcome-email/activate", {"changedBy": "someone-else"})

    assert _recorded_actors(backend) == [None]


def test_the_resolver_may_be_asynchronous(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend, settings: "LazySettings"
) -> None:
    settings.MANAGED_TEMPLATE_ACTOR_RESOLVER = resolve_asynchronously
    backend.add(key="welcome-email", version=1)

    post("/api/v1/templates/welcome-email/activate", {"changedBy": "someone-else"})

    assert _recorded_actors(backend) == ["async-actor"]


def test_without_a_resolver_changed_by_still_comes_from_the_body(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend, settings: "LazySettings"
) -> None:
    settings.MANAGED_TEMPLATE_ACTOR_RESOLVER = ""
    backend.add(key="welcome-email", version=1)

    post("/api/v1/templates/welcome-email/activate", {"changedBy": "from-the-body"})

    assert _recorded_actors(backend) == ["from-the-body"]


# --- unhandled errors -------------------------------------------------------------------


def _explode_on_preview(backend: InMemoryTemplateManagerBackend) -> None:
    def explode(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError(f"template engine failed rendering {SECRET}")

    backend.add(key="welcome-email", version=1)
    backend.get_template = explode  # type: ignore[method-assign]


def test_the_default_handler_logs_one_redacted_line(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _explode_on_preview(backend)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        response = post(
            "/api/v1/templates/welcome-email/preview", {"context": {"diagnosis": SECRET}}
        )

    request_id = response.headers["X-Request-Id"]
    records = [r for r in caplog.records if r.name.startswith(LOGGER)]
    assert len(records) == 1
    record = records[0]
    message = record.getMessage()
    assert "RuntimeError" in message
    assert request_id in message
    assert "POST" in message
    assert "api/v1/templates/<key>/preview" in message
    assert "welcome-email" not in message
    assert record.exc_info is None
    assert record.exc_text is None
    for logged in caplog.records:
        assert SECRET not in logged.getMessage()
        assert logged.exc_info is None or SECRET not in str(logged.exc_info[1])


def test_the_500_body_stays_generic_and_carries_the_request_id(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    _explode_on_preview(backend)

    response = post("/api/v1/templates/welcome-email/preview", {"context": {"x": SECRET}})

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred while handling the request.",
        }
    }
    assert uuid.UUID(response.headers["X-Request-Id"])


def test_a_safe_client_request_id_is_reused(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend, caplog: pytest.LogCaptureFixture
) -> None:
    _explode_on_preview(backend)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        response = post(
            "/api/v1/templates/welcome-email/preview",
            {},
            headers={"X-Request-Id": "trace.ABC-123_x"},
        )

    assert response.headers["X-Request-Id"] == "trace.ABC-123_x"
    assert "trace.ABC-123_x" in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    "supplied",
    [
        pytest.param("abc\nFORGED log line", id="newline"),
        pytest.param("abc\n", id="trailing-newline"),
        pytest.param("a" * 129, id="too-long"),
        pytest.param("has space", id="space"),
        pytest.param("", id="empty"),
    ],
)
def test_an_unsafe_client_request_id_is_replaced(
    client: object,
    backend: InMemoryTemplateManagerBackend,
    caplog: pytest.LogCaptureFixture,
    supplied: str,
) -> None:
    from django.test import Client

    from .fakes import AUTH_HEADERS

    assert isinstance(client, Client)
    _explode_on_preview(backend)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        # Through the WSGI environ, since the header API refuses a newline outright.
        response = client.post(
            "/api/v1/templates/welcome-email/preview",
            data={},
            content_type="application/json",
            headers=AUTH_HEADERS,
            HTTP_X_REQUEST_ID=supplied,
        )

    request_id = response.headers["X-Request-Id"]
    assert request_id != supplied
    assert uuid.UUID(request_id)
    assert "FORGED" not in caplog.records[0].getMessage()


def test_an_injected_handler_receives_the_error_instead(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    settings: "LazySettings",
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER = f"{__name__}.record_unhandled"
    _explode_on_preview(backend)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        response = post("/api/v1/templates/welcome-email/preview", {})

    assert response.status_code == 500
    assert HANDLED == [("RuntimeError", "POST", response.headers["X-Request-Id"])]
    assert [r for r in caplog.records if r.name.startswith(LOGGER)] == []


def test_an_injected_handler_may_be_asynchronous(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend, settings: "LazySettings"
) -> None:
    settings.MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER = record_unhandled_asynchronously
    _explode_on_preview(backend)

    response = post("/api/v1/templates/welcome-email/preview", {})

    assert HANDLED == [("RuntimeError", "async", response.headers["X-Request-Id"])]


def test_nothing_else_logs_the_error_or_the_concrete_path(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Django's own ``django.request`` 5xx record would repeat the path and carry the request."""
    _explode_on_preview(backend)

    with caplog.at_level(logging.DEBUG):
        post("/api/v1/templates/welcome-email/preview", {"context": {"diagnosis": SECRET}})

    errors = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.name for r in errors] == [f"{LOGGER}.templates_manager.hooks"]
    for record in caplog.records:
        assert "welcome-email" not in record.getMessage()
        assert SECRET not in record.getMessage()


def test_a_failing_injected_handler_falls_back_to_the_default_line(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    settings: "LazySettings",
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The error is still recorded, and what the handler raised is not."""
    settings.MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER = failing_handler
    _explode_on_preview(backend)

    with caplog.at_level(logging.DEBUG):
        response = post("/api/v1/templates/welcome-email/preview", {})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert [r.getMessage() for r in caplog.records if r.name.startswith(LOGGER)] == [
        "Unhandled RuntimeError (request "
        f"{response.headers['X-Request-Id']}) on POST api/v1/templates/<key>/preview"
    ]
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert "the handler itself failed" not in record.getMessage()


def test_an_unresolvable_handler_falls_back_to_the_default_line(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    settings: "LazySettings",
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER = "no_such_module.handler"
    _explode_on_preview(backend)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        response = post("/api/v1/templates/welcome-email/preview", {})

    assert response.status_code == 500
    assert [r.getMessage() for r in caplog.records if r.name.startswith(LOGGER)] == [
        "Unhandled RuntimeError (request "
        f"{response.headers['X-Request-Id']}) on POST api/v1/templates/<key>/preview"
    ]


# --- deletes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/v1/templates/welcome-email/versions/1", id="by-version"),
        pytest.param("/api/v1/templates/welcome-email?version=1", id="by-query"),
        pytest.param("/api/v1/templates/welcome-email", id="latest"),
    ],
)
def test_deleting_a_published_version_is_a_409(
    delete: ReadRequest, backend: InMemoryTemplateManagerBackend, path: str
) -> None:
    _published(backend)

    response = delete(path)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CONFLICT"
    assert "archive" in error["message"]
    assert [template.version for template in backend.templates] == [1]


def test_a_never_published_draft_is_still_deleted(
    delete: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    _published(backend, 1)
    backend.add(key="welcome-email", version=2)

    response = delete("/api/v1/templates/welcome-email")

    assert response.status_code == 204
    assert [template.version for template in backend.templates] == [1]


def test_a_key_with_no_active_version_previews_its_draft_by_default(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """Previews read the editing view: the newest version, whatever its status."""
    backend.add(key="welcome-email", version=1, body_template="<p>draft</p>")

    response = post("/api/v1/templates/welcome-email/preview", {})

    assert response.status_code == 200
