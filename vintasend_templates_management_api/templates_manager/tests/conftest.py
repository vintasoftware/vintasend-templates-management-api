"""Shared test wiring.

Tests drive the real Django application through the test client with a real
``ManagedTemplateService`` composed over in-memory seams, so they cover routing, auth,
validation, filter negotiation, lifecycle rules and serialization without needing a
database or a template engine.
"""

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Callable, Protocol

from django.test import Client

import pytest
from pydantic import JsonValue
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateRenderer
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from ..service import ServiceCaller, set_service_caller
from .fakes import (
    AUTH_HEADERS,
    TEST_API_KEY,
    FakeEmailRenderer,
    InMemoryTemplateManagerBackend,
)


if TYPE_CHECKING:
    # What `Client.get` and friends actually return: an `HttpResponse` with `.json()`
    # patched on for tests. django-stubs only names it privately, and naming it here is
    # still better than widening every fixture's return type to `Any` -- the alternative
    # loses `.status_code` and header access as well.
    from django.conf import LazySettings
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse


class ReadRequest(Protocol):
    """A request fixture that sends no body."""

    def __call__(self, path: str, headers: Mapping[str, str] | None = None) -> "TestResponse": ...


class WriteRequest(Protocol):
    """A request fixture that sends a JSON body.

    ``body`` is ``JsonValue`` rather than a schema type on purpose: these tests post
    malformed and partial payloads to assert the API rejects them, which a typed body
    would make impossible to express.
    """

    # `Mapping[str, JsonValue]` alongside `JsonValue`: a plain `dict[str, str]` literal is
    # not a `JsonValue`, because `dict` is invariant in its value type. `Mapping` is
    # covariant, so this accepts the dict literals tests actually write while still
    # rejecting a body that could not be serialized.
    def __call__(
        self,
        path: str,
        body: Mapping[str, JsonValue] | JsonValue | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> "TestResponse": ...


@pytest.fixture(autouse=True)
def api_key(settings: "LazySettings") -> None:
    settings.VINTASEND_API_KEY = TEST_API_KEY


@pytest.fixture
def reset_injected_service() -> Iterator[None]:
    """Clear the process-wide service around every test.

    It is cached for the life of the process in production, which is exactly what a test
    must not inherit from the test before it.

    Deliberately not autouse: an autouse fixture is ordered against the other autouse
    fixtures, and this one has to run *before* the service is installed, not merely at some
    point during setup. Making it a dependency of ``install_service`` is what guarantees
    that -- as autouse it teardown-cleared the service the same setup had just installed.
    """
    set_service_caller(None)
    yield
    set_service_caller(None)


@pytest.fixture
def backend() -> InMemoryTemplateManagerBackend:
    return InMemoryTemplateManagerBackend()


@pytest.fixture
def install_service(
    reset_injected_service: None,
    backend: InMemoryTemplateManagerBackend,
) -> Callable[..., ManagedTemplateService]:
    """Install a service over the in-memory backend and return it.

    Both seams are overridable so a test can vary one without rebuilding the wiring: a
    renderer that raises, a backend that declares reduced capabilities, a service with the
    transition table turned off.
    """

    def _install(
        template_backend: InMemoryTemplateManagerBackend | None = None,
        renderer: ManagedTemplateRenderer | None = None,
        validate_status_transitions: bool = True,
    ) -> ManagedTemplateService:
        service = ManagedTemplateService(
            template_manager_backend=template_backend or backend,
            template_renderer=renderer or FakeEmailRenderer(),
            validate_status_transitions=validate_status_transitions,
        )
        set_service_caller(ServiceCaller(service))
        return service

    return _install


@pytest.fixture(autouse=True)
def default_service(install_service: Callable[..., ManagedTemplateService]) -> None:
    """Every test gets a working service unless it installs a different one.

    Re-installing simply replaces this one, so a test that needs a variant just calls
    ``install_service`` again.
    """
    install_service()


@pytest.fixture
def client() -> Client:
    return Client()


def _with_auth(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Add the bearer token to a request's headers without discarding the caller's.

    A test that passes its own ``headers`` -- an ``Origin`` for the CORS cases -- must not
    lose authentication by doing so.
    """
    return {**AUTH_HEADERS, **(headers or {})}


@pytest.fixture
def get(client: Client) -> ReadRequest:
    def _get(path: str, headers: Mapping[str, str] | None = None) -> "TestResponse":
        return client.get(path, headers=_with_auth(headers))

    return _get


@pytest.fixture
def post(client: Client) -> WriteRequest:
    def _post(
        path: str,
        body: Mapping[str, JsonValue] | JsonValue | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> "TestResponse":
        return client.post(
            path,
            data=body if body is not None else {},
            content_type="application/json",
            headers=_with_auth(headers),
        )

    return _post


@pytest.fixture
def delete(client: Client) -> ReadRequest:
    def _delete(path: str, headers: Mapping[str, str] | None = None) -> "TestResponse":
        return client.delete(path, headers=_with_auth(headers))

    return _delete


@pytest.fixture
def put(client: Client) -> WriteRequest:
    def _put(
        path: str,
        body: Mapping[str, JsonValue] | JsonValue | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> "TestResponse":
        return client.put(
            path,
            data=body if body is not None else {},
            content_type="application/json",
            headers=_with_auth(headers),
        )

    return _put


@pytest.fixture
def patch(client: Client) -> WriteRequest:
    def _patch(
        path: str,
        body: Mapping[str, JsonValue] | JsonValue | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> "TestResponse":
        return client.patch(
            path,
            data=body if body is not None else {},
            content_type="application/json",
            headers=_with_auth(headers),
        )

    return _patch
