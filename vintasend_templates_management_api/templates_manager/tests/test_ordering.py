"""``GET /templates?orderByField=...``, and why an order the backend cannot apply is a 400.

This endpoint negotiates two things against the same capability report and resolves them in
opposite directions. That is the design, not an inconsistency:

* an unsupported **filter** is dropped and the request succeeds -- the caller sees the extra
  rows in the response and can tell;
* an unsupported **order** is a ``400`` -- an ignored order returns exactly the rows that were
  asked for in an arbitrary sequence, and nothing in the response says so.

A client rendering that second case under a highlighted "sorted by name" column header is
showing a sort that never happened, which is why it is refused instead.
"""

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.filters import MANAGED_TEMPLATE_ORDER_BY_FIELDS
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from ..errors import ApiError
from ..service import ServiceCaller
from .conftest import ReadRequest
from .fakes import FIXED_NOW, InMemoryTemplateManagerBackend


if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse


def at(days: int) -> datetime.datetime:
    return FIXED_NOW + datetime.timedelta(days=days)


def seed(backend: InMemoryTemplateManagerBackend) -> None:
    """Four keys whose fields disagree about the order they imply.

    Insertion order is the fallback, and every field below disagrees with it, so a sort that
    quietly did nothing -- or sorted on the wrong column -- would fail rather than coincide
    with the right answer. The timestamps are distinct for the same reason: rows sharing one
    would fall through to the ``(key, version)`` tiebreak and pass whatever was compared.
    """
    backend.add(
        key="delta",
        name="Alpha name",
        version=2,
        status=ManagedTemplateStatus.DRAFT,
        created=at(3),
        updated=at(0),
    )
    backend.add(
        key="alpha",
        name="Delta name",
        version=10,
        status=ManagedTemplateStatus.ARCHIVED,
        created=at(2),
        updated=at(1),
    )
    backend.add(
        key="charlie",
        name="Bravo name",
        version=3,
        status=ManagedTemplateStatus.ACTIVE,
        created=at(1),
        updated=at(2),
    )
    backend.add(
        key="bravo",
        name="Charlie name",
        version=1,
        status=ManagedTemplateStatus.INACTIVE,
        created=at(0),
        updated=at(3),
    )


def paginated_call(backend: InMemoryTemplateManagerBackend) -> tuple[object, ...]:
    """The arguments of the last paginated read.

    Not simply the last recorded call: the fake answers a paginated read by delegating to
    ``get_filtered_templates``, which records itself afterwards.
    """
    return next(
        args for name, args in reversed(backend.calls) if name == "get_paginated_filtered_templates"
    )


def listed(get: ReadRequest, query: str) -> list[str]:
    response = get(f"/api/v1/templates?mostRecentActiveVersion=false&{query}")

    assert response.status_code == 200, response.json()
    return [row["key"] for row in response.json()["data"]]


# --- ordering that works --------------------------------------------------------------


def test_an_omitted_order_leaves_the_backends_own_order(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """No default: omitted asks for whatever order the store already returns."""
    seed(backend)

    assert listed(get, "") == ["delta", "alpha", "charlie", "bravo"]


def test_each_field_orders(get: ReadRequest, backend: InMemoryTemplateManagerBackend) -> None:
    seed(backend)

    assert listed(get, "orderByField=key") == ["alpha", "bravo", "charlie", "delta"]
    assert listed(get, "orderByField=name") == ["delta", "charlie", "bravo", "alpha"]
    assert listed(get, "orderByField=version") == ["bravo", "delta", "charlie", "alpha"]
    assert listed(get, "orderByField=status") == ["charlie", "alpha", "delta", "bravo"]
    assert listed(get, "orderByField=createdAt") == ["bravo", "charlie", "alpha", "delta"]
    assert listed(get, "orderByField=updatedAt") == ["delta", "alpha", "charlie", "bravo"]


def test_the_direction_defaults_to_ascending(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """The SQL default, and the one a reader assumes when none is named."""
    seed(backend)

    assert listed(get, "orderByField=key") == listed(get, "orderByField=key&orderByDirection=asc")


def test_descending_reverses_it(get: ReadRequest, backend: InMemoryTemplateManagerBackend) -> None:
    seed(backend)

    assert listed(get, "orderByField=key&orderByDirection=desc") == [
        "delta",
        "charlie",
        "bravo",
        "alpha",
    ]


def test_version_orders_numerically(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """v10 comes after v2. A store padding versions into strings gets this wrong silently."""
    for version in (10, 2, 3, 1, 11):
        backend.add(key="welcome", version=version)

    rows = get(
        "/api/v1/templates?mostRecentActiveVersion=false&orderByField=version&orderByDirection=asc"
    ).json()["data"]

    assert [row["version"] for row in rows] == [1, 2, 3, 10, 11]


def test_the_order_is_applied_before_the_page_is_chosen(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """The failure this catches looks right on page 1 and is wrong on every page after it."""
    seed(backend)

    first = listed(get, "orderByField=key&pageSize=2&page=1")
    second = listed(get, "orderByField=key&pageSize=2&page=2")

    assert first == ["alpha", "bravo"]
    assert second == ["charlie", "delta"]


def test_ordering_combines_with_a_filter(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    seed(backend)

    assert listed(get, "orderByField=key&status=draft&status=active") == ["charlie", "delta"]


def test_the_order_reaches_the_backend_rather_than_being_applied_here(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """Ordering has to reach the store, or paging and sorting disagree about the same set."""
    seed(backend)

    get("/api/v1/templates?orderByField=name&orderByDirection=desc")

    assert paginated_call(backend)[-1] == {"field": "name", "direction": "desc"}


def test_no_order_is_passed_when_none_was_asked_for(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    seed(backend)

    get("/api/v1/templates")

    assert paginated_call(backend)[-1] is None


# --- ordering that is refused ---------------------------------------------------------


def failure(response: "TestResponse") -> dict[str, Any]:
    """The error envelope of a request that should have been refused."""
    assert response.status_code == 400, response.json()

    error: dict[str, Any] = response.json()["error"]
    return error


def test_a_field_the_backend_cannot_order_by_is_a_400(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """Refused rather than dropped: a dropped order leaves no trace in the rows."""
    backend = InMemoryTemplateManagerBackend(capabilities={"orderBy.key": True})
    install_service(template_backend=backend)
    backend.add(key="welcome")

    error = failure(get("/api/v1/templates?orderByField=name"))

    assert error["code"] == "BAD_REQUEST"
    assert "cannot order by 'name'" in error["message"]


def test_the_refusal_names_the_capability_key_to_look_up(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """So a client can go straight from the error to the report entry that explains it."""
    install_service(template_backend=InMemoryTemplateManagerBackend(capabilities={}))

    error = failure(get("/api/v1/templates?orderByField=createdAt"))

    assert error["details"]["capability"] == "orderBy.createdAt"
    assert "/capabilities" in error["message"]


def test_a_backend_that_declares_nothing_refuses_every_field(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    install_service(template_backend=InMemoryTemplateManagerBackend(capabilities={}))

    for field in ("key", "name", "version", "status", "createdAt", "updatedAt"):
        assert get(f"/api/v1/templates?orderByField={field}").status_code == 400


def test_the_unordered_listing_still_works_against_a_backend_that_cannot_sort(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """Why neither parameter has a default: a default field would 400 the common listing."""
    backend = InMemoryTemplateManagerBackend(capabilities={})
    install_service(template_backend=backend)
    backend.add(key="welcome")

    assert get("/api/v1/templates").status_code == 200


def test_a_direction_without_a_field_is_a_400(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """Ignoring it looks exactly like a backend that cannot sort, which hides the client bug."""
    seed(backend)

    error = failure(get("/api/v1/templates?orderByDirection=desc"))

    assert "nothing to order by" in error["message"]


def test_an_unknown_field_is_rejected_by_validation(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """Not in the enum, so it is refused before any capability is consulted."""
    error = failure(get("/api/v1/templates?orderByField=tags"))

    assert [issue["path"] for issue in error["details"]["issues"]] == ["orderByField"]


def test_an_unknown_direction_is_rejected_by_validation(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    error = failure(get("/api/v1/templates?orderByField=key&orderByDirection=sideways"))

    assert [issue["path"] for issue in error["details"]["issues"]] == ["orderByDirection"]


def test_every_field_the_spec_accepts_can_be_ordered_by(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """The default fake declares all six, so each must round-trip rather than 400."""
    seed(backend)

    for field in MANAGED_TEMPLATE_ORDER_BY_FIELDS:
        wire = {"created_at": "createdAt", "updated_at": "updatedAt"}.get(field, field)
        assert get(f"/api/v1/templates?orderByField={wire}").status_code == 200


def test_the_library_s_own_refusal_is_reported_as_a_400(
    install_service: Callable[..., ManagedTemplateService],
) -> None:
    """The backstop for the route and the service disagreeing about what is orderable.

    ``build_order_by`` reads the same report and refuses first, so nothing reaches this
    through the route today. Called directly -- as a future refactor could -- the library's
    ``ManagedTemplateUnsupportedOrderingError`` still has to arrive as the client error it
    is, rather than as a 500 on a request that is not the server's fault.
    """
    backend = InMemoryTemplateManagerBackend(capabilities={})
    caller = ServiceCaller(install_service(template_backend=backend))

    with pytest.raises(ApiError) as raised:
        caller.get_paginated_filtered_templates(
            {}, 1, 10, {"field": "created_at", "direction": "asc"}
        )

    assert raised.value.code == "BAD_REQUEST"
    assert "orderBy.createdAt" in str(raised.value)
