"""Tag CRUD, tagging templates, and searching templates by tag.

Every test drives the real HTTP stack over the real ``ManagedTemplateService``, so what is
being checked is the whole path: query validation, capability negotiation, the service's
normalization, and the wire shape that comes back.
"""

from collections.abc import Callable, Mapping, Sequence

import pytest
from pydantic import JsonValue
from vintasend_managed_templates.constants import ManagedTemplateTagStatus
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .conftest import ReadRequest, WriteRequest
from .fakes import InMemoryTemplateManagerBackend


def rows_of(body: Mapping[str, JsonValue]) -> list[Mapping[str, JsonValue]]:
    """Narrow a list envelope's ``data`` to rows, once, so callers can index them.

    Response JSON arrives untyped, and every helper below would otherwise re-assert its
    shape. Failing here with the envelope that came back also beats a ``TypeError`` raised
    somewhere further into a test.
    """
    data = body["data"]
    assert isinstance(data, list), f"expected a list envelope, got {body!r}"
    rows: list[Mapping[str, JsonValue]] = [row for row in data if isinstance(row, Mapping)]
    assert len(rows) == len(data), f"expected every row to be an object, got {data!r}"
    return rows


def slugs(rows: Sequence[Mapping[str, JsonValue]]) -> list[str]:
    return [str(row["slug"]) for row in rows]


def keys(body: Mapping[str, JsonValue]) -> list[str]:
    return [str(row["key"]) for row in rows_of(body)]


@pytest.fixture
def tagged(backend: InMemoryTemplateManagerBackend) -> InMemoryTemplateManagerBackend:
    """Three templates with overlapping tags. ``newsletter`` carries none."""
    transactional, onboarding, billing = backend.get_or_create_tags(
        ["Transactional", "Onboarding", "Billing"]
    )
    backend.add(key="welcome", tags=[transactional, onboarding])
    backend.add(key="receipt", tags=[transactional, billing])
    backend.add(key="newsletter", tags=[])
    return backend


# ----------------------------------------------------------------------
# Tag CRUD
# ----------------------------------------------------------------------


def test_creates_a_tag_and_derives_its_slug(post: WriteRequest) -> None:
    response = post("/api/v1/tags", {"text": "Black Friday"})

    assert response.status_code == 201
    tag = response.json()["data"]
    assert tag["text"] == "Black Friday"
    assert tag["slug"] == "black-friday"
    assert tag["status"] == "active"


def test_serializes_every_tag_contract_field(post: WriteRequest) -> None:
    tag = post("/api/v1/tags", {"text": "Black Friday", "tenant": "acme"}).json()["data"]

    assert tag == {
        "id": tag["id"],
        "text": "Black Friday",
        "slug": "black-friday",
        "status": "active",
        "tenant": "acme",
        "createdAt": "2024-01-15T09:00:00.000Z",
        "updatedAt": "2024-01-15T09:00:00.000Z",
    }


def test_creating_a_tag_whose_slug_is_taken_is_a_conflict(post: WriteRequest) -> None:
    """The request was well-formed and what it asked for is already there -- 409, not 400."""
    post("/api/v1/tags", {"text": "Black Friday"})

    response = post("/api/v1/tags", {"text": "black friday"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize("text", ["!!!", "---", "@#$"])
def test_a_tag_with_nothing_sluggable_is_a_bad_request(post: WriteRequest, text: str) -> None:
    response = post("/api/v1/tags", {"text": text})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_a_blank_tag_text_is_rejected_by_validation(post: WriteRequest) -> None:
    response = post("/api/v1/tags", {"text": ""})

    assert response.status_code == 400
    assert response.json()["error"]["details"]["issues"][0]["path"] == "text"


def test_reads_a_tag_by_slug_or_by_its_text(post: WriteRequest, get: ReadRequest) -> None:
    post("/api/v1/tags", {"text": "Black Friday"})

    assert get("/api/v1/tags/black-friday").json()["data"]["text"] == "Black Friday"
    assert get("/api/v1/tags/Black%20Friday").json()["data"]["text"] == "Black Friday"


def test_reading_an_unknown_tag_is_a_404(get: ReadRequest) -> None:
    response = get("/api/v1/tags/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_renaming_a_tag_regenerates_its_slug(post: WriteRequest, patch: WriteRequest) -> None:
    post("/api/v1/tags", {"text": "Blak Friday"})

    response = patch("/api/v1/tags/blak-friday", {"text": "Black Friday"})

    assert response.status_code == 200
    assert response.json()["data"] == {
        **response.json()["data"],
        "text": "Black Friday",
        "slug": "black-friday",
    }


def test_a_rename_reaches_the_templates_carrying_the_tag(
    post: WriteRequest, patch: WriteRequest, get: ReadRequest
) -> None:
    post(
        "/api/v1/templates",
        {
            "key": "welcome",
            "name": "Welcome",
            "templateManagedBackend": "django",
            "bodyTemplate": "<p>Hi</p>",
            "tags": ["Blak Friday"],
        },
    )

    patch("/api/v1/tags/blak-friday", {"text": "Black Friday"})

    template = get("/api/v1/templates/welcome").json()["data"]
    assert slugs(template["tags"]) == ["black-friday"]


def test_renaming_onto_a_taken_slug_gets_a_numeric_suffix(
    post: WriteRequest, patch: WriteRequest
) -> None:
    post("/api/v1/tags", {"text": "Black Friday"})
    post("/api/v1/tags", {"text": "Cyber Monday"})

    renamed = patch("/api/v1/tags/cyber-monday", {"text": "Black Friday"}).json()["data"]

    assert renamed["slug"] == "black-friday-2"


def test_renaming_to_text_with_nothing_sluggable_is_a_bad_request(
    post: WriteRequest, patch: WriteRequest
) -> None:
    post("/api/v1/tags", {"text": "Black Friday"})

    assert patch("/api/v1/tags/black-friday", {"text": "!!!"}).status_code == 400


def test_renaming_an_unknown_tag_is_a_404(patch: WriteRequest) -> None:
    assert patch("/api/v1/tags/nope", {"text": "Whatever"}).status_code == 404


def test_archiving_a_tag_retires_it(post: WriteRequest) -> None:
    post("/api/v1/tags", {"text": "Onboarding"})

    response = post("/api/v1/tags/onboarding/archive")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "archived"


def test_an_archived_tag_can_be_restored(post: WriteRequest) -> None:
    post("/api/v1/tags", {"text": "Onboarding"})
    post("/api/v1/tags/onboarding/archive")

    assert post("/api/v1/tags/onboarding/restore").json()["data"]["status"] == "active"


def test_archiving_an_unknown_tag_is_a_404(post: WriteRequest) -> None:
    assert post("/api/v1/tags/nope/archive").status_code == 404


def test_deletes_a_tag(post: WriteRequest, delete: ReadRequest, get: ReadRequest) -> None:
    post("/api/v1/tags", {"text": "Onboarding"})

    assert delete("/api/v1/tags/onboarding").status_code == 204
    assert get("/api/v1/tags/onboarding").status_code == 404


def test_deleting_a_tag_strips_it_from_the_templates_carrying_it(
    post: WriteRequest, delete: ReadRequest, get: ReadRequest
) -> None:
    post(
        "/api/v1/templates",
        {
            "key": "welcome",
            "name": "Welcome",
            "templateManagedBackend": "django",
            "bodyTemplate": "<p>Hi</p>",
            "tags": ["Onboarding", "Billing"],
        },
    )

    delete("/api/v1/tags/onboarding")

    assert slugs(get("/api/v1/templates/welcome").json()["data"]["tags"]) == ["billing"]


def test_deleting_an_unknown_tag_is_a_404(delete: ReadRequest) -> None:
    assert delete("/api/v1/tags/nope").status_code == 404


# ----------------------------------------------------------------------
# Listing tags
# ----------------------------------------------------------------------


def test_lists_tags_with_the_pagination_envelope(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.get_or_create_tags(["Onboarding", "Billing"])

    body = get("/api/v1/tags").json()

    assert sorted(slugs(body["data"])) == ["billing", "onboarding"]
    assert (body["page"], body["pageSize"], body["hasMore"]) == (1, 20, False)


def test_tag_pages_are_one_indexed(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.get_or_create_tags(["Alpha", "Beta"])

    first = get("/api/v1/tags?page=1&pageSize=1").json()
    second = get("/api/v1/tags?page=2&pageSize=1").json()

    assert slugs(first["data"]) == ["alpha"]
    assert slugs(second["data"]) == ["beta"]
    assert first["hasMore"] is True


def test_tags_can_be_narrowed_by_status(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.get_or_create_tags(["Onboarding", "Billing"])
    backend.set_tag_status("billing", ManagedTemplateTagStatus.ARCHIVED)

    assert slugs(get("/api/v1/tags?status=active").json()["data"]) == ["onboarding"]
    assert slugs(get("/api/v1/tags?status=archived").json()["data"]) == ["billing"]


def test_repeating_the_status_parameter_asks_for_several(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.get_or_create_tags(["Onboarding", "Billing"])
    backend.set_tag_status("billing", ManagedTemplateTagStatus.ARCHIVED)

    body = get("/api/v1/tags?status=active&status=archived").json()

    assert sorted(slugs(body["data"])) == ["billing", "onboarding"]


def test_an_unknown_tag_status_is_a_bad_request(get: ReadRequest) -> None:
    assert get("/api/v1/tags?status=nope").status_code == 400


def test_tags_can_be_searched(get: ReadRequest, backend: InMemoryTemplateManagerBackend) -> None:
    backend.get_or_create_tags(["Black Friday", "Onboarding"])

    assert slugs(get("/api/v1/tags?search=friday").json()["data"]) == ["black-friday"]


def test_a_blank_search_is_rejected_rather_than_matching_everything(
    get: ReadRequest,
) -> None:
    assert get("/api/v1/tags?search=%20%20").status_code == 400


def test_tags_can_be_narrowed_by_tenant(
    get: ReadRequest, backend: InMemoryTemplateManagerBackend
) -> None:
    backend.get_or_create_tags(["Onboarding"], "acme")
    backend.get_or_create_tags(["Billing"], "other")

    assert slugs(get("/api/v1/tags?tenant=acme").json()["data"]) == ["onboarding"]


# ----------------------------------------------------------------------
# Tagging templates
# ----------------------------------------------------------------------


CREATE_BODY = {
    "key": "welcome",
    "name": "Welcome",
    "templateManagedBackend": "django",
    "bodyTemplate": "<p>Hi</p>",
}


def test_creating_a_template_with_tags_creates_them_on_the_fly(
    post: WriteRequest, get: ReadRequest
) -> None:
    response = post("/api/v1/templates", {**CREATE_BODY, "tags": ["Transactional", "Onboarding"]})

    assert response.status_code == 201
    assert slugs(response.json()["data"]["tags"]) == ["transactional", "onboarding"]
    assert sorted(slugs(get("/api/v1/tags").json()["data"])) == ["onboarding", "transactional"]


def test_a_template_created_without_tags_reports_an_empty_list(post: WriteRequest) -> None:
    assert post("/api/v1/templates", CREATE_BODY).json()["data"]["tags"] == []


def test_a_template_tag_that_cannot_be_slugified_is_a_bad_request(post: WriteRequest) -> None:
    response = post("/api/v1/templates", {**CREATE_BODY, "tags": ["!!!"]})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_a_new_version_carries_the_tags_forward_when_none_are_sent(
    post: WriteRequest,
) -> None:
    post("/api/v1/templates", {**CREATE_BODY, "tags": ["Onboarding"]})

    response = post("/api/v1/templates/welcome/versions", {"name": "Renamed"})

    assert slugs(response.json()["data"]["tags"]) == ["onboarding"]


def test_a_new_version_can_replace_the_tags(post: WriteRequest) -> None:
    post("/api/v1/templates", {**CREATE_BODY, "tags": ["Onboarding"]})

    response = post("/api/v1/templates/welcome/versions", {"tags": ["Billing"]})

    assert slugs(response.json()["data"]["tags"]) == ["billing"]


def test_a_new_version_can_clear_the_tags_with_an_empty_list(post: WriteRequest) -> None:
    """`[]` and an omitted field mean different things on this body, unlike every other."""
    post("/api/v1/templates", {**CREATE_BODY, "tags": ["Onboarding"]})

    response = post("/api/v1/templates/welcome/versions", {"tags": []})

    assert response.json()["data"]["tags"] == []


def test_retagging_a_version_in_place_does_not_create_a_version(
    post: WriteRequest, put: WriteRequest, get: ReadRequest
) -> None:
    created = post("/api/v1/templates", {**CREATE_BODY, "tags": ["Onboarding"]}).json()["data"]

    response = put("/api/v1/templates/welcome/tags", {"tags": ["Billing"]})

    assert response.status_code == 200
    assert response.json()["data"]["version"] == created["version"]
    assert len(get("/api/v1/templates/welcome/versions").json()["data"]) == 1


def test_retagging_leaves_the_versions_status_alone(post: WriteRequest, put: WriteRequest) -> None:
    post("/api/v1/templates", CREATE_BODY)
    post("/api/v1/templates/welcome/activate")

    assert (
        put("/api/v1/templates/welcome/tags", {"tags": ["Billing"]}).json()["data"]["status"]
        == "active"
    )


def test_retagging_with_an_empty_list_clears_the_tags(
    post: WriteRequest, put: WriteRequest
) -> None:
    post("/api/v1/templates", {**CREATE_BODY, "tags": ["Onboarding"]})

    assert put("/api/v1/templates/welcome/tags", {"tags": []}).json()["data"]["tags"] == []


def test_retagging_can_name_an_explicit_version(
    post: WriteRequest, put: WriteRequest, get: ReadRequest
) -> None:
    post("/api/v1/templates", {**CREATE_BODY, "tags": ["Onboarding"]})
    post("/api/v1/templates/welcome/versions", {"name": "v2"})

    put("/api/v1/templates/welcome/tags", {"tags": ["Billing"], "version": 1})

    assert slugs(get("/api/v1/templates/welcome?version=1").json()["data"]["tags"]) == ["billing"]
    assert slugs(get("/api/v1/templates/welcome?version=2").json()["data"]["tags"]) == [
        "onboarding"
    ]


def test_retagging_an_unknown_template_is_a_404(put: WriteRequest) -> None:
    assert put("/api/v1/templates/nope/tags", {"tags": ["Billing"]}).status_code == 404


def test_retagging_with_an_unusable_tag_is_a_bad_request(
    post: WriteRequest, put: WriteRequest
) -> None:
    post("/api/v1/templates", CREATE_BODY)

    assert put("/api/v1/templates/welcome/tags", {"tags": ["!!!"]}).status_code == 400


# ----------------------------------------------------------------------
# Searching templates by tag
# ----------------------------------------------------------------------


def test_includes_all_tags_requires_every_tag(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    body = get("/api/v1/templates?includesAllTags=transactional&includesAllTags=onboarding").json()

    assert keys(body) == ["welcome"]


def test_includes_any_of_tags_requires_only_one(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    body = get("/api/v1/templates?includesAnyOfTags=onboarding&includesAnyOfTags=billing").json()

    assert sorted(keys(body)) == ["receipt", "welcome"]


def test_a_single_tag_means_the_same_under_either_parameter(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    all_of = get("/api/v1/templates?includesAllTags=transactional").json()
    any_of = get("/api/v1/templates?includesAnyOfTags=transactional").json()

    assert sorted(keys(all_of)) == sorted(keys(any_of)) == ["receipt", "welcome"]


def test_a_tag_filter_accepts_the_text_behind_the_slug(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    body = get("/api/v1/templates?includesAnyOfTags=Transactional").json()

    assert sorted(keys(body)) == ["receipt", "welcome"]


def test_an_unknown_tag_matches_nothing(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    assert get("/api/v1/templates?includesAnyOfTags=nope").json()["data"] == []


def test_the_two_tag_parameters_combine_with_and(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    body = get("/api/v1/templates?includesAllTags=transactional&includesAnyOfTags=billing").json()

    assert keys(body) == ["receipt"]


def test_a_tag_filter_combines_with_the_other_filters(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    body = get("/api/v1/templates?includesAnyOfTags=transactional&key=receipt").json()

    assert keys(body) == ["receipt"]


def test_a_blank_tag_filter_is_rejected_rather_than_matching_nothing(
    get: ReadRequest,
) -> None:
    """A parameter present but empty is a client bug, not a filter that silently matches
    nothing -- which is what ``includesAnyOfTags`` with no tags would be."""
    response = get("/api/v1/templates?includesAnyOfTags=%20")

    assert response.status_code == 400
    assert response.json()["error"]["details"]["issues"][0]["path"] == "includesAnyOfTags"


def test_blank_entries_alongside_real_ones_are_dropped(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    """A trailing comma in a tag input is a UI artifact, not something a person meant."""
    body = get("/api/v1/templates?includesAnyOfTags=transactional&includesAnyOfTags=%20").json()

    assert sorted(keys(body)) == ["receipt", "welcome"]


def test_too_many_tags_in_one_filter_are_rejected(get: ReadRequest) -> None:
    query = "&".join(f"includesAllTags=tag-{index}" for index in range(51))

    assert get(f"/api/v1/templates?{query}").status_code == 400


def test_tag_filters_paginate_without_duplicating_a_multi_tag_match(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    query = "includesAnyOfTags=transactional&includesAnyOfTags=onboarding"
    first = get(f"/api/v1/templates?{query}&page=1&pageSize=1").json()
    second = get(f"/api/v1/templates?{query}&page=2&pageSize=1").json()

    assert keys(first) == ["welcome"]
    assert keys(second) == ["receipt"]


def test_filtering_by_an_archived_tag_still_finds_its_templates(
    get: ReadRequest, post: WriteRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    post("/api/v1/tags/transactional/archive")

    body = get("/api/v1/templates?includesAnyOfTags=transactional").json()

    assert sorted(keys(body)) == ["receipt", "welcome"]


def test_templates_report_their_tags_in_the_list_response(
    get: ReadRequest, tagged: InMemoryTemplateManagerBackend
) -> None:
    rows = {row["key"]: row for row in get("/api/v1/templates").json()["data"]}

    assert slugs(rows["welcome"]["tags"]) == ["transactional", "onboarding"]
    assert rows["newsletter"]["tags"] == []


# ----------------------------------------------------------------------
# Capability negotiation
# ----------------------------------------------------------------------


def test_the_tag_capabilities_default_to_supported(get: ReadRequest) -> None:
    capabilities = get("/api/v1/capabilities").json()["data"]

    assert capabilities["fields.includesAllTags"] is True
    assert capabilities["fields.includesAnyOfTags"] is True


def test_a_declined_tag_filter_is_dropped_rather_than_failing_the_request(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """Dropping an unsupported filter is the contract's choice; failing the request is not."""
    backend = InMemoryTemplateManagerBackend(capabilities={"fields.includesAllTags": False})
    install_service(template_backend=backend)
    tag = backend.get_or_create_tags(["Onboarding"])[0]
    backend.add(key="welcome", tags=[tag])
    backend.add(key="receipt", tags=[])

    body = get("/api/v1/templates?includesAllTags=onboarding").json()

    assert sorted(keys(body)) == ["receipt", "welcome"]


def test_the_two_tag_capabilities_are_declined_independently(
    get: ReadRequest, install_service: Callable[..., ManagedTemplateService]
) -> None:
    """ "Every tag" and "at least one tag" are different queries, so they are different keys."""
    backend = InMemoryTemplateManagerBackend(capabilities={"fields.includesAllTags": False})
    install_service(template_backend=backend)
    tag = backend.get_or_create_tags(["Onboarding"])[0]
    backend.add(key="welcome", tags=[tag])
    backend.add(key="receipt", tags=[])

    body = get("/api/v1/templates?includesAnyOfTags=onboarding").json()

    assert keys(body) == ["welcome"]
