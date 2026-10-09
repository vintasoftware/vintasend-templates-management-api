"""How a JSON route reads its body, and the one 400 envelope every invalid input gets.

* A request declaring a JSON media type must carry valid JSON; an empty body there is
  malformed.
* A request declaring no media type, or another one, is read as ``{}`` when its body is
  empty -- which is what lets an all-optional body be omitted -- and refused when it is not.
* Every 400 carries ``details.issues``, whatever the mistake was.
"""

from typing import TYPE_CHECKING

from django.test import Client

import pytest

from .fakes import AUTH_HEADERS, InMemoryTemplateManagerBackend


if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse


ARCHIVE = "/api/v1/templates/welcome-email/archive"


def send(client: Client, path: str, body: bytes | str, content_type: str | None) -> "TestResponse":
    """POST raw bytes, with exactly the ``Content-Type`` given -- or none at all.

    Sent as a header rather than as ``content_type``, which the test client drops when the
    body is empty.
    """
    headers = (
        AUTH_HEADERS if content_type is None else {**AUTH_HEADERS, "Content-Type": content_type}
    )
    return client.generic("POST", path, body, content_type="", headers=headers)


@pytest.fixture
def two_versions(backend: InMemoryTemplateManagerBackend) -> InMemoryTemplateManagerBackend:
    backend.add(key="welcome-email", version=1)
    backend.add(key="welcome-email", version=2)
    return backend


def statuses(backend: InMemoryTemplateManagerBackend) -> dict[int, str]:
    return {
        template.version: template.status.value
        for template in backend.templates
        if template.key == "welcome-email"
    }


def assert_body_refused(response: "TestResponse", message: str) -> None:
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BAD_REQUEST"
    assert error["details"]["issues"] == [{"path": "", "message": message}]


# --- a body in another media type -----------------------------------------------------


def test_a_form_encoded_body_is_refused_rather_than_read(
    client: Client, two_versions: InMemoryTemplateManagerBackend
) -> None:
    """What `curl -d` sends. Read as `{}` it would archive the latest version; read as JSON
    it would be a request the contract does not describe. Archiving cannot be undone."""
    before = statuses(two_versions)

    response = send(client, ARCHIVE, '{"version":1}', "application/x-www-form-urlencoded")

    assert_body_refused(response, "Send the request body as application/json.")
    assert statuses(two_versions) == before


def test_a_body_with_no_content_type_is_refused(
    client: Client, two_versions: InMemoryTemplateManagerBackend
) -> None:
    before = statuses(two_versions)

    response = send(client, ARCHIVE, b'{"version":1}', None)

    assert_body_refused(response, "Send the request body as application/json.")
    assert statuses(two_versions) == before


def test_an_empty_body_in_another_media_type_is_an_omitted_body(
    client: Client, two_versions: InMemoryTemplateManagerBackend
) -> None:
    response = send(client, ARCHIVE, b"", "text/plain")

    assert response.status_code == 200
    assert response.json()["data"]["version"] == 2


def test_a_structured_json_media_type_is_read_as_json(
    client: Client, two_versions: InMemoryTemplateManagerBackend
) -> None:
    response = send(client, ARCHIVE, '{"version":1}', "application/merge-patch+json")

    assert response.status_code == 200
    assert response.json()["data"]["version"] == 1


def test_a_json_media_type_with_parameters_is_read_as_json(
    client: Client, two_versions: InMemoryTemplateManagerBackend
) -> None:
    response = send(client, ARCHIVE, '{"version":1}', "application/json; charset=utf-8")

    assert response.status_code == 200
    assert response.json()["data"]["version"] == 1


def test_a_required_body_in_another_media_type_is_refused(client: Client) -> None:
    response = send(client, "/api/v1/tags", "text=urgent", "application/x-www-form-urlencoded")

    assert_body_refused(response, "Send the request body as application/json.")


# --- a body declared as JSON ----------------------------------------------------------


def test_an_empty_body_declared_as_json_is_malformed(
    client: Client, two_versions: InMemoryTemplateManagerBackend
) -> None:
    before = statuses(two_versions)

    response = send(client, ARCHIVE, b"", "application/json")

    assert_body_refused(response, "Malformed JSON in request body")
    assert statuses(two_versions) == before


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/templates/welcome-email/activate",
        "/api/v1/templates/welcome-email/deactivate",
        "/api/v1/templates/welcome-email/archive",
        "/api/v1/templates/welcome-email/preview",
        "/api/v1/templates/welcome-email/versions",
    ],
)
def test_every_optional_body_refuses_an_empty_json_body(
    client: Client, two_versions: InMemoryTemplateManagerBackend, path: str
) -> None:
    assert_body_refused(
        send(client, path, b"", "application/json"), "Malformed JSON in request body"
    )


def test_malformed_json_uses_the_error_envelope(
    client: Client, two_versions: InMemoryTemplateManagerBackend
) -> None:
    response = send(client, ARCHIVE, '{"version":', "application/json")

    assert_body_refused(response, "Malformed JSON in request body")


@pytest.mark.parametrize("body", ["null", "[]", '"archive"', "2"])
def test_a_json_body_that_is_not_an_object_is_refused(
    client: Client, two_versions: InMemoryTemplateManagerBackend, body: str
) -> None:
    before = statuses(two_versions)

    response = send(client, ARCHIVE, body, "application/json")

    assert_body_refused(response, "The request body must be a JSON object.")
    assert statuses(two_versions) == before


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/templates/welcome-email/activate",
        "/api/v1/templates/welcome-email/deactivate",
        "/api/v1/templates/welcome-email/archive",
        "/api/v1/templates/welcome-email/preview",
    ],
)
def test_an_invalid_optional_body_is_a_400_listing_the_field(
    client: Client, two_versions: InMemoryTemplateManagerBackend, path: str
) -> None:
    response = send(client, path, '{"version":"two"}', "application/json")

    assert response.status_code == 400
    assert [issue["path"] for issue in response.json()["error"]["details"]["issues"]] == ["version"]


# --- one envelope for every 400 -------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/templates?orderByDirection=desc",
        "/api/v1/templates?page=0",
    ],
)
def test_every_400_lists_its_issues(client: Client, path: str) -> None:
    response = client.get(path, headers=AUTH_HEADERS)

    assert response.status_code == 400
    issues = response.json()["error"]["details"]["issues"]
    assert issues
    assert all(set(issue) == {"path", "message"} for issue in issues)


def test_a_refusal_from_the_library_lists_its_message_as_the_issue(client: Client) -> None:
    """Text with nothing sluggable in it gets past validation and is refused by the library."""
    response = send(client, "/api/v1/tags", '{"text":"!!!"}', "application/json")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["details"]["issues"] == [{"path": "", "message": error["message"]}]
