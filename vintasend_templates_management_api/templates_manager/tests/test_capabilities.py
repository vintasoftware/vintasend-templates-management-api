"""What the capabilities endpoint publishes, and what the list route does with it."""

from collections.abc import Callable

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from ..capabilities import (
    DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES,
    MANAGED_TEMPLATE_ORDER_BY_FIELDS,
    order_by_capability_key,
)
from .conftest import ReadRequest
from .fakes import InMemoryTemplateManagerBackend


def test_a_backend_with_no_report_is_fully_capable(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """Saying nothing is fine: every filter reads as supported, every order as unsupported."""
    install_service(template_backend=InMemoryTemplateManagerBackend(capabilities={}))

    response = get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.json()["data"] == DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES


def test_a_backends_report_is_merged_over_the_default(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """A backend declares only what it cannot do; everything else stays supported."""
    install_service(
        template_backend=InMemoryTemplateManagerBackend(
            capabilities={"stringLookups.includes": False}
        )
    )

    data = get("/api/v1/capabilities").json()["data"]

    assert data["stringLookups.includes"] is False
    assert data["stringLookups.exact"] is True
    assert data["fields.key"] is True


def test_non_boolean_values_are_coerced(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """The contract types these as booleans, so a truthy non-boolean cannot leak through."""
    install_service(template_backend=InMemoryTemplateManagerBackend(capabilities={"fields.key": 0}))

    assert get("/api/v1/capabilities").json()["data"]["fields.key"] is False


def test_an_ordering_capability_is_published_for_every_orderable_field(
    get: ReadRequest,
) -> None:
    """A field the API accepts but the report never mentions is one a client cannot discover.

    It would also fall to the False default and 400 on every request, which is a limitation
    with nowhere to read it from.
    """
    data = get("/api/v1/capabilities").json()["data"]

    for field in MANAGED_TEMPLATE_ORDER_BY_FIELDS:
        assert order_by_capability_key(field) in data


def test_a_backend_that_says_nothing_reports_nothing_orderable(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """The one place a missing key does *not* mean supported, and the reason it does not.

    Ordering is newer vocabulary than the filters. A True default would have every backend
    written before it existed claim an order it silently ignores.
    """
    install_service(template_backend=InMemoryTemplateManagerBackend(capabilities={}))

    data = get("/api/v1/capabilities").json()["data"]

    assert [key for key in data if key.startswith("orderBy.")]
    assert not [key for key, value in data.items() if key.startswith("orderBy.") and value]


# --- filter negotiation --------------------------------------------------------------


def test_falls_back_to_an_exact_match_without_includes(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    backend = InMemoryTemplateManagerBackend(capabilities={"stringLookups.includes": False})
    install_service(template_backend=backend)
    backend.add(key="welcome-email")

    assert get("/api/v1/templates?key=welcome").json()["data"] == []
    assert len(get("/api/v1/templates?key=WELCOME-EMAIL").json()["data"]) == 1


def test_falls_back_to_a_case_sensitive_match_without_case_folding(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    backend = InMemoryTemplateManagerBackend(
        capabilities={"stringLookups.includes": False, "stringLookups.caseInsensitive": False}
    )
    install_service(template_backend=backend)
    backend.add(key="welcome-email")

    assert get("/api/v1/templates?key=WELCOME-EMAIL").json()["data"] == []
    assert len(get("/api/v1/templates?key=welcome-email").json()["data"]) == 1


def test_case_sensitivity_is_read_from_its_own_key(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """`caseSensitive` and `caseInsensitive` are independent, not a flag and its negation.

    A backend on a case-insensitive collation declines `caseSensitive` and can still match
    case-insensitively; deriving one from the other would decline the lookup it supports.
    """
    backend = InMemoryTemplateManagerBackend(capabilities={"stringLookups.caseSensitive": False})
    install_service(template_backend=backend)
    backend.add(key="welcome-email")

    assert len(get("/api/v1/templates?key=WELCOME").json()["data"]) == 1


def test_an_unsupported_field_is_dropped_rather_than_failing_the_request(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    backend = InMemoryTemplateManagerBackend(capabilities={"fields.status": False})
    install_service(template_backend=backend)
    backend.add(key="welcome-email")

    response = get("/api/v1/templates?status=archived")

    assert response.status_code == 200
    # The filter was dropped, so the draft template still comes back.
    assert len(response.json()["data"]) == 1


def test_a_backend_that_cannot_collapse_versions_gets_every_version(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """Declining `fields.mostRecentActiveVersion` drops the filter, like any other.

    The listing then shows a row per version -- the raw seam read -- rather than failing a
    request a client did not even know was making a demand of the backend.
    """
    backend = InMemoryTemplateManagerBackend(capabilities={"fields.mostRecentActiveVersion": False})
    install_service(template_backend=backend)
    backend.add(key="welcome-email", version=1, status=ManagedTemplateStatus.ARCHIVED)
    backend.add(key="welcome-email", version=2, status=ManagedTemplateStatus.ACTIVE)

    rows = get("/api/v1/templates").json()["data"]

    assert [row["version"] for row in rows] == [1, 2]


def test_capabilities_are_read_once_per_process(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """A backend's capabilities are static, so re-asking every request would buy nothing.

    It matters more than it used to: the report is now read before every filtered read, to
    prune what the backend cannot answer, rather than only when a client asks for it.
    """
    calls: list[int] = []

    class CountingBackend(InMemoryTemplateManagerBackend):
        def get_filter_capabilities(self) -> dict[str, bool]:
            calls.append(1)
            return {}

    backend = CountingBackend()
    install_service(template_backend=backend)

    get("/api/v1/capabilities")
    get("/api/v1/capabilities")
    get("/api/v1/templates")

    assert len(calls) == 1
