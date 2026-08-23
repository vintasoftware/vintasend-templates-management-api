"""Wire contract for the VintaSend managed-templates API.

These schemas describe the JSON payloads exchanged over HTTP, and they are what
``openapi.yaml`` is generated from -- the schema is a *product* of this module, not a
document kept in step with it by hand. Regenerate with ``manage.py export_openapi``.

They intentionally avoid re-exporting anything from ``vintasend_managed_templates``: a
consumer of this API needs these definitions and nothing else, and any future
implementation of the same contract (in another language, over another storage seam)
must produce exactly these shapes.

Attribute names are camelCase on purpose, so these classes can be read side by side with
the generated schema and with a TypeScript client's types. ``ruff``'s N815 is disabled
for this module in ``pyproject.toml`` for that reason.

All timestamps are ISO-8601 strings in UTC, and are ``null`` when unset -- never absent.
"""

from typing import Generic, Literal, TypeVar

from ninja import Schema

from pydantic import JsonValue


API_VERSION = "v1"

API_BASE_PATH = f"/api/{API_VERSION}"

# The wire spelling of ``ManagedTemplateStatus``. Kept as a literal rather than derived
# from the enum so a status added to the library is a deliberate contract change here
# rather than one that appears in responses unannounced.
TemplateStatus = Literal["draft", "active", "inactive", "archived"]

# The wire spelling of ``ManagedTemplateTagStatus``, kept as a literal for the same reason
# ``TemplateStatus`` is.
TagStatus = Literal["active", "archived"]

# How a multi-tag filter combines. ``all`` requires every tag, ``any`` at least one.
TagMatchMode = Literal["all", "any"]

# Which field a list query may order by, in the wire's camelCase.
#
# Deliberately narrow: each one is a scalar the backend already stores per row, so a store
# can answer it from an index rather than with a computed sort. Tags are absent because
# ordering by a many-to-many has no single value to compare, and ``mostRecentActiveVersion``
# because it is a filter rather than a field.
#
# This literal is one of three places the same six names appear -- the others are the enum in
# ``openapi.yaml`` and ``MANAGED_TEMPLATE_ORDER_BY_FIELDS`` in the library. ``test_openapi.py``
# pins all three against each other, so a field added to one and forgotten in the others fails
# a test rather than a client.
TemplateOrderByField = Literal["key", "name", "version", "status", "createdAt", "updatedAt"]

TemplateOrderDirection = Literal["asc", "desc"]

# Machine-readable error codes. Clients branch on these, not on messages.
ApiErrorCode = Literal[
    "BAD_REQUEST",
    "UNAUTHORIZED",
    "NOT_FOUND",
    "CONFLICT",
    "INVALID_STATUS_TRANSITION",
    "PREVIEW_UNAVAILABLE",
    "TEMPLATE_COMPOSITION_ERROR",
    "INTERNAL_ERROR",
]

# What a template reference is: a template building on another one, or splicing one in.
TemplateReferenceKind = Literal["extends", "include"]

# Which of a template's three sources a reference was written in. Each composes against the
# same field of the template it names, so a reference in the subject resolves the referenced
# template's subject.
TemplateSourceField = Literal["bodyTemplate", "subjectTemplate", "preheaderTemplate"]


class ManagedTemplateTagOut(Schema):
    """A label attached to any number of template versions.

    ``slug`` is the tag's identity: it is normalized from ``text``, unique store-wide, and
    what the ``includesAllTags`` / ``includesAnyOfTags`` filters match on. A client that
    stores a tag reference should store the slug, and re-read it after a rename -- editing
    ``text`` regenerates the slug, so the old one stops matching.
    """

    id: str
    text: str
    slug: str
    status: TagStatus
    tenant: str | None
    createdAt: str | None
    updatedAt: str | None


class ManagedTemplateOut(Schema):
    """One version of a managed template.

    Templates are versioned rather than edited in place, so this is always a specific
    version of ``key`` -- never "the template" in the abstract.
    """

    id: str
    key: str
    version: int
    name: str
    description: str
    templateManagedBackend: str
    bodyTemplate: str
    subjectTemplate: str | None
    preheaderTemplate: str | None
    status: TemplateStatus
    tenant: str | None
    createdAt: str | None
    updatedAt: str | None
    # Every tag on this version. Tags hang off a version rather than a key, so two versions
    # of one template can be labelled differently.
    tags: list[ManagedTemplateTagOut]
    # Which statuses this version may move to right now, as the configured service
    # answers it. Present so a UI enables exactly the buttons that will work instead of
    # reimplementing the transition table -- or discovering the answer by catching a 409.
    allowedTransitions: list[TemplateStatus]
    # True when this is a base to build on rather than a template to send: it declares a
    # ``{% managed_children %}`` hole, or blocks without extending anything. Read from the
    # backend's stored flag, so it costs no parsing and cannot fail -- ask
    # ``GET /templates/{key}/composition`` for the recomputed answer and the reasons behind
    # it. A picker choosing a *sendable* template should leave these out; a picker choosing
    # a base to extend should show only these.
    isAbstract: bool


class TemplateStatusHistoryOut(Schema):
    """One entry in a template version's status audit trail."""

    templateKey: str
    version: int
    status: TemplateStatus
    # The library's ``ManagedTemplateStatusHistory`` field is ``created_by``; the service
    # method that writes it takes ``changed_by``. The wire uses ``changedBy``, matching
    # the name a caller sets rather than the name the record stores.
    changedBy: str | None
    tenant: str | None
    createdAt: str | None


class TemplateReferenceOut(Schema):
    """One template this version directly extends or includes.

    Direct references only -- what the referenced templates themselves reference is not
    followed. ``version`` is null when the reference names no version, which resolves to
    whatever that key currently is at render time.
    """

    kind: TemplateReferenceKind
    key: str
    version: int | None
    field: TemplateSourceField


class TemplateCompositionOut(Schema):
    """A template version assembled the way the engine will receive it.

    Composition is resolved before any template engine runs: a version that extends a base
    or includes a fragment reaches the engine as one flat string with no ``managed_*`` tag
    left in it. The stored sources on ``ManagedTemplateOut`` are what someone typed; these
    are what actually renders.

    Nothing here is rendered against a context -- ``{{ name }}`` and every other engine tag
    survives untouched. Use ``POST /templates/{key}/preview`` to see the rendered result.
    """

    key: str
    version: int
    # Recomputed from the source rather than read from the stored flag, which makes this
    # the authority the flag on ``ManagedTemplateOut`` is a copy of.
    isAbstract: bool
    references: list[TemplateReferenceOut]
    composedBodyTemplate: str
    composedSubjectTemplate: str | None
    composedPreheaderTemplate: str | None


class TemplatePreviewOut(Schema):
    """A template version rendered against a caller-supplied context.

    ``renderedSubject`` and ``renderedPreheader`` are ``null`` for renderers that do not
    produce them -- an SMS renderer produces a body only.
    """

    key: str
    version: int
    renderedBody: str
    renderedSubject: str | None
    renderedPreheader: str | None


T = TypeVar("T")


class PaginatedResponse(Schema, Generic[T]):
    """Envelope returned by every paginated endpoint. ``page`` is 1-indexed."""

    data: list[T]
    page: int
    pageSize: int
    # True when the page came back full, meaning another page may exist. The template
    # manager seam has no count method, so no total is available.
    hasMore: bool


class DataResponse(Schema, Generic[T]):
    """Envelope returned by every single-resource endpoint."""

    data: T


class ListResponse(Schema, Generic[T]):
    """Envelope returned by unpaginated collection endpoints (versions, history)."""

    data: list[T]


class HealthOut(Schema):
    status: Literal["ok"] = "ok"
    apiVersion: str = API_VERSION


class ApiErrorBody(Schema):
    code: ApiErrorCode
    message: str
    # Optional machine-readable context, such as field issues. `JsonValue` rather than
    # `Any`: the payload really can be any shape, but it has to survive a JSON round trip,
    # and typing it says so.
    #
    # Absent rather than null when there is nothing to report. That is enforced where the
    # body is actually built -- `api._envelope` adds the key only when it has a value --
    # not here: this class is never instantiated. Error responses are `JsonResponse`s
    # written by the exception handlers, so `ApiErrorBody` exists purely to describe them
    # in the generated schema.
    #
    # It used to carry a `model_serializer` that dropped a null `details`. That never ran
    # for the same reason, and it had one real effect: pydantic builds a serialization
    # schema from a wrap serializer's return type, so the whole error body was published
    # as an empty schema and clients reading `openapi.yaml` saw no `code` or `message` at
    # all. Describing the envelope is this class's only job, so the serializer had to go.
    details: JsonValue | None = None


class ApiErrorResponse(Schema):
    """Error envelope returned with every non-2xx response."""

    error: ApiErrorBody
