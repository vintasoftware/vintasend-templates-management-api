"""Loading the operator-provided service, and the startup checks that guard it."""

from django.conf import LazySettings
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import Client, override_settings

import pytest
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from ..apps import check_api_configuration
from ..service import (
    ServiceConfigurationError,
    get_service_caller,
    load_template_service,
    set_service_caller,
)
from .conftest import ReadRequest
from .fakes import FakeEmailRenderer, InMemoryTemplateManagerBackend


def build_service() -> ManagedTemplateService:
    """A module-level factory, since the loader resolves callables by dotted path."""
    return ManagedTemplateService(
        template_manager_backend=InMemoryTemplateManagerBackend(),
        template_renderer=FakeEmailRenderer(),
    )


def returns_nothing() -> None:
    return None


def raises_on_call() -> ManagedTemplateService:
    raise RuntimeError("the database is not reachable")


def returns_the_wrong_type() -> object:
    """A factory that returns something that is not a service at all.

    Stands in for the realistic version of this mistake: a factory pointed at the backend,
    or at the renderer, rather than at the service that composes them.
    """
    return InMemoryTemplateManagerBackend()


NOT_CALLABLE = "a string, not a factory"

FACTORY_PATH = f"{__name__}.build_service"


# --- system checks -------------------------------------------------------------------


@override_settings(VINTASEND_API_KEY="", MANAGED_TEMPLATE_SERVICE_FACTORY="")
def test_reports_both_missing_settings() -> None:
    errors = check_api_configuration(None)

    assert sorted(str(error.id) for error in errors) == [
        "vintasend_templates_management_api.E001",
        "vintasend_templates_management_api.E002",
    ]


@override_settings(VINTASEND_API_KEY="k", MANAGED_TEMPLATE_SERVICE_FACTORY=FACTORY_PATH)
def test_a_configured_deployment_passes_its_checks() -> None:
    assert check_api_configuration(None) == []


@override_settings(VINTASEND_API_KEY="", MANAGED_TEMPLATE_SERVICE_FACTORY="")
def test_manage_py_check_fails_on_a_misconfigured_deployment() -> None:
    """The check runs under `manage.py check`, which is what a release step calls.

    Asserted through the real command rather than by calling the check function, because
    the guarantee being claimed is that a deployment *fails to start* -- which depends on
    the check being registered, not merely on it returning errors.
    """
    with pytest.raises(SystemCheckError) as failure:
        call_command("check")

    assert "vintasend_templates_management_api.E001" in str(failure.value)
    assert "vintasend_templates_management_api.E002" in str(failure.value)


@pytest.mark.parametrize(
    ("setting_name", "check_id"),
    [
        ("MANAGED_TEMPLATE_ACTOR_RESOLVER", "vintasend_templates_management_api.E003"),
        ("MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER", "vintasend_templates_management_api.E004"),
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("no_such_module.resolver", id="unimportable"),
        pytest.param(f"{__name__}.NOT_CALLABLE", id="not-callable"),
    ],
)
def test_a_hook_that_cannot_be_used_fails_the_checks(
    settings: LazySettings, setting_name: str, check_id: str, value: str
) -> None:
    settings.VINTASEND_API_KEY = "k"
    settings.MANAGED_TEMPLATE_SERVICE_FACTORY = FACTORY_PATH
    setattr(settings, setting_name, value)

    assert [str(error.id) for error in check_api_configuration(None)] == [check_id]


@override_settings(
    VINTASEND_API_KEY="k",
    MANAGED_TEMPLATE_SERVICE_FACTORY=FACTORY_PATH,
    MANAGED_TEMPLATE_ACTOR_RESOLVER=f"{__name__}.build_service",
    MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER=returns_nothing,
)
def test_usable_hooks_pass_the_checks() -> None:
    assert check_api_configuration(None) == []


# --- loading the factory -------------------------------------------------------------


def test_loads_a_service_from_a_dotted_path() -> None:
    assert isinstance(load_template_service(FACTORY_PATH), ManagedTemplateService)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("", "MANAGED_TEMPLATE_SERVICE_FACTORY is not set"),
        (f"{__name__}.no_such_name", "Could not import"),
        ("no.such.module.factory", "Could not import"),
        (f"{__name__}.NOT_CALLABLE", "is not callable"),
        (f"{__name__}.raises_on_call", "failed"),
        (f"{__name__}.returns_nothing", "did not return a service"),
        (
            f"{__name__}.returns_the_wrong_type",
            "returned a InMemoryTemplateManagerBackend, not a ManagedTemplateService",
        ),
    ],
)
def test_reports_why_a_factory_could_not_be_used(path: str, expected: str) -> None:
    with pytest.raises(ServiceConfigurationError, match=expected):
        load_template_service(path)


# --- process-wide caching ------------------------------------------------------------


@override_settings(MANAGED_TEMPLATE_SERVICE_FACTORY=FACTORY_PATH)
def test_the_service_is_built_once_and_reused() -> None:
    set_service_caller(None)

    assert get_service_caller() is get_service_caller()


@override_settings(MANAGED_TEMPLATE_SERVICE_FACTORY="no.such.module.factory")
def test_a_failure_is_not_cached() -> None:
    """A transient misconfiguration must not poison the process for its whole life."""
    set_service_caller(None)

    with pytest.raises(ServiceConfigurationError):
        get_service_caller()
    with pytest.raises(ServiceConfigurationError):
        get_service_caller()


# --- CORS ----------------------------------------------------------------------------


def test_no_cors_headers_by_default(get: ReadRequest) -> None:
    """Clients are expected to call this API from their own server side."""
    response = get("/api/v1/templates", headers={"Origin": "https://ui.example.com"})

    assert "Access-Control-Allow-Origin" not in response


def test_echoes_only_a_configured_origin(get: ReadRequest, settings: "LazySettings") -> None:
    settings.VINTASEND_API_CORS_ORIGINS = ["https://ui.example.com"]

    allowed = get("/api/v1/templates", headers={"Origin": "https://ui.example.com"})
    other = get("/api/v1/templates", headers={"Origin": "https://evil.example.com"})

    assert allowed["Access-Control-Allow-Origin"] == "https://ui.example.com"
    assert allowed["Vary"] == "Origin"
    assert "Access-Control-Allow-Origin" not in other


def test_never_echoes_a_wildcard(get: ReadRequest, settings: "LazySettings") -> None:
    """Every request carries a bearer token; a wildcard would let any page spend it."""
    settings.VINTASEND_API_CORS_ORIGINS = ["https://ui.example.com"]

    response = get("/api/v1/templates", headers={"Origin": "https://ui.example.com"})

    assert response["Access-Control-Allow-Origin"] != "*"


def test_answers_a_preflight(client: Client, settings: "LazySettings") -> None:
    settings.VINTASEND_API_CORS_ORIGINS = ["https://ui.example.com"]

    response = client.options(
        "/api/v1/templates",
        headers={
            "Origin": "https://ui.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 204
    assert "POST" in response["Access-Control-Allow-Methods"]


def test_cors_does_not_apply_outside_the_api_prefix(
    client: Client, settings: "LazySettings"
) -> None:
    settings.VINTASEND_API_CORS_ORIGINS = ["https://ui.example.com"]

    response = client.get("/health", headers={"Origin": "https://ui.example.com"})

    assert "Access-Control-Allow-Origin" not in response
