"""Every /api/v1 route is behind the shared secret; /health is not."""

from typing import NoReturn

from django.conf import LazySettings
from django.test import Client

import pytest

from .conftest import ReadRequest
from .fakes import AUTH_HEADERS, TEST_API_KEY, InMemoryTemplateManagerBackend


PROTECTED_PATHS = [
    "/api/v1/capabilities",
    "/api/v1/templates",
    "/api/v1/templates/welcome-email",
    "/api/v1/templates/welcome-email/versions",
    "/api/v1/templates/welcome-email/versions/1",
    "/api/v1/templates/welcome-email/status-history",
]


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_rejects_a_request_with_no_key(client: Client, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_rejects_a_wrong_key(client: Client, path: str) -> None:
    response = client.get(path, headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_rejects_a_header_that_is_not_a_bearer_token(client: Client) -> None:
    response = client.get("/api/v1/templates", headers={"Authorization": TEST_API_KEY})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_rejects_every_request_when_no_key_is_configured(
    client: Client, settings: "LazySettings"
) -> None:
    """A deployment that never set the key must not accept the empty string as one."""
    settings.VINTASEND_API_KEY = ""

    response = client.get("/api/v1/templates", headers=AUTH_HEADERS)

    assert response.status_code == 401


def test_health_needs_no_key(client: Client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "apiVersion": "v1"}


def test_an_unmatched_path_uses_the_error_envelope(client: Client) -> None:
    """A mistyped URL outside /api/v1 gets JSON, not Django's HTML error page."""
    response = client.get("/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_an_unmatched_api_path_uses_the_error_envelope(get: ReadRequest) -> None:
    response = get("/api/v1/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_an_unexpected_failure_never_leaks_backend_internals(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """A backend blowing up must reach the client as a generic 500, not as a stack trace.

    Driver errors routinely carry connection strings and credentials in their messages,
    which is why the handler reports a fixed message and logs the real one.
    """

    def explode(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("could not connect to postgres://user:hunter2@db:5432/app")

    # Monkeypatched onto the instance: the point is a backend that fails at runtime,
    # which is exactly what a method assignment models and what mypy forbids statically.
    backend.get_paginated_filtered_templates = explode  # type: ignore[method-assign]

    response = get("/api/v1/templates")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in body["message"]
