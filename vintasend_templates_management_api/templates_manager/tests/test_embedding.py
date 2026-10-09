"""Embedding the app in a host project.

A host adds the app to ``INSTALLED_APPS``, includes its URLconf under a prefix of its choosing,
and sets the few settings it needs. None of the bundled project's settings, middleware or
``handler404`` are required, so every setting the app reads has a default, a host may replace
the shared key with its own authenticator, and the routes work wherever they are mounted.
"""

from typing import TYPE_CHECKING, NoReturn

from django.core.management import call_command
from django.http import HttpRequest
from django.test import Client, RequestFactory
from django.urls import resolve, reverse

import pytest
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .. import auth
from ..api import api, health_api
from ..apps import check_api_configuration
from ..errors import ApiError
from ..service import ServiceConfigurationError, get_service_caller, set_service_caller
from .conftest import ReadRequest, WriteRequest
from .fakes import AUTH_HEADERS, FakeEmailRenderer, InMemoryTemplateManagerBackend


if TYPE_CHECKING:
    from django.conf import LazySettings


HOST_URLCONF = f"{__package__}.host_urls"
# Where `host_urls` mounts the app. Not imported from it: importing a URLconf builds it.
HOST_PREFIX = "/templates-api"
NAMESPACE = "vintasend_templates_management_api"
HOST_TOKEN = "host-issued-token"
SECRET = "patient-note-that-must-never-be-logged"

# Every setting the app reads that a host may leave out entirely. The two required ones are
# covered separately: their absence is a failed system check, not a default.
OPTIONAL_SETTINGS = (
    "VINTASEND_API_AUTHENTICATOR",
    "VINTASEND_API_CORS_ORIGINS",
    "MANAGED_TEMPLATE_BACKEND_NAME",
    "MANAGED_TEMPLATE_ACTOR_RESOLVER",
    "MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER",
)

CREATE_BODY = {
    "key": "welcome-email",
    "name": "Welcome",
    "templateManagedBackend": "from-the-body",
    "bodyTemplate": "<p>Hi {{ name }}</p>",
}


def build_service() -> ManagedTemplateService:
    return ManagedTemplateService(
        template_manager_backend=InMemoryTemplateManagerBackend(),
        template_renderer=FakeEmailRenderer(),
    )


FACTORY_PATH = f"{__name__}.build_service"


def _remove(settings: "LazySettings", *names: str) -> None:
    """Take settings out entirely, as a host that never heard of them would have it."""
    for name in names:
        if hasattr(settings, name):
            delattr(settings, name)


# --- host authenticators ----------------------------------------------------------------

AUTHENTICATED: list[str] = []


@pytest.fixture(autouse=True)
def _clear_authenticated() -> None:
    AUTHENTICATED.clear()


def refuse_everyone(request: HttpRequest) -> NoReturn:
    raise ApiError("FORBIDDEN", "You cannot manage templates.")


def allow_everyone(request: HttpRequest) -> None:
    AUTHENTICATED.append(request.method or "")


async def allow_asynchronously(request: HttpRequest) -> None:
    AUTHENTICATED.append("async")


def check_the_host_token(request: HttpRequest) -> None:
    """What a host writes around a token it verifies itself."""
    token = auth.bearer_token(request)
    if token is None:
        raise ApiError("UNAUTHORIZED", "Sign in to manage templates.")
    if token != HOST_TOKEN:
        raise ApiError("FORBIDDEN", "You cannot manage templates.")


NOT_CALLABLE = "a string, not an authenticator"


# --- settings absent: the defaults --------------------------------------------------------


def test_the_api_works_with_no_optional_setting_present(
    get: ReadRequest, settings: "LazySettings"
) -> None:
    _remove(settings, *OPTIONAL_SETTINGS)

    response = get("/api/v1/templates", headers={"Origin": "https://ui.example.com"})

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response


def test_with_no_backend_name_setting_the_body_names_the_backend(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend, settings: "LazySettings"
) -> None:
    _remove(settings, *OPTIONAL_SETTINGS)

    response = post("/api/v1/templates", CREATE_BODY)

    assert response.status_code == 201
    assert response.json()["data"]["templateManagedBackend"] == "from-the-body"


def test_with_no_resolver_setting_changed_by_comes_from_the_body(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend, settings: "LazySettings"
) -> None:
    _remove(settings, *OPTIONAL_SETTINGS)
    backend.add(key="welcome-email", version=1)

    response = post("/api/v1/templates/welcome-email/activate", {"changedBy": "from-the-body"})

    assert response.status_code == 200
    assert [record.created_by for record in backend.history] == ["from-the-body"]


def test_with_no_handler_setting_an_unexpected_error_is_a_generic_500(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend, settings: "LazySettings"
) -> None:
    _remove(settings, *OPTIONAL_SETTINGS)

    def explode(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError(SECRET)

    backend.get_paginated_filtered_templates = explode  # type: ignore[method-assign]

    response = get("/api/v1/templates")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert SECRET not in response.content.decode()


def test_with_no_key_setting_and_no_authenticator_every_request_is_refused(
    client: Client, settings: "LazySettings"
) -> None:
    """A missing key is refused like an empty one, not answered with a crash."""
    _remove(settings, "VINTASEND_API_KEY", *OPTIONAL_SETTINGS)

    response = client.get("/api/v1/templates", headers=AUTH_HEADERS)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_the_checks_report_required_settings_that_are_absent(settings: "LazySettings") -> None:
    _remove(settings, "VINTASEND_API_KEY", "MANAGED_TEMPLATE_SERVICE_FACTORY", *OPTIONAL_SETTINGS)

    assert sorted(str(error.id) for error in check_api_configuration(None)) == [
        "vintasend_templates_management_api.E001",
        "vintasend_templates_management_api.E002",
    ]


def test_an_absent_factory_setting_is_a_configuration_error(settings: "LazySettings") -> None:
    _remove(settings, "MANAGED_TEMPLATE_SERVICE_FACTORY")
    set_service_caller(None)

    with pytest.raises(ServiceConfigurationError, match="is not set"):
        get_service_caller()


# --- a host authenticator ---------------------------------------------------------------


def test_an_authenticator_that_refuses_answers_403_in_the_envelope(
    client: Client, settings: "LazySettings"
) -> None:
    settings.VINTASEND_API_AUTHENTICATOR = f"{__name__}.refuse_everyone"

    # The shared key is valid, and still not enough: the authenticator replaces the check.
    response = client.get("/api/v1/templates", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "FORBIDDEN", "message": "You cannot manage templates."}
    }


def test_an_authenticator_that_accepts_needs_no_api_key(
    client: Client, settings: "LazySettings"
) -> None:
    _remove(settings, "VINTASEND_API_KEY")
    settings.VINTASEND_API_AUTHENTICATOR = allow_everyone

    response = client.get("/api/v1/templates")

    assert response.status_code == 200
    assert AUTHENTICATED == ["GET"]


def test_the_authenticator_may_be_asynchronous(client: Client, settings: "LazySettings") -> None:
    settings.VINTASEND_API_AUTHENTICATOR = allow_asynchronously

    response = client.get("/api/v1/templates")

    assert response.status_code == 200
    assert AUTHENTICATED == ["async"]


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        pytest.param({}, 401, "UNAUTHORIZED", id="no-token"),
        pytest.param({"Authorization": "Bearer someone-elses"}, 403, "FORBIDDEN", id="wrong"),
        pytest.param({"Authorization": f"bearer {HOST_TOKEN}"}, 200, None, id="accepted"),
    ],
)
def test_an_authenticator_built_on_bearer_token(
    client: Client,
    settings: "LazySettings",
    headers: dict[str, str],
    status: int,
    code: str | None,
) -> None:
    settings.VINTASEND_API_KEY = ""
    settings.VINTASEND_API_AUTHENTICATOR = f"{__name__}.check_the_host_token"

    response = client.get("/api/v1/templates", headers=headers)

    assert response.status_code == status
    if code is not None:
        assert response.json()["error"]["code"] == code


def test_the_authenticator_leaves_health_alone(client: Client, settings: "LazySettings") -> None:
    settings.VINTASEND_API_AUTHENTICATOR = refuse_everyone

    assert client.get("/health").status_code == 200


def test_an_authenticator_makes_the_api_key_optional_in_the_checks(
    settings: "LazySettings",
) -> None:
    _remove(settings, "VINTASEND_API_KEY")
    settings.MANAGED_TEMPLATE_SERVICE_FACTORY = FACTORY_PATH
    settings.VINTASEND_API_AUTHENTICATOR = f"{__name__}.allow_everyone"

    assert check_api_configuration(None) == []
    call_command("check")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("no_such_module.authenticate", id="unimportable"),
        pytest.param(f"{__name__}.NOT_CALLABLE", id="not-callable"),
    ],
)
def test_an_authenticator_that_cannot_be_used_fails_the_checks(
    settings: "LazySettings", value: str
) -> None:
    """Reported on its own: E001 stays quiet, since an authenticator was configured."""
    _remove(settings, "VINTASEND_API_KEY")
    settings.MANAGED_TEMPLATE_SERVICE_FACTORY = FACTORY_PATH
    settings.VINTASEND_API_AUTHENTICATOR = value

    assert [str(error.id) for error in check_api_configuration(None)] == [
        "vintasend_templates_management_api.E005"
    ]


# --- bearer_token -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param("Bearer abc", "abc", id="plain"),
        pytest.param("bearer abc", "abc", id="lowercase-scheme"),
        pytest.param("BEARER abc", "abc", id="uppercase-scheme"),
        pytest.param("Bearer   abc  ", "abc", id="padded"),
        pytest.param("Bearer a b", "a b", id="inner-space-kept"),
        pytest.param(None, None, id="no-header"),
        pytest.param("", None, id="empty-header"),
        pytest.param("Bearer", None, id="scheme-only"),
        pytest.param("Bearer    ", None, id="empty-token"),
        pytest.param("Basic abc", None, id="other-scheme"),
        pytest.param("abc", None, id="no-scheme"),
        pytest.param("Bearerabc", None, id="no-separator"),
    ],
)
def test_bearer_token_reads_a_header(header: str | None, expected: str | None) -> None:
    assert auth.bearer_token(header) == expected


def test_bearer_token_reads_a_request() -> None:
    factory = RequestFactory()

    assert auth.bearer_token(factory.get("/", headers={"Authorization": "Bearer abc"})) == "abc"
    assert auth.bearer_token(factory.get("/")) is None


# --- mounted under a prefix -------------------------------------------------------------


@pytest.fixture
def mounted(settings: "LazySettings") -> None:
    settings.ROOT_URLCONF = HOST_URLCONF


@pytest.mark.usefixtures("mounted")
def test_health_is_served_under_the_prefix(client: Client) -> None:
    response = client.get(f"{HOST_PREFIX}/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "apiVersion": "v1"}


@pytest.mark.usefixtures("mounted")
def test_the_api_is_served_under_the_prefix(get: ReadRequest, client: Client) -> None:
    assert get(f"{HOST_PREFIX}/api/v1/templates").status_code == 200

    refused = client.get(f"{HOST_PREFIX}/api/v1/templates")
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.usefixtures("mounted")
def test_writes_work_under_the_prefix(post: WriteRequest, get: ReadRequest) -> None:
    created = post(f"{HOST_PREFIX}/api/v1/templates", CREATE_BODY)
    fetched = get(f"{HOST_PREFIX}/api/v1/templates/welcome-email")

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json()["data"]["key"] == "welcome-email"


@pytest.mark.usefixtures("mounted")
def test_the_host_keeps_its_own_routes(client: Client) -> None:
    assert client.get("/").json() == {"host": "home"}
    assert client.get("/api/v1/templates", headers=AUTH_HEADERS).status_code == 404


@pytest.mark.usefixtures("mounted")
def test_named_routes_reverse_under_the_prefix() -> None:
    assert reverse(f"{NAMESPACE}:openapi-json") == f"{HOST_PREFIX}/api/v1/openapi.json"


@pytest.mark.usefixtures("mounted")
def test_cors_applies_to_the_api_under_the_prefix(
    get: ReadRequest, client: Client, settings: "LazySettings"
) -> None:
    settings.VINTASEND_API_CORS_ORIGINS = ["https://ui.example.com"]
    origin = {"Origin": "https://ui.example.com"}

    api_response = get(f"{HOST_PREFIX}/api/v1/templates", headers=origin)
    health_response = client.get(f"{HOST_PREFIX}/health", headers=origin)
    preflight = client.options(
        f"{HOST_PREFIX}/api/v1/templates",
        headers={**origin, "Access-Control-Request-Method": "POST"},
    )

    assert api_response["Access-Control-Allow-Origin"] == "https://ui.example.com"
    assert "Access-Control-Allow-Origin" not in health_response
    assert preflight.status_code == 204


def test_cors_keeps_answering_an_unmatched_path_under_the_standalone_prefix(
    get: ReadRequest, settings: "LazySettings"
) -> None:
    """Resolving routes for a mounted app must not change what the standalone project sends."""
    settings.VINTASEND_API_CORS_ORIGINS = ["https://ui.example.com"]
    origin = {"Origin": "https://ui.example.com"}

    under_the_api = get("/api/v1/nope", headers=origin)
    elsewhere = get("/nope", headers=origin)

    assert under_the_api["Access-Control-Allow-Origin"] == "https://ui.example.com"
    assert "Access-Control-Allow-Origin" not in elsewhere


# --- the URL namespace ------------------------------------------------------------------


def test_the_api_has_a_namespace_of_its_own() -> None:
    """Ninja's default, ``api-1.0.0``, would collide with vintasend-api's in one project."""
    assert api.urls_namespace == NAMESPACE
    assert health_api.urls_namespace not in {NAMESPACE, "api-1.0.0"}


def test_routes_resolve_into_the_namespace() -> None:
    assert resolve("/api/v1/templates").namespace == NAMESPACE
