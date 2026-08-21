"""Rendering a template version against a supplied context."""

from collections.abc import Callable

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .conftest import WriteRequest
from .fakes import (
    BodylessRenderer,
    FakeEmailRenderer,
    FakeSMSRenderer,
    InMemoryTemplateManagerBackend,
)


def test_renders_the_latest_version(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(
        key="welcome-email",
        body_template="<p>Hello {{ name }}</p>",
        subject_template="Welcome, {{ name }}",
    )

    response = post("/api/v1/templates/welcome-email/preview", {"context": {"name": "Ana"}})

    assert response.status_code == 200
    assert response.json()["data"] == {
        "key": "welcome-email",
        "version": 1,
        "renderedBody": "<p>Hello Ana</p>",
        "renderedSubject": "Welcome, Ana",
        "renderedPreheader": None,
    }


def test_renders_a_pinned_version(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1, body_template="old")
    backend.add(key="welcome-email", version=2, body_template="new")

    response = post("/api/v1/templates/welcome-email/preview", {"version": 1})

    assert response.json()["data"]["renderedBody"] == "old"
    assert response.json()["data"]["version"] == 1


def test_previews_an_unpublished_draft(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """The reason this endpoint pins a version: a draft is reviewable before activation."""
    backend.add(
        key="welcome-email", version=1, status=ManagedTemplateStatus.ACTIVE, body_template="live"
    )
    backend.add(
        key="welcome-email", version=2, status=ManagedTemplateStatus.DRAFT, body_template="draft"
    )

    response = post("/api/v1/templates/welcome-email/preview", {"version": 2})

    assert response.status_code == 200
    assert response.json()["data"]["renderedBody"] == "draft"


def test_renders_a_preheader_when_the_template_has_one(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", preheader_template="Hi {{ name }}")

    data = post("/api/v1/templates/welcome-email/preview", {"context": {"name": "Ana"}}).json()[
        "data"
    ]

    assert data["renderedPreheader"] == "Hi Ana"


def test_a_body_only_renderer_reports_no_subject(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    install_service: Callable[..., ManagedTemplateService],
) -> None:
    """An SMS renderer produces a body and nothing else, which is not an error."""
    install_service(renderer=FakeSMSRenderer())
    backend.add(key="alert-sms", body_template="Alert for {{ name }}")

    data = post("/api/v1/templates/alert-sms/preview", {"context": {"name": "Ana"}}).json()["data"]

    assert data["renderedBody"] == "Alert for Ana"
    assert data["renderedSubject"] is None
    assert data["renderedPreheader"] is None


def test_an_empty_context_is_accepted(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", body_template="static body")

    response = post("/api/v1/templates/welcome-email/preview")

    assert response.status_code == 200
    assert response.json()["data"]["renderedBody"] == "static body"


def test_a_broken_template_is_reported_as_preview_unavailable(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    install_service: Callable[..., ManagedTemplateService],
) -> None:
    """A template that will not render is what the caller asked to find out, not a 500."""
    install_service(renderer=FakeEmailRenderer(raises=ValueError("unclosed tag on line 3")))
    backend.add(key="welcome-email")

    response = post("/api/v1/templates/welcome-email/preview")

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "PREVIEW_UNAVAILABLE"
    assert "unclosed tag on line 3" in body["message"]


def test_a_renderer_with_no_text_body_is_reported_rather_than_shown_empty(
    post: WriteRequest,
    backend: InMemoryTemplateManagerBackend,
    install_service: Callable[..., ManagedTemplateService],
) -> None:
    install_service(renderer=BodylessRenderer())
    backend.add(key="welcome-email")

    response = post("/api/v1/templates/welcome-email/preview")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PREVIEW_UNAVAILABLE"


def test_renders_a_nested_context(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """A nested object is an entirely ordinary context and must survive the round trip.

    ``NotificationContextDict`` cannot represent one -- see ``preview._as_context`` -- which
    is why the context is passed through as the plain dict a real send carries.
    """
    backend.add(key="welcome-email", body_template="<p>Hi</p>", subject_template="Hi")

    response = post(
        "/api/v1/templates/welcome-email/preview",
        {"context": {"user": {"name": "Ana"}, "orders": [{"id": 1}]}},
    )

    assert response.status_code == 200


def test_renders_context_values_the_seam_type_would_reject(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """``None`` values and lists of strings both appear in stored contexts.

    Refusing them here would make the preview stricter than the send it previews.
    """
    backend.add(key="welcome-email", body_template="<p>{{ nickname }}</p>")

    response = post(
        "/api/v1/templates/welcome-email/preview",
        {"context": {"nickname": "Ana", "middleName": None, "tags": ["a", "b"]}},
    )

    assert response.status_code == 200
    assert response.json()["data"]["renderedBody"] == "<p>Ana</p>"


def test_previewing_an_unknown_key_is_a_404(post: WriteRequest) -> None:
    assert post("/api/v1/templates/nope/preview").status_code == 404


def test_previewing_an_unknown_version_is_a_404(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)

    assert post("/api/v1/templates/welcome-email/preview", {"version": 9}).status_code == 404


def test_the_preview_reads_the_pinned_version_not_the_renderers_own_lookup(
    post: WriteRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    """`ManagedTemplateRenderer.render` would resolve the key to the latest version.

    The service drives `create_template_content` / `render_from_template_content` instead,
    so the renderer's own backend is never consulted -- which is what makes pinning work.
    """
    backend.add(key="welcome-email", version=1, body_template="old")
    backend.add(key="welcome-email", version=2, body_template="new")

    post("/api/v1/templates/welcome-email/preview", {"version": 1})

    assert ("get_template", ("welcome-email", 1)) in backend.calls
