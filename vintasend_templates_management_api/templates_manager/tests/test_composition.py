"""Templates that build on other templates, over the wire.

A managed template can extend a base, fill its ``{% managed_children %}`` hole, override
its blocks and splice fragments in. All of it is resolved before any template engine runs,
so the stored ``bodyTemplate`` is only half the story -- which is what
``GET /templates/{key}/composition`` exists to show, and what a preview already reflects.

Three surfaces are covered here: the ``isAbstract`` flag on every template payload and the
filter that queries it, the composition endpoint, and the fact that previewing composes.
"""

from typing import Any, Callable

from .fakes import InMemoryTemplateManagerBackend


BASE_BODY = "<html><body>{% managed_block header %}Acme{% managed_endblock %}"
BASE_BODY += "{% managed_children %}</body></html>"


def _seed_base_and_child(backend: InMemoryTemplateManagerBackend) -> None:
    backend.add(key="base-email", body_template=BASE_BODY, subject_template=None)
    backend.add(
        key="welcome-email",
        body_template='{% managed_extends "base-email" %}<p>Hi</p>',
        subject_template=None,
    )


# --- the flag ------------------------------------------------------------------------


def test_a_base_is_reported_as_abstract(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    _seed_base_and_child(backend)

    rows = {row["key"]: row for row in get("/api/v1/templates").json()["data"]}

    assert rows["base-email"]["isAbstract"] is True
    assert rows["welcome-email"]["isAbstract"] is False


def test_the_flag_is_on_a_single_version_payload(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    _seed_base_and_child(backend)

    assert get("/api/v1/templates/base-email").json()["data"]["isAbstract"] is True


# --- the filter ----------------------------------------------------------------------


def test_filtering_to_the_bases(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    _seed_base_and_child(backend)

    response = get("/api/v1/templates?isAbstract=true")

    assert [row["key"] for row in response.json()["data"]] == ["base-email"]


def test_filtering_to_what_can_be_sent(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """What a "pick a template to send" screen asks for: everything except the bases."""
    _seed_base_and_child(backend)

    response = get("/api/v1/templates?isAbstract=false")

    assert [row["key"] for row in response.json()["data"]] == ["welcome-email"]


def test_omitting_the_filter_lists_both(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    _seed_base_and_child(backend)

    response = get("/api/v1/templates")

    assert {row["key"] for row in response.json()["data"]} == {"base-email", "welcome-email"}


def test_a_backend_that_cannot_filter_on_it_gets_the_unfiltered_listing(
    get: Callable[..., Any], install_service: Callable[..., Any]
) -> None:
    """An unsupported filter is dropped, never an error -- the contract's choice."""
    declining = InMemoryTemplateManagerBackend({"fields.isAbstract": False})
    _seed_base_and_child(declining)
    install_service(template_backend=declining)

    response = get("/api/v1/templates?isAbstract=false")

    assert response.status_code == 200
    assert {row["key"] for row in response.json()["data"]} == {"base-email", "welcome-email"}


def test_the_capability_is_advertised(get: Callable[..., Any]) -> None:
    capabilities = get("/api/v1/capabilities").json()["data"]

    assert capabilities["fields.isAbstract"] is True


# --- the composition endpoint --------------------------------------------------------


def test_returns_the_assembled_sources(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    _seed_base_and_child(backend)

    response = get("/api/v1/templates/welcome-email/composition")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "key": "welcome-email",
        "version": 1,
        "isAbstract": False,
        "references": [
            {"kind": "extends", "key": "base-email", "version": None, "field": "bodyTemplate"}
        ],
        "composedBodyTemplate": "<html><body>Acme<p>Hi</p></body></html>",
        "composedSubjectTemplate": None,
        "composedPreheaderTemplate": None,
    }


def test_engine_syntax_survives_composition(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """Nothing here is rendered: a preview does that. Composition only assembles."""
    backend.add(key="base-email", body_template="[{% managed_children %}]")
    backend.add(
        key="welcome-email",
        body_template='{% managed_extends "base-email" %}<p>{{ name }}</p>',
    )

    composed = get("/api/v1/templates/welcome-email/composition").json()["data"]

    assert composed["composedBodyTemplate"] == "[<p>{{ name }}</p>]"


def test_a_template_that_composes_to_itself_is_returned_unchanged(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="plain", body_template="<p>Hi</p>", subject_template="Hello")

    composed = get("/api/v1/templates/plain/composition").json()["data"]

    assert composed["composedBodyTemplate"] == "<p>Hi</p>"
    assert composed["composedSubjectTemplate"] == "Hello"
    assert composed["references"] == []


def test_a_version_can_be_pinned(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="base-email", body_template="v1:{% managed_children %}", version=1)
    backend.add(key="base-email", body_template="v2:{% managed_children %}", version=2)
    backend.add(
        key="welcome-email",
        version=1,
        body_template='{% managed_extends "base-email[v1]" %}Hi',
    )

    composed = get("/api/v1/templates/welcome-email/composition?version=1").json()["data"]

    assert composed["composedBodyTemplate"] == "v1:Hi"
    assert composed["references"] == [
        {"kind": "extends", "key": "base-email", "version": 1, "field": "bodyTemplate"}
    ]


def test_reports_a_base_that_does_not_exist(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """A 409, not a 404: the template asked for exists -- what it names does not."""
    backend.add(key="welcome-email", body_template='{% managed_extends "nowhere" %}Hi')

    response = get("/api/v1/templates/welcome-email/composition")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TEMPLATE_COMPOSITION_ERROR"
    assert "nowhere" in response.json()["error"]["message"]


def test_reports_a_malformed_tag(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="broken", body_template="{% managed_block a %}never closed")

    response = get("/api/v1/templates/broken/composition")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TEMPLATE_COMPOSITION_ERROR"


def test_reports_a_loop(get: Callable[..., Any], backend: InMemoryTemplateManagerBackend) -> None:
    backend.add(key="first", body_template='{% managed_extends "second" %}')
    backend.add(key="second", body_template='{% managed_extends "first" %}')

    response = get("/api/v1/templates/first/composition")

    assert response.status_code == 409
    assert "loops" in response.json()["error"]["message"]


def test_an_unknown_key_is_a_404(get: Callable[..., Any]) -> None:
    response = get("/api/v1/templates/nowhere/composition")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_an_unknown_version_is_a_404(
    get: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", version=1)

    response = get("/api/v1/templates/welcome-email/composition?version=7")

    assert response.status_code == 404


def test_the_endpoint_requires_authentication(client: Any) -> None:
    response = client.get("/api/v1/templates/welcome-email/composition")

    assert response.status_code == 401


# --- previewing composes -------------------------------------------------------------


def test_a_preview_renders_the_composed_template(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    """The point of composing before the engine: a preview shows the base's chrome too."""
    backend.add(key="base-email", body_template="[{% managed_children %}]")
    backend.add(
        key="welcome-email",
        body_template='{% managed_extends "base-email" %}<p>Hello {{ name }}</p>',
        subject_template="Welcome, {{ name }}",
    )

    response = post("/api/v1/templates/welcome-email/preview", {"context": {"name": "Ana"}})

    assert response.status_code == 200
    assert response.json()["data"]["renderedBody"] == "[<p>Hello Ana</p>]"


def test_a_preview_of_a_template_that_cannot_be_assembled_says_so(
    post: Callable[..., Any], backend: InMemoryTemplateManagerBackend
) -> None:
    backend.add(key="welcome-email", body_template='{% managed_extends "nowhere" %}Hi')

    response = post("/api/v1/templates/welcome-email/preview", {})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PREVIEW_UNAVAILABLE"
    assert "nowhere" in response.json()["error"]["message"]
