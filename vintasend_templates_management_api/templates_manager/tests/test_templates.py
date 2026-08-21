"""Listing, reading, creating, versioning and deleting templates."""

import datetime
from typing import Any, Callable

import pytest
from vintasend_managed_templates.constants import ManagedTemplateStatus

from .fakes import FIXED_NOW, InMemoryTemplateManagerBackend


def test_lists_templates_with_the_pagination_envelope(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email")
    backend.add(key="password-reset")

    response = get("/api/v1/templates")

    assert response.status_code == 200
    body = response.json()
    assert [row["key"] for row in body["data"]] == ["welcome-email", "password-reset"]
    assert body["page"] == 1
    assert body["pageSize"] == 20
    assert body["hasMore"] is False


def test_reports_has_more_when_a_page_comes_back_full(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """The seam has no count method, so a full page is the only signal another may exist."""
    backend.add(key="a")
    backend.add(key="b")

    response = get("/api/v1/templates?pageSize=2")

    assert response.json()["hasMore"] is True


def test_pages_are_one_indexed(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """Page 1 is the first page on the wire and in the service, with no conversion between."""
    backend.add(key="a")
    backend.add(key="b")

    first = get("/api/v1/templates?page=1&pageSize=1").json()
    second = get("/api/v1/templates?page=2&pageSize=1").json()

    assert [row["key"] for row in first["data"]] == ["a"]
    assert [row["key"] for row in second["data"]] == ["b"]


def test_serializes_every_contract_field(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(
        key="welcome-email",
        name="Welcome",
        description="Sent on signup.",
        template_managed_backend="django",
        body_template="<p>Hi</p>",
        subject_template="Welcome",
        preheader_template="Glad you are here",
        version=3,
        status=ManagedTemplateStatus.ACTIVE,
        tenant="acme",
    )

    row = get("/api/v1/templates").json()["data"][0]

    assert row == {
        "id": row["id"],
        "key": "welcome-email",
        "version": 3,
        "name": "Welcome",
        "description": "Sent on signup.",
        "templateManagedBackend": "django",
        "bodyTemplate": "<p>Hi</p>",
        "subjectTemplate": "Welcome",
        "preheaderTemplate": "Glad you are here",
        "status": "active",
        "tenant": "acme",
        "createdAt": "2024-01-15T09:00:00.000Z",
        "updatedAt": "2024-01-15T09:00:00.000Z",
        "tags": [],
        "allowedTransitions": ["inactive", "archived"],
        "isAbstract": False,
    }


def test_absent_optional_fields_are_null_not_missing(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(subject_template=None, preheader_template=None, tenant=None)

    row = get("/api/v1/templates").json()["data"][0]

    assert row["subjectTemplate"] is None
    assert row["preheaderTemplate"] is None
    assert row["tenant"] is None


# --- filtering -----------------------------------------------------------------------


def test_filters_by_key_case_insensitively(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email")
    backend.add(key="password-reset")

    response = get("/api/v1/templates?key=WELCOME")

    assert [row["key"] for row in response.json()["data"]] == ["welcome-email"]


def test_filters_by_a_single_status(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="draft-one", status=ManagedTemplateStatus.DRAFT)
    backend.add(key="active-one", status=ManagedTemplateStatus.ACTIVE)

    response = get("/api/v1/templates?status=active")

    assert [row["key"] for row in response.json()["data"]] == ["active-one"]


def test_filters_by_several_statuses_at_once(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="draft-one", status=ManagedTemplateStatus.DRAFT)
    backend.add(key="active-one", status=ManagedTemplateStatus.ACTIVE)
    backend.add(key="archived-one", status=ManagedTemplateStatus.ARCHIVED)

    response = get("/api/v1/templates?status=draft&status=active")

    assert [row["key"] for row in response.json()["data"]] == ["draft-one", "active-one"]


def test_filters_by_version(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)
    backend.add(key="welcome-email", version=2)

    response = get("/api/v1/templates?version=2")

    assert [row["version"] for row in response.json()["data"]] == [2]


def test_filters_by_created_range(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="old", created=FIXED_NOW - datetime.timedelta(days=10))
    backend.add(key="new", created=FIXED_NOW)

    response = get("/api/v1/templates?createdAtFrom=2024-01-10T00:00:00Z")

    assert [row["key"] for row in response.json()["data"]] == ["new"]


def test_combines_filters_with_and(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", status=ManagedTemplateStatus.DRAFT)
    backend.add(key="welcome-email", version=2, status=ManagedTemplateStatus.ACTIVE)
    backend.add(key="password-reset", status=ManagedTemplateStatus.ACTIVE)

    response = get("/api/v1/templates?key=welcome&status=active")

    assert [row["version"] for row in response.json()["data"]] == [2]


def test_lists_only_the_current_version_of_each_key_by_default(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1, status=ManagedTemplateStatus.INACTIVE)
    backend.add(key="welcome-email", version=2, status=ManagedTemplateStatus.ACTIVE)
    backend.add(key="password-reset", version=1, status=ManagedTemplateStatus.DRAFT)

    rows = get("/api/v1/templates").json()["data"]

    assert [(row["key"], row["version"]) for row in rows] == [
        ("welcome-email", 2),
        ("password-reset", 1),
    ]


def test_a_key_with_no_active_or_draft_version_is_not_listed_by_default(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="retired", version=1, status=ManagedTemplateStatus.ARCHIVED)
    backend.add(key="live", version=1, status=ManagedTemplateStatus.ACTIVE)

    rows = get("/api/v1/templates").json()["data"]

    assert [row["key"] for row in rows] == ["live"]


def test_most_recent_active_version_false_lists_every_version(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """`false` lifts the default rather than asking for the rows it hides."""
    backend.add(key="welcome-email", version=1, status=ManagedTemplateStatus.INACTIVE)
    backend.add(key="welcome-email", version=2, status=ManagedTemplateStatus.ACTIVE)

    rows = get("/api/v1/templates?mostRecentActiveVersion=false").json()["data"]

    assert [row["version"] for row in rows] == [1, 2]


def test_the_default_combines_with_the_other_filters(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1, status=ManagedTemplateStatus.ACTIVE)
    backend.add(key="welcome-email", version=2, status=ManagedTemplateStatus.ARCHIVED)

    # v2 is archived, so v1 is the current version -- but the status filter asks for the
    # archived one, and the two together match nothing.
    assert get("/api/v1/templates?status=archived").json()["data"] == []
    assert len(get("/api/v1/templates?status=active").json()["data"]) == 1


def test_rejects_a_non_boolean_most_recent_active_version(get: Callable[..., Any]) -> None:
    assert get("/api/v1/templates?mostRecentActiveVersion=maybe").status_code == 400


def test_rejects_a_blank_filter_value(get: Callable[..., Any]) -> None:
    """A parameter present but blank is a client bug, not a filter matching everything."""
    response = get("/api/v1/templates?key=%20%20")

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "BAD_REQUEST"
    assert body["details"]["issues"][0]["path"] == "key"


def test_rejects_an_unknown_status(get: Callable[..., Any]) -> None:
    response = get("/api/v1/templates?status=published")

    assert response.status_code == 400
    assert response.json()["error"]["details"]["issues"][0]["path"] == "status.0"


@pytest.mark.parametrize("query", ["page=0", "pageSize=0", "pageSize=101"])
def test_rejects_out_of_range_pagination(get: Callable[..., Any], query: str) -> None:
    response = get(f"/api/v1/templates?{query}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


# --- reading one version -------------------------------------------------------------


def test_reads_the_latest_version_by_default(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1, name="v1")
    backend.add(key="welcome-email", version=2, name="v2")

    response = get("/api/v1/templates/welcome-email")

    assert response.status_code == 200
    assert response.json()["data"]["name"] == "v2"


def test_reads_a_pinned_version(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1, name="v1")
    backend.add(key="welcome-email", version=2, name="v2")

    assert get("/api/v1/templates/welcome-email?version=1").json()["data"]["name"] == "v1"
    assert get("/api/v1/templates/welcome-email/versions/1").json()["data"]["name"] == "v1"


def test_reading_an_unknown_key_is_a_404(get: Callable[..., Any]) -> None:
    response = get("/api/v1/templates/nope")

    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "NOT_FOUND"
    assert "nope" in body["message"]


def test_reading_an_unknown_version_is_a_404(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)

    response = get("/api/v1/templates/welcome-email/versions/9")

    assert response.status_code == 404
    assert "version 9" in response.json()["error"]["message"]


# --- version listing -----------------------------------------------------------------


def test_lists_versions_newest_first(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)
    backend.add(key="welcome-email", version=3)
    backend.add(key="welcome-email", version=2)
    backend.add(key="password-reset", version=1)

    response = get("/api/v1/templates/welcome-email/versions")

    assert [row["version"] for row in response.json()["data"]] == [3, 2, 1]


def test_listing_versions_of_an_unknown_key_is_a_404(get: Callable[..., Any]) -> None:
    """The service filters rather than raising here, so an empty result is the miss."""
    response = get("/api/v1/templates/nope/versions")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --- creating ------------------------------------------------------------------------


def test_creates_a_first_version(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    response = post(
        "/api/v1/templates",
        {
            "key": "welcome-email",
            "name": "Welcome",
            "description": "Sent on signup.",
            "templateManagedBackend": "django",
            "bodyTemplate": "<p>Hi {{ name }}</p>",
            "subjectTemplate": "Welcome",
            "tenant": "acme",
        },
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["key"] == "welcome-email"
    assert data["version"] == 1
    assert data["status"] == "draft"
    assert data["preheaderTemplate"] is None
    assert len(backend.templates) == 1


@pytest.mark.parametrize("missing", ["key", "name", "templateManagedBackend", "bodyTemplate"])
def test_rejects_a_create_missing_a_required_field(post: Callable[..., Any], missing: str) -> None:
    body = {
        "key": "welcome-email",
        "name": "Welcome",
        "templateManagedBackend": "django",
        "bodyTemplate": "<p>Hi</p>",
    }
    body.pop(missing)

    response = post("/api/v1/templates", body)

    assert response.status_code == 400
    assert response.json()["error"]["details"]["issues"][0]["path"] == missing


def test_rejects_an_empty_body_template(post: Callable[..., Any]) -> None:
    response = post(
        "/api/v1/templates",
        {
            "key": "welcome-email",
            "name": "Welcome",
            "templateManagedBackend": "django",
            "bodyTemplate": "",
        },
    )

    assert response.status_code == 400


# --- versioning ----------------------------------------------------------------------


def test_creates_a_new_version_from_the_latest(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1, name="v1", body_template="<p>old</p>")

    response = post("/api/v1/templates/welcome-email/versions", {"bodyTemplate": "<p>new</p>"})

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["version"] == 2
    assert data["bodyTemplate"] == "<p>new</p>"
    # Unset fields are carried forward rather than blanked.
    assert data["name"] == "v1"


def test_a_new_version_never_touches_the_one_it_came_from(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """Templates are versioned rather than edited, which is the point of the POST."""
    original = backend.add(key="welcome-email", version=1, body_template="<p>old</p>")

    post("/api/v1/templates/welcome-email/versions", {"bodyTemplate": "<p>new</p>"})

    assert original.body_template == "<p>old</p>"


def test_versioning_an_unknown_key_is_a_404(post: Callable[..., Any]) -> None:
    response = post("/api/v1/templates/nope/versions", {"name": "x"})

    assert response.status_code == 404


# --- deleting ------------------------------------------------------------------------


def test_deletes_the_latest_version_by_default(
    delete: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)
    backend.add(key="welcome-email", version=2)

    response = delete("/api/v1/templates/welcome-email")

    assert response.status_code == 204
    assert [template.version for template in backend.templates] == [1]


def test_deletes_a_pinned_version(
    delete: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)
    backend.add(key="welcome-email", version=2)

    response = delete("/api/v1/templates/welcome-email/versions/1")

    assert response.status_code == 204
    assert [template.version for template in backend.templates] == [2]


def test_deleting_an_unknown_version_is_a_404(
    delete: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)

    assert delete("/api/v1/templates/welcome-email/versions/9").status_code == 404
    assert delete("/api/v1/templates/nope").status_code == 404
