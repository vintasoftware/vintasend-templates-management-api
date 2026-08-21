"""Shared test wiring.

Tests drive the real Django application through the test client with a real
``ManagedTemplateService`` composed over in-memory seams, so they cover routing, auth,
validation, filter negotiation, lifecycle rules and serialization without needing a
database or a template engine.
"""

from typing import Any, Callable, Iterator

from django.test import Client

import pytest
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from ..service import ServiceCaller, set_service_caller
from .fakes import (
    AUTH_HEADERS,
    TEST_API_KEY,
    FakeEmailRenderer,
    InMemoryTemplateManagerBackend,
)


@pytest.fixture(autouse=True)
def api_key(settings: Any) -> None:
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
        renderer: Any = None,
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


def _with_auth(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Add the bearer token to a request's headers without discarding the caller's.

    A test that passes its own ``headers`` -- an ``Origin`` for the CORS cases -- must not
    lose authentication by doing so.
    """
    return {**kwargs, "headers": {**AUTH_HEADERS, **kwargs.get("headers", {})}}


@pytest.fixture
def get(client: Client) -> Callable[..., Any]:
    def _get(path: str, **kwargs: Any) -> Any:
        return client.get(path, **_with_auth(kwargs))

    return _get


@pytest.fixture
def post(client: Client) -> Callable[..., Any]:
    def _post(path: str, body: Any = None, **kwargs: Any) -> Any:
        return client.post(
            path,
            data=body if body is not None else {},
            content_type="application/json",
            **_with_auth(kwargs),
        )

    return _post


@pytest.fixture
def delete(client: Client) -> Callable[..., Any]:
    def _delete(path: str, **kwargs: Any) -> Any:
        return client.delete(path, **_with_auth(kwargs))

    return _delete


@pytest.fixture
def put(client: Client) -> Callable[..., Any]:
    def _put(path: str, body: Any = None, **kwargs: Any) -> Any:
        return client.put(
            path,
            data=body if body is not None else {},
            content_type="application/json",
            **_with_auth(kwargs),
        )

    return _put


@pytest.fixture
def patch(client: Client) -> Callable[..., Any]:
    def _patch(path: str, body: Any = None, **kwargs: Any) -> Any:
        return client.patch(
            path,
            data=body if body is not None else {},
            content_type="application/json",
            **_with_auth(kwargs),
        )

    return _patch
