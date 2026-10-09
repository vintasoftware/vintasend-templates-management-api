"""Request validation schemas.

These define, precisely, what the API accepts, and they are what the generated
``openapi.yaml`` documents as each operation's parameters and request body.

Query parameter and body field names are camelCase to match the contract, so the
attribute names here are camelCase too (``ruff``'s N815 is disabled for this module in
``pyproject.toml``).
"""

import datetime

from ninja import Field, Schema

from pydantic import field_validator

from .contract import TagStatus, TemplateOrderByField, TemplateOrderDirection, TemplateStatus


DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MIN_PAGE_SIZE = 1
# Largest page a client may ask for, guarding a backend that materialises a whole page in
# memory. A module constant rather than a setting because it is published as a `maximum`
# in the generated schema, which a per-deployment value could not be.
MAX_PAGE_SIZE = 100

# Longest template body/subject/preheader this API accepts. Templates are source text, not
# documents, and a bound keeps a single request from pinning an arbitrary amount of memory.
MAX_TEMPLATE_LENGTH = 1_000_000

# Longest tag text this API accepts, matching the 255-char column backends store it in.
MAX_TAG_LENGTH = 255

# Most tags one request may name -- on a template, or in a single tag filter. A bound rather
# than an unbounded list because each tag in an ``includesAllTags`` filter is a term the
# backend has to satisfy, and an arbitrarily long list is an arbitrarily expensive query.
MAX_TAGS_PER_REQUEST = 50


def _trimmed_or_none(value: str | None) -> str | None:
    """Trim, then reject what is left if it is empty.

    A parameter present but blank is a client bug worth a 400, not a filter that silently
    matches nothing.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("String should have at least 1 character")
    return trimmed


def _clean_tags(value: list[str] | None) -> list[str] | None:
    """Trim each tag, drop the blanks, and reject a list that was nothing but blanks.

    Blank entries are dropped rather than rejected because a trailing comma in a tag input
    is a UI artifact, not something a person meant. A list containing *only* blanks is a
    different thing: the caller asked to filter by tags and named none, which under
    ``includesAnyOfTags`` would silently match nothing.
    """
    if value is None:
        return None
    cleaned = [tag.strip() for tag in value if tag and tag.strip()]
    if not cleaned:
        raise ValueError("At least one non-empty tag is required")
    return cleaned


class PaginationQuery(Schema):
    page: int = Field(DEFAULT_PAGE, ge=1)
    pageSize: int = Field(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE)


class VersionQuery(Schema):
    """``version`` on an endpoint that acts on the latest version when it is omitted.

    ``None`` is not "no version" in the sense of "all versions" -- it is the service's
    documented shorthand for "the latest one", and every route that takes this passes it
    through unchanged.
    """

    version: int | None = Field(None, ge=1)


class StatusHistoryQuery(Schema):
    """``version`` on the status-history endpoint, where omitting it means every version.

    This is the one place ``version=None`` does *not* mean "the latest": the service
    forwards it to the backend, which returns the whole key's history when the backend
    supports it.
    """

    version: int | None = Field(None, ge=1)


class TemplateListQuery(PaginationQuery):
    """Filters accepted by ``GET /api/v1/templates``.

    Every filter present is combined with AND, which is what a bare field filter means in
    the library's filter vocabulary.

    ``mostRecentActiveVersion`` is the one filter that is on unless a client turns it off,
    and the one whose default changes what a bare ``GET /templates`` returns: one row per
    key instead of one per version.

    ``orderByField`` and ``orderByDirection`` have no default: most backends can order by
    nothing, so a default would make the common listing a 400 against them. Omitted asks for
    the backend own order. An order the backend cannot apply is a 400 rather than a silent
    drop -- unlike a dropped filter, a dropped order leaves no trace in the rows.
    """

    key: str | None = None
    name: str | None = None
    description: str | None = None
    templateManagedBackend: str | None = None
    version: int | None = Field(None, ge=1)
    # Repeat the parameter to match several statuses at once (`?status=draft&status=active`),
    # which becomes an `in` lookup. A single value becomes an exact match.
    status: list[TemplateStatus] | None = Field(
        None,
        description=(
            "Applies on top of mostRecentActiveVersion, which defaults to true and keeps one "
            "row per key: its newest draft or active version. That row is never inactive or "
            "archived, so to find inactive or archived versions send "
            "mostRecentActiveVersion=false as well."
        ),
    )
    createdAtFrom: datetime.datetime | None = None
    createdAtTo: datetime.datetime | None = None
    updatedAtFrom: datetime.datetime | None = None
    updatedAtTo: datetime.datetime | None = None
    # Repeat the parameter to name several tags
    # (`?includesAllTags=billing&includesAllTags=urgent`).
    #
    # `includesAllTags` matches a template carrying *every* tag listed; `includesAnyOfTags`
    # one carrying *at least one*. Both accept a tag's slug or the text it came from -- the
    # library slugifies what it is given, so `Black Friday` and `black-friday` are the same
    # tag here. Sending both parameters combines them with AND, like every other filter pair.
    includesAllTags: list[str] | None = Field(None, max_length=MAX_TAGS_PER_REQUEST)
    includesAnyOfTags: list[str] | None = Field(None, max_length=MAX_TAGS_PER_REQUEST)
    # One row per key -- the highest-numbered `active` or `draft` version -- which is what a
    # list of templates almost always means. Send `false` to list every version instead; the
    # parameter then adds no constraint at all, rather than asking for the versions the
    # default hides. A key whose versions are all `inactive` or `archived` has no current
    # version, so the default listing does not show it.
    mostRecentActiveVersion: bool = True
    # Bases, or templates to send. Omitted means both, which is the listing a template
    # manager wants; `false` is what a picker choosing a template to *send* asks for, and
    # `true` what a picker choosing a base to extend asks for. Answered from the stored
    # `isAbstract` flag rather than by parsing every row -- see `capabilities.py`.
    isAbstract: bool | None = None
    # Declared last so the generated document lists them after the filters, and with no
    # default so an omitted parameter reaches ``build_order_by`` as "no order asked for"
    # rather than as a field most backends would have to refuse.
    orderByField: TemplateOrderByField | None = None
    # Only meaningful alongside a field. Sent on its own it is a 400, because ignoring it
    # looks exactly like a backend that cannot sort, which hides the client bug.
    orderByDirection: TemplateOrderDirection | None = None

    @field_validator("key", "name", "description", "templateManagedBackend")
    @classmethod
    def _non_empty_after_trim(cls, value: str | None) -> str | None:
        return _trimmed_or_none(value)

    @field_validator("includesAllTags", "includesAnyOfTags")
    @classmethod
    def _non_empty_tags(cls, value: list[str] | None) -> list[str] | None:
        return _clean_tags(value)


class CreateTemplateBody(Schema):
    """Body of ``POST /api/v1/templates``, which creates a template's first version."""

    key: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field("", max_length=2000)
    templateManagedBackend: str = Field(min_length=1, max_length=255)
    bodyTemplate: str = Field(min_length=1, max_length=MAX_TEMPLATE_LENGTH)
    subjectTemplate: str | None = Field(None, max_length=MAX_TEMPLATE_LENGTH)
    preheaderTemplate: str | None = Field(None, max_length=MAX_TEMPLATE_LENGTH)
    tenant: str | None = Field(None, max_length=255)
    # Tag *texts*, not slugs: a tag that does not exist yet is created, so a caller never has
    # to create tags before using them. Omitted or empty means an untagged template.
    tags: list[str] | None = Field(None, max_length=MAX_TAGS_PER_REQUEST)

    @field_validator("tags")
    @classmethod
    def _non_empty_tags(cls, value: list[str] | None) -> list[str] | None:
        return _clean_tags(value)


class CreateVersionBody(Schema):
    """Body of ``POST /api/v1/templates/{key}/versions``.

    Every field is optional and ``None`` means "carry this one forward": the backend copies
    the latest version and applies only the fields that are set. That is why there is no
    ``templateManagedBackend`` or ``tenant`` here -- ``ManagedTemplateUpdateInput`` carries
    neither, so neither can change across versions of one key.

    A body with nothing set is accepted and produces a new version identical to the latest,
    which is a legitimate way to branch a version off for a status change.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    bodyTemplate: str | None = Field(None, min_length=1, max_length=MAX_TEMPLATE_LENGTH)
    subjectTemplate: str | None = Field(None, max_length=MAX_TEMPLATE_LENGTH)
    preheaderTemplate: str | None = Field(None, max_length=MAX_TEMPLATE_LENGTH)
    # Unlike the other fields here, tags distinguish omitted from empty: omitting carries the
    # previous version's tags forward, `[]` creates the version with none.
    tags: list[str] | None = Field(None, max_length=MAX_TAGS_PER_REQUEST)

    @field_validator("tags")
    @classmethod
    def _tags_may_be_cleared(cls, value: list[str] | None) -> list[str] | None:
        """`[]` is allowed here -- it is how a new version drops every tag."""
        if value is not None and not value:
            return value
        return _clean_tags(value)


class SetTemplateTagsBody(Schema):
    """Body of ``PUT /api/v1/templates/{key}/tags``, which retags a version in place.

    An empty list is how a version's tags are cleared, so unlike the filters this one does
    not reject it.
    """

    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS_PER_REQUEST)
    version: int | None = Field(None, ge=1)

    @field_validator("tags")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return [tag.strip() for tag in value if tag and tag.strip()]


class TagListQuery(PaginationQuery):
    """Filters accepted by ``GET /api/v1/tags``."""

    # Repeat to ask for several (`?status=active&status=archived`). Omitted means every status.
    status: list[TagStatus] | None = None
    # A case-insensitive substring of the tag's text or slug.
    search: str | None = None
    tenant: str | None = None

    @field_validator("search", "tenant")
    @classmethod
    def _non_empty_after_trim(cls, value: str | None) -> str | None:
        return _trimmed_or_none(value)


class CreateTagBody(Schema):
    """Body of ``POST /api/v1/tags``.

    The slug is derived from ``text`` by the library and is not a client's to set: it is the
    tag's identity, and a client-supplied one could name a tag no other caller would produce.
    """

    text: str = Field(min_length=1, max_length=MAX_TAG_LENGTH)
    tenant: str | None = Field(None, max_length=255)


class UpdateTagBody(Schema):
    """Body of ``PATCH /api/v1/tags/{slug}``, which renames a tag.

    The slug is regenerated from the new text, so the tag's URL changes and a stored filter
    naming the old slug stops matching. The templates carrying the tag keep it.
    """

    text: str = Field(min_length=1, max_length=MAX_TAG_LENGTH)


class StatusChangeBody(Schema):
    """Body of the named lifecycle routes (activate / deactivate / archive)."""

    version: int | None = Field(None, ge=1)
    # Passed to the service untouched, `None` included: the service requires no
    # attribution on a status change and this API adds no policy of its own.
    changedBy: str | None = Field(None, max_length=255)


class SetStatusBody(StatusChangeBody):
    """Body of ``POST /api/v1/templates/{key}/status``, which names the target status."""

    status: TemplateStatus


class PreviewBody(Schema):
    """Body of ``POST /api/v1/templates/{key}/preview``.

    ``context`` is rendered verbatim. Nothing is generated: this API has no notification to
    resolve a registered context generator from, and a preview is meant to show what a
    given context produces.
    """

    context: dict = Field(default_factory=dict)
    version: int | None = Field(None, ge=1)
