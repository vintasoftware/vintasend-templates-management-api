"""The status lifecycle: transitions, their audit trail, and what a client is told."""

from typing import Any, Callable

import pytest
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .fakes import InMemoryTemplateManagerBackend


ACTIONS = {
    "activate": ManagedTemplateStatus.ACTIVE,
    "deactivate": ManagedTemplateStatus.INACTIVE,
    "archive": ManagedTemplateStatus.ARCHIVED,
}


def test_activates_the_latest_version(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)
    backend.add(key="welcome-email", version=2)

    response = post("/api/v1/templates/welcome-email/activate")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["version"] == 2
    assert data["status"] == "active"


def test_activates_a_pinned_version(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    first = backend.add(key="welcome-email", version=1)
    second = backend.add(key="welcome-email", version=2)

    post("/api/v1/templates/welcome-email/activate", {"version": 1})

    assert first.status is ManagedTemplateStatus.ACTIVE
    assert second.status is ManagedTemplateStatus.DRAFT


def test_activating_leaves_other_active_versions_alone(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """A key may hold several active versions at once; picking between them is the host's."""
    first = backend.add(key="welcome-email", version=1, status=ManagedTemplateStatus.ACTIVE)
    backend.add(key="welcome-email", version=2)

    post("/api/v1/templates/welcome-email/activate", {"version": 2})

    assert first.status is ManagedTemplateStatus.ACTIVE


def test_deactivating_allows_activating_again(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", status=ManagedTemplateStatus.ACTIVE)

    assert post("/api/v1/templates/welcome-email/deactivate").status_code == 200
    assert post("/api/v1/templates/welcome-email/activate").status_code == 200


def test_sets_an_explicitly_named_status(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email")

    response = post("/api/v1/templates/welcome-email/status", {"status": "active"})

    assert response.json()["data"]["status"] == "active"


def test_rejects_an_unknown_status(post: Callable[..., Any]) -> None:
    response = post("/api/v1/templates/welcome-email/status", {"status": "published"})

    assert response.status_code == 400
    assert response.json()["error"]["details"]["issues"][0]["path"] == "status"


# --- refused transitions -------------------------------------------------------------


def test_a_disallowed_move_is_a_409_naming_the_code(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """Archived is terminal under the default lifecycle."""
    backend.add(key="welcome-email", status=ManagedTemplateStatus.ARCHIVED)

    response = post("/api/v1/templates/welcome-email/activate")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"


def test_a_draft_cannot_be_deactivated(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", status=ManagedTemplateStatus.DRAFT)

    response = post("/api/v1/templates/welcome-email/deactivate")

    assert response.status_code == 409


def test_setting_the_status_a_version_already_holds_is_a_no_op(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """Repeating a call must not fill the audit trail with entries recording nothing."""
    backend.add(key="welcome-email", status=ManagedTemplateStatus.ACTIVE)

    response = post("/api/v1/templates/welcome-email/activate")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "active"
    assert backend.history == []


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_a_status_change_on_an_unknown_key_is_a_404(post: Callable[..., Any], action: str) -> None:
    assert post(f"/api/v1/templates/nope/{action}").status_code == 404


def test_a_status_change_on_an_unknown_version_is_a_404(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)

    assert post("/api/v1/templates/welcome-email/activate", {"version": 9}).status_code == 404


def test_a_service_with_validation_off_allows_any_move(
    post: Callable[..., Any],
    backend: InMemoryTemplateManagerBackend,
    install_service: Callable[..., ManagedTemplateService],
) -> None:
    """The lifecycle is the configured service's, not this API's."""
    install_service(validate_status_transitions=False)
    backend.add(key="welcome-email", status=ManagedTemplateStatus.ARCHIVED)

    response = post("/api/v1/templates/welcome-email/activate")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "active"


# --- allowedTransitions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ManagedTemplateStatus.DRAFT, ["active", "archived"]),
        (ManagedTemplateStatus.ACTIVE, ["inactive", "archived"]),
        (ManagedTemplateStatus.INACTIVE, ["active", "archived"]),
        (ManagedTemplateStatus.ARCHIVED, []),
    ],
)
def test_reports_which_moves_will_work(
    get: Callable[..., Any],
    backend: InMemoryTemplateManagerBackend,
    status: ManagedTemplateStatus,
    expected: list[str],
) -> None:
    backend.add(key="welcome-email", status=status)

    data = get("/api/v1/templates/welcome-email").json()["data"]

    assert data["allowedTransitions"] == expected


def test_the_current_status_is_never_offered_as_a_transition(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """Setting a version to the status it holds is a documented no-op, not an action."""
    backend.add(key="welcome-email", status=ManagedTemplateStatus.ACTIVE)

    data = get("/api/v1/templates/welcome-email").json()["data"]

    assert "active" not in data["allowedTransitions"]


def test_reports_every_move_when_validation_is_off(
    get: Callable[..., Any],
    backend: InMemoryTemplateManagerBackend,
    install_service: Callable[..., ManagedTemplateService],
) -> None:
    install_service(validate_status_transitions=False)
    backend.add(key="welcome-email", status=ManagedTemplateStatus.ARCHIVED)

    data = get("/api/v1/templates/welcome-email").json()["data"]

    assert data["allowedTransitions"] == ["draft", "active", "inactive"]


# --- audit trail ---------------------------------------------------------------------


def test_records_a_status_change_in_the_audit_trail(
    post: Callable[..., Any], get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email")

    post("/api/v1/templates/welcome-email/activate", {"changedBy": "ana"})

    history = get("/api/v1/templates/welcome-email/status-history").json()["data"]
    assert history == [
        {
            "templateKey": "welcome-email",
            "version": 1,
            "status": "active",
            "changedBy": "ana",
            "tenant": None,
            "createdAt": "2024-01-15T09:00:00.000Z",
        }
    ]


def test_attribution_is_optional(
    post: Callable[..., Any], get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """The service requires no attribution, and this API adds no policy of its own."""
    backend.add(key="welcome-email")

    post("/api/v1/templates/welcome-email/activate")

    assert (
        get("/api/v1/templates/welcome-email/status-history").json()["data"][0]["changedBy"] is None
    )


def test_history_is_most_recent_first(
    post: Callable[..., Any], get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email")

    post("/api/v1/templates/welcome-email/activate")
    post("/api/v1/templates/welcome-email/deactivate")
    post("/api/v1/templates/welcome-email/archive")

    history = get("/api/v1/templates/welcome-email/status-history").json()["data"]

    assert [record["status"] for record in history] == ["archived", "inactive", "active"]


def test_history_can_be_narrowed_to_one_version(
    post: Callable[..., Any], get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)
    backend.add(key="welcome-email", version=2)
    post("/api/v1/templates/welcome-email/activate", {"version": 1})
    post("/api/v1/templates/welcome-email/activate", {"version": 2})

    history = get("/api/v1/templates/welcome-email/status-history?version=2").json()["data"]

    assert [record["version"] for record in history] == [2]


def test_omitting_version_asks_for_every_version(
    post: Callable[..., Any], get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """The one place `version=None` does not mean "the latest"."""
    backend.add(key="welcome-email", version=1)
    backend.add(key="welcome-email", version=2)
    post("/api/v1/templates/welcome-email/activate", {"version": 1})
    post("/api/v1/templates/welcome-email/activate", {"version": 2})

    history = get("/api/v1/templates/welcome-email/status-history").json()["data"]

    assert sorted(record["version"] for record in history) == [1, 2]


def test_history_for_an_unknown_key_is_a_404(get: Callable[..., Any]) -> None:
    assert get("/api/v1/templates/nope/status-history").status_code == 404
