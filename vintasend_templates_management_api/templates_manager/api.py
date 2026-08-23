"""Assembles the HTTP application: error envelope, auth, and the template routes.

Each handler maps HTTP input to a ``ManagedTemplateService`` call and the result back to
the wire contract -- no business logic beyond the translation itself. The lifecycle rules,
the version resolution and the filter validation all live in the service, which is the
point: this API is one more caller of it, not a second implementation of it.

``openapi.yaml`` is generated from the routes declared here by ``manage.py export_openapi``.
"""

import logging
from collections.abc import Sequence
from typing import TypeVar

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from ninja import NinjaAPI, Query, Schema, Status
from ninja.errors import AuthenticationError, ValidationError

from pydantic import JsonValue
from vintasend_managed_templates.constants import ManagedTemplateStatus, ManagedTemplateTagStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateTag,
    ManagedTemplateUpdateInput,
)

from .auth import ApiKeyAuth
from .contract import (
    API_VERSION,
    ApiErrorResponse,
    DataResponse,
    HealthOut,
    ListResponse,
    ManagedTemplateOut,
    ManagedTemplateTagOut,
    PaginatedResponse,
    TemplateCompositionOut,
    TemplatePreviewOut,
    TemplateStatusHistoryOut,
)
from .errors import STATUS_BY_CODE, ApiError
from .filters import build_backend_filter, build_order_by
from .preview import build_template_preview
from .query import (
    CreateTagBody,
    CreateTemplateBody,
    CreateVersionBody,
    PreviewBody,
    SetStatusBody,
    SetTemplateTagsBody,
    StatusChangeBody,
    StatusHistoryQuery,
    TagListQuery,
    TemplateListQuery,
    UpdateTagBody,
    VersionQuery,
)
from .serialize import (
    serialize_composition,
    serialize_status_history,
    serialize_tag,
    serialize_template,
)
from .service import ServiceCaller, describe_missing, get_service_caller


logger = logging.getLogger(__name__)

# The row type of a page sliced by `_paginate`, so a page comes back as a list of whatever
# went in rather than a list of `Any`.
RowT = TypeVar("RowT")

TemplatePage = PaginatedResponse[ManagedTemplateOut]
TagPage = PaginatedResponse[ManagedTemplateTagOut]

# Error responses are produced by the exception handlers below rather than returned from a
# view, so they are declared purely so the generated schema documents them. Each route
# declares the subset it can actually produce.
LIST_ERRORS: dict[int, type[Schema]] = {400: ApiErrorResponse, 401: ApiErrorResponse}
LOOKUP_ERRORS: dict[int, type[Schema]] = {401: ApiErrorResponse, 404: ApiErrorResponse}
WRITE_ERRORS: dict[int, type[Schema]] = {
    400: ApiErrorResponse,
    401: ApiErrorResponse,
    404: ApiErrorResponse,
}
# 409 covers both CONFLICT and INVALID_STATUS_TRANSITION; the body's `code` says which.
STATUS_ERRORS: dict[int, type[Schema]] = {**WRITE_ERRORS, 409: ApiErrorResponse}
PREVIEW_ERRORS: dict[int, type[Schema]] = {**WRITE_ERRORS, 409: ApiErrorResponse}
# 409 here is TEMPLATE_COMPOSITION_ERROR: the template exists and cannot be assembled.
COMPOSITION_ERRORS: dict[int, type[Schema]] = {**LOOKUP_ERRORS, 409: ApiErrorResponse}
# Creating a tag whose text already slugs onto an existing one is a CONFLICT.
TAG_CREATE_ERRORS: dict[int, type[Schema]] = {
    400: ApiErrorResponse,
    401: ApiErrorResponse,
    409: ApiErrorResponse,
}

# The status-change and preview bodies are optional, so an omitted one falls back to
# these. Shared rather than constructed per call because they are only ever read; every
# field is spelled out so a field added to either schema is a compile-time decision here
# rather than a silent default.
DEFAULT_STATUS_CHANGE_BODY = StatusChangeBody(version=None, changedBy=None)
DEFAULT_PREVIEW_BODY = PreviewBody(context={}, version=None)

api = NinjaAPI(
    title="VintaSend Managed Templates API",
    version="1.0.0",
    description=(
        "HTTP contract for managing VintaSend notification templates: their versions, "
        "their status lifecycle, and previewing a version before it is published."
    ),
    urls_namespace="vintasend_templates_management_api",
    auth=ApiKeyAuth(),
    docs_url="/docs",
)


# --- error envelope ------------------------------------------------------------------


def _envelope(
    request: HttpRequest, code: str, message: str, details: JsonValue | None = None
) -> HttpResponse:
    error: dict[str, JsonValue] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JsonResponse({"error": error}, status=STATUS_BY_CODE[code])


@api.exception_handler(ApiError)
def handle_api_error(request: HttpRequest, exc: ApiError) -> HttpResponse:
    return _envelope(request, exc.code, exc.message, exc.details)


@api.exception_handler(AuthenticationError)
def handle_authentication_error(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
    """Covers a missing or non-bearer ``Authorization`` header.

    A wrong key never reaches here -- ``ApiKeyAuth`` raises ``ApiError`` itself -- but a
    header Ninja cannot parse as a bearer token is rejected before the auth class runs.
    """
    return _envelope(request, "UNAUTHORIZED", "A valid API key is required.")


# Request sources pydantic puts at the front of a `loc`.
REQUEST_SOURCES = {"query", "body", "path", "form", "header", "cookie"}

# The names this API gives its container view arguments. Ninja labels a validation error
# with the argument name for request bodies and for list-valued query fields -- a bad
# `status` value arrives as `("query", "query", "status", 0)` while a bad `page` arrives as
# `("query", "page")` -- so the name has to be dropped for reported paths to be consistent.
#
# Safe to drop unconditionally only because no schema in `query.py` has a field called
# `payload` or `query`; a contract field with either name would need a rename, or this
# would start hiding a real path segment.
SYNTHETIC_ARGUMENT_NAMES = {"payload", "query"}


@api.exception_handler(ValidationError)
def handle_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    """Report invalid input as a 400 listing the offending fields.

    ``loc`` arrives as ``("query", "page")`` / ``("body", "payload", "bodyTemplate")``. The
    leading source segment and Ninja's synthetic argument name are dropped so the reported
    path is the field name the client actually sent.
    """
    # Annotated rather than inferred: pydantic types an error dict's values loosely, so
    # without this the list infers as `list[dict[str, Any]]` and the `Any` would ride into
    # the response body untyped.
    issues: list[JsonValue] = [
        {
            "path": ".".join(str(part) for part in _issue_path(issue.get("loc", ()))),
            "message": str(issue.get("msg", "")),
        }
        for issue in exc.errors
    ]
    return _envelope(request, "BAD_REQUEST", "Invalid request.", {"issues": issues})


def _issue_path(loc: Sequence[str | int]) -> list[str | int]:
    parts: list[str | int] = list(loc)
    if parts and parts[0] in REQUEST_SOURCES:
        parts = parts[1:]
    if parts and parts[0] in SYNTHETIC_ARGUMENT_NAMES:
        parts = parts[1:]
    return parts


@api.exception_handler(Http404)
def handle_not_found(request: HttpRequest, exc: Http404) -> HttpResponse:
    return _envelope(
        request,
        "NOT_FOUND",
        f"No route matches {request.method} {request.path}.",
    )


@api.exception_handler(Exception)
def handle_unexpected_error(request: HttpRequest, exc: Exception) -> HttpResponse:
    """Log unexpected errors in full but report them generically, so backend internals --
    connection strings, credentials in driver messages -- never leak to a client."""
    logger.exception("Unhandled error while handling %s %s", request.method, request.path)
    return _envelope(
        request,
        "INTERNAL_ERROR",
        "An unexpected error occurred while handling the request.",
    )


# --- helpers -------------------------------------------------------------------------


def _out(service: ServiceCaller, template: ManagedTemplate) -> ManagedTemplateOut:
    return serialize_template(template, service.allowed_transitions(template))


def _data(service: ServiceCaller, template: ManagedTemplate) -> DataResponse[ManagedTemplateOut]:
    return DataResponse[ManagedTemplateOut](data=_out(service, template))


def _set_tag_status(slug: str, status: ManagedTemplateTagStatus) -> ManagedTemplateTag:
    """Body shared by the archive and restore routes, which differ only in the target."""
    return get_service_caller().set_tag_status(slug, status)


def statuses_from_tags(values: Sequence[str]) -> list[ManagedTemplateTagStatus]:
    """Parse wire tag-status strings into the library's enum.

    Query validation has already restricted the values to the contract's literal, so an
    unknown one here would be a bug in this API rather than bad input.
    """
    return [ManagedTemplateTagStatus(value) for value in values]


def _paginate(rows: list[RowT], page: int, page_size: int) -> list[RowT]:
    """Slice a 1-indexed page in process.

    Unlike the template list -- which pushes paging down to the backend -- the tag seam has
    no paginated read, so the page is taken here. Sound in a way in-process *ordering* would
    not be: ``get_tags`` returns the whole set in one stable order, so a page is a slice of a
    complete list rather than a re-sort of an arbitrary window.
    """
    start = (page - 1) * page_size
    return rows[start : start + page_size]


def _change_status(
    key: str, status: ManagedTemplateStatus, payload: StatusChangeBody
) -> DataResponse[ManagedTemplateOut]:
    """Body shared by the four status routes, which differ only in how the target is chosen."""
    service = get_service_caller()
    template = service.set_status(key, status, payload.version, payload.changedBy)
    return _data(service, template)


# --- system --------------------------------------------------------------------------


@api.get(
    "/capabilities",
    response={200: DataResponse[dict[str, bool]], 401: ApiErrorResponse},
    tags=["system"],
)
def get_capabilities(request: HttpRequest) -> DataResponse[dict[str, bool]]:
    """Which filters and orders the configured template backend can honour.

    ``orderBy.*`` keys report which fields ``GET /templates`` can sort by. They default
    to false, so a backend that cannot sort reports nothing orderable and a client should
    offer no sortable columns. See ``capabilities.py``.
    """
    return DataResponse[dict[str, bool]](data=get_service_caller().get_capabilities())


# --- templates -----------------------------------------------------------------------


@api.get(
    "/templates",
    response={200: TemplatePage, **LIST_ERRORS},
    tags=["templates"],
)
def list_templates(request: HttpRequest, query: Query[TemplateListQuery]) -> TemplatePage:
    """List templates matching the filters -- one row per key by default.

    A row in the store is a *version*, so an unfiltered read of the seam returns a key once
    per version it has ever had. ``mostRecentActiveVersion`` defaults to ``true`` and asks
    the backend for the current version of each key instead: the highest-numbered ``active``
    or ``draft`` one. The collapsing happens in the store, not here, so a page is still a
    page of what the backend counted.

    Send ``mostRecentActiveVersion=false`` for the raw listing, every version included. A
    backend that cannot answer the filter has it dropped like any other unsupported one, and
    then returns every version too.
    """
    # Ordering is documented on ``TemplateListQuery`` rather than here, so the generated
    # operation description stays byte-identical to the TypeScript sibling's copy of
    # ``openapi.yaml``. The short version: neither ordering parameter has a default, because
    # every ``orderBy.*`` capability defaults to false and a default field would make this
    # listing a 400 against most backends. Unlike a filter, an order the backend cannot apply
    # is refused rather than dropped -- see ``filters.build_order_by``.
    service = get_service_caller()
    capabilities = service.get_capabilities()
    templates = service.get_paginated_filtered_templates(
        build_backend_filter(query, capabilities),
        query.page,
        query.pageSize,
        build_order_by(query, capabilities),
    )

    data = [_out(service, template) for template in templates]
    return TemplatePage(
        data=data,
        page=query.page,
        pageSize=query.pageSize,
        # True when the page came back full, meaning another page may exist. The seam has
        # no count method, so no total is available.
        hasMore=len(data) == query.pageSize,
    )


@api.post(
    "/templates",
    response={201: DataResponse[ManagedTemplateOut], 400: ApiErrorResponse, 401: ApiErrorResponse},
    tags=["templates"],
)
def create_template(request: HttpRequest, payload: CreateTemplateBody) -> Status:
    """Create a template's first version.

    Whether re-using an existing key is an error is the backend's call, not this API's --
    the seam does not define it, and a backend that treats a repeat ``create_template`` as
    a new version is behaving legitimately. Use ``POST /templates/{key}/versions`` when you
    mean "next version of this key".
    """
    service = get_service_caller()
    template = service.create_template(
        ManagedTemplateCreateInput(
            key=payload.key,
            name=payload.name,
            description=payload.description,
            template_managed_backend=payload.templateManagedBackend,
            template_body=payload.bodyTemplate,
            template_subject=payload.subjectTemplate,
            template_preheader=payload.preheaderTemplate,
            tenant=payload.tenant,
            tags=payload.tags,
        )
    )
    return Status(201, _data(service, template))


# The `/versions` routes are registered before `/templates/{key}` on purpose: routes match
# in registration order, and although the paths differ in segment count, keeping the more
# specific ones first makes the precedence explicit rather than incidental.


@api.get(
    "/templates/{key}/versions",
    response={200: ListResponse[ManagedTemplateOut], **LOOKUP_ERRORS},
    tags=["templates"],
)
def list_template_versions(request: HttpRequest, key: str) -> ListResponse[ManagedTemplateOut]:
    """Every version of one template, newest version first.

    The service resolves this by filtering on the key, which matches nothing for a key that
    does not exist rather than raising -- so an empty result is a missing key, and is
    reported as the 404 the contract documents rather than an empty list. A key that exists
    always has at least one version.
    """
    service = get_service_caller()
    versions = service.get_template_versions(key)

    if not versions:
        raise ApiError.not_found(describe_missing(key, None))

    return ListResponse[ManagedTemplateOut](data=[_out(service, template) for template in versions])


@api.post(
    "/templates/{key}/versions",
    response={201: DataResponse[ManagedTemplateOut], **WRITE_ERRORS},
    tags=["templates"],
)
def create_template_version(request: HttpRequest, key: str, payload: CreateVersionBody) -> Status:
    """Create a new version of an existing template, copied forward from its latest one.

    Templates are versioned rather than edited in place, which is why this is a POST that
    creates a resource and not a PATCH that mutates one: an already-published version is
    never modified. Fields left unset are carried over from the latest version.
    """
    service = get_service_caller()
    template = service.update_template(
        key,
        ManagedTemplateUpdateInput(
            name=payload.name,
            description=payload.description,
            template_body=payload.bodyTemplate,
            template_subject=payload.subjectTemplate,
            template_preheader=payload.preheaderTemplate,
            tags=payload.tags,
        ),
    )
    return Status(201, _data(service, template))


@api.get(
    "/templates/{key}/versions/{version}",
    response={200: DataResponse[ManagedTemplateOut], **LOOKUP_ERRORS},
    tags=["templates"],
)
def get_template_version(
    request: HttpRequest, key: str, version: int
) -> DataResponse[ManagedTemplateOut]:
    service = get_service_caller()
    return _data(service, service.get_template(key, version))


@api.delete(
    "/templates/{key}/versions/{version}",
    response={204: None, **LOOKUP_ERRORS},
    tags=["templates"],
)
def delete_template_version(request: HttpRequest, key: str, version: int) -> Status:
    service = get_service_caller()
    service.delete_template(key, version)
    return Status(204, None)


@api.get(
    "/templates/{key}/composition",
    response={200: DataResponse[TemplateCompositionOut], **COMPOSITION_ERRORS},
    tags=["templates"],
)
def get_template_composition(
    request: HttpRequest, key: str, query: Query[VersionQuery]
) -> DataResponse[TemplateCompositionOut]:
    """One version assembled the way the template engine will receive it.

    A managed template can build on another: ``{% managed_extends "base" %}`` to fill a
    base's ``{% managed_children %}`` hole and override its ``{% managed_block %}`` regions,
    ``{% managed_include "footer" %}`` to splice a fragment in. All of it is resolved before
    the engine runs, so the stored ``bodyTemplate`` is only half the template -- this is what
    actually renders.

    Nothing here is rendered against a context: engine syntax survives untouched. Use
    ``POST /templates/{key}/preview`` for the rendered result.

    Also reports what this version directly references, and whether it is abstract --
    recomputed from the source rather than read from the stored flag, so it is the authority
    behind the ``isAbstract`` on every template payload.

    A template that cannot be assembled -- a base that does not exist, a chain that loops, a
    malformed tag -- comes back as a 409 ``TEMPLATE_COMPOSITION_ERROR`` naming the chain that
    broke, for the same reason a template that will not render is a 409 rather than a 500.
    """
    service = get_service_caller()
    template = service.get_template(key, query.version)
    composed = service.get_composed_template(key, query.version)
    return DataResponse[TemplateCompositionOut](
        data=serialize_composition(
            template,
            composed,
            service.get_template_references(template),
            service.is_abstract(template),
        )
    )


@api.get(
    "/templates/{key}/status-history",
    response={200: ListResponse[TemplateStatusHistoryOut], **LOOKUP_ERRORS},
    tags=["statuses"],
)
def get_status_history(
    request: HttpRequest, key: str, query: Query[StatusHistoryQuery]
) -> ListResponse[TemplateStatusHistoryOut]:
    """The status audit trail, most recent change first.

    Omitting ``version`` asks for the whole key's history, which backends that keep it that
    way will return. Unlike everywhere else in this API, ``version=None`` here does not mean
    "the latest version".
    """
    service = get_service_caller()
    history = service.get_status_history(key, query.version)
    return ListResponse[TemplateStatusHistoryOut](
        data=[serialize_status_history(record) for record in history]
    )


@api.post(
    "/templates/{key}/status",
    response={200: DataResponse[ManagedTemplateOut], **STATUS_ERRORS},
    tags=["statuses"],
)
def set_template_status(
    request: HttpRequest, key: str, payload: SetStatusBody
) -> DataResponse[ManagedTemplateOut]:
    """Move one version to an explicitly named status.

    Setting a version to the status it already holds is a no-op the service reports as
    success: the version comes back unchanged and no audit entry is written, so a client
    retrying a request does not fill the trail with entries recording nothing.

    A move the lifecycle does not allow is a 409 with code ``INVALID_STATUS_TRANSITION``.
    ``allowedTransitions`` on every template payload says in advance which moves will work.
    """
    return _change_status(key, ManagedTemplateStatus(payload.status), payload)


@api.post(
    "/templates/{key}/activate",
    response={200: DataResponse[ManagedTemplateOut], **STATUS_ERRORS},
    tags=["statuses"],
)
def activate_template(
    request: HttpRequest, key: str, payload: StatusChangeBody = DEFAULT_STATUS_CHANGE_BODY
) -> DataResponse[ManagedTemplateOut]:
    """Publish one version.

    Other versions of the same key that are already active are left alone: a key may hold
    several active versions at once, and choosing between them is the host application's
    call, not this API's.
    """
    return _change_status(key, ManagedTemplateStatus.ACTIVE, payload)


@api.post(
    "/templates/{key}/deactivate",
    response={200: DataResponse[ManagedTemplateOut], **STATUS_ERRORS},
    tags=["statuses"],
)
def deactivate_template(
    request: HttpRequest, key: str, payload: StatusChangeBody = DEFAULT_STATUS_CHANGE_BODY
) -> DataResponse[ManagedTemplateOut]:
    """Retire one version without archiving it, so it can be activated again later."""
    return _change_status(key, ManagedTemplateStatus.INACTIVE, payload)


@api.post(
    "/templates/{key}/archive",
    response={200: DataResponse[ManagedTemplateOut], **STATUS_ERRORS},
    tags=["statuses"],
)
def archive_template(
    request: HttpRequest, key: str, payload: StatusChangeBody = DEFAULT_STATUS_CHANGE_BODY
) -> DataResponse[ManagedTemplateOut]:
    """Archive one version. Terminal under the default lifecycle: an archived version has
    no allowed transitions, and publishing a new version is the way forward from there."""
    return _change_status(key, ManagedTemplateStatus.ARCHIVED, payload)


@api.post(
    "/templates/{key}/preview",
    response={200: DataResponse[TemplatePreviewOut], **PREVIEW_ERRORS},
    tags=["templates"],
)
def preview_template(
    request: HttpRequest, key: str, payload: PreviewBody = DEFAULT_PREVIEW_BODY
) -> DataResponse[TemplatePreviewOut]:
    """Render a version against a supplied context, whatever its status.

    Pinning ``version`` is the point: it is what lets a draft be reviewed before anyone
    activates it. Omitting it previews the latest version.

    A template that fails to render comes back as a 409 ``PREVIEW_UNAVAILABLE`` carrying the
    renderer's message, because a broken template is what the caller asked to find out.
    """
    service = get_service_caller()
    template = service.get_template(key, payload.version)
    return DataResponse[TemplatePreviewOut](
        data=build_template_preview(service, template, payload.context)
    )


@api.put(
    "/templates/{key}/tags",
    response={200: DataResponse[ManagedTemplateOut], **WRITE_ERRORS},
    tags=["tags"],
)
def set_template_tags(
    request: HttpRequest, key: str, payload: SetTemplateTagsBody
) -> DataResponse[ManagedTemplateOut]:
    """Replace one version's tags, creating any tag that does not exist yet.

    A PUT that edits a version in place, unlike every other write on a template -- which
    creates a version instead. Tags describe how a template is *found*, not what it renders,
    so relabelling one should not fork a version and drop it back to draft. To change the
    tags *and* the content together, send them on ``POST /templates/{key}/versions``.

    An empty ``tags`` list clears the version's tags. Omitting ``version`` retags the latest.
    """
    service = get_service_caller()
    template = service.set_template_tags(key, payload.tags, payload.version)
    return _data(service, template)


# --- tags ----------------------------------------------------------------------------


@api.get(
    "/tags",
    response={200: TagPage, **LIST_ERRORS},
    tags=["tags"],
)
def list_tags(request: HttpRequest, query: Query[TagListQuery]) -> TagPage:
    """List tags, newest filter first: by status, by a text search, or by tenant.

    A tag picker wants ``?status=active``: archived tags are still attached to the templates
    carrying them and can still be filtered on, they are simply no longer offered.
    """
    service = get_service_caller()
    tags = service.get_tags(
        statuses_from_tags(query.status) if query.status else None,
        query.search,
        query.tenant,
    )

    page = _paginate(tags, query.page, query.pageSize)
    return TagPage(
        data=[serialize_tag(tag) for tag in page],
        page=query.page,
        pageSize=query.pageSize,
        hasMore=len(page) == query.pageSize,
    )


@api.post(
    "/tags",
    response={201: DataResponse[ManagedTemplateTagOut], **TAG_CREATE_ERRORS},
    tags=["tags"],
)
def create_tag(request: HttpRequest, payload: CreateTagBody) -> Status:
    """Define a tag ahead of any template using it.

    Tagging a template creates missing tags on its own, so this is for the case where a
    collision with an existing tag is worth hearing about -- it is a 409 here, where tagging
    a template would silently resolve to the tag already there.
    """
    service = get_service_caller()
    created = service.create_tag(payload.text, payload.tenant)
    return Status(201, DataResponse[ManagedTemplateTagOut](data=serialize_tag(created)))


@api.get(
    "/tags/{slug}",
    response={200: DataResponse[ManagedTemplateTagOut], **LOOKUP_ERRORS},
    tags=["tags"],
)
def get_tag(request: HttpRequest, slug: str) -> DataResponse[ManagedTemplateTagOut]:
    """One tag. The path accepts the tag's slug or the text it was created from."""
    return DataResponse[ManagedTemplateTagOut](
        data=serialize_tag(get_service_caller().get_tag(slug))
    )


@api.patch(
    "/tags/{slug}",
    response={200: DataResponse[ManagedTemplateTagOut], **WRITE_ERRORS},
    tags=["tags"],
)
def update_tag(
    request: HttpRequest, slug: str, payload: UpdateTagBody
) -> DataResponse[ManagedTemplateTagOut]:
    """Rename a tag. Its slug is regenerated, so its URL changes.

    The templates carrying the tag keep it, but a stored filter naming the old slug stops
    matching -- read ``slug`` off the response and store that. Renaming a tag onto another
    tag's text is allowed and yields a ``-2`` suffix: two tags may legitimately read the
    same, and the slug is what tells them apart.
    """
    return DataResponse[ManagedTemplateTagOut](
        data=serialize_tag(get_service_caller().update_tag(slug, payload.text))
    )


@api.delete(
    "/tags/{slug}",
    response={204: None, **LOOKUP_ERRORS},
    tags=["tags"],
)
def delete_tag(request: HttpRequest, slug: str) -> Status:
    """Delete a tag, removing the label from every template carrying it.

    Not reversible, and the templates lose the label. Archive instead when the tag should
    stop being offered but the templates should keep it.
    """
    get_service_caller().delete_tag(slug)
    return Status(204, None)


@api.post(
    "/tags/{slug}/archive",
    response={200: DataResponse[ManagedTemplateTagOut], **WRITE_ERRORS},
    tags=["tags"],
)
def archive_tag(request: HttpRequest, slug: str) -> DataResponse[ManagedTemplateTagOut]:
    """Retire a tag from the pickers without touching the templates carrying it.

    Filtering by an archived tag still returns those templates -- archiving hides the tag
    from ``?status=active``, it does not hide the templates.
    """
    return DataResponse[ManagedTemplateTagOut](
        data=serialize_tag(_set_tag_status(slug, ManagedTemplateTagStatus.ARCHIVED))
    )


@api.post(
    "/tags/{slug}/restore",
    response={200: DataResponse[ManagedTemplateTagOut], **WRITE_ERRORS},
    tags=["tags"],
)
def restore_tag(request: HttpRequest, slug: str) -> DataResponse[ManagedTemplateTagOut]:
    """Put an archived tag back on offer.

    Unlike an archived template version -- terminal, because reviving one would rewrite what
    its audit trail says happened -- a tag carries no history to contradict, so archiving one
    is reversible.
    """
    return DataResponse[ManagedTemplateTagOut](
        data=serialize_tag(_set_tag_status(slug, ManagedTemplateTagStatus.ACTIVE))
    )


# Registered last: `/templates/{key}` would otherwise be a candidate for paths the routes
# above own.


@api.get(
    "/templates/{key}",
    response={200: DataResponse[ManagedTemplateOut], **LOOKUP_ERRORS},
    tags=["templates"],
)
def get_template(
    request: HttpRequest, key: str, query: Query[VersionQuery]
) -> DataResponse[ManagedTemplateOut]:
    """One version of a template. Omitting ``version`` returns the latest."""
    service = get_service_caller()
    return _data(service, service.get_template(key, query.version))


@api.delete(
    "/templates/{key}",
    response={204: None, **LOOKUP_ERRORS},
    tags=["templates"],
)
def delete_template(request: HttpRequest, key: str, query: Query[VersionQuery]) -> Status:
    """Delete one version of a template, or its latest version when ``version`` is omitted.

    This deletes a *version*, never a whole key: the seam has no operation that removes
    every version at once, and doing it here as a loop would be a multi-step deletion with
    no transaction around it.
    """
    service = get_service_caller()
    service.delete_template(key, query.version)
    return Status(204, None)


# --- health --------------------------------------------------------------------------
#
# Unauthenticated and outside the versioned prefix: load balancers and container health
# checks have no API key.

health_api = NinjaAPI(
    version="health",
    urls_namespace="vintasend_templates_management_api_health",
    auth=None,
    docs_url=None,
)


@health_api.get("/health", response=HealthOut, tags=["system"])
def health(request: HttpRequest) -> dict[str, str]:
    return {"status": "ok", "apiVersion": API_VERSION}
