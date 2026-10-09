"""Translates validated query parameters into a ``ManagedTemplateFilter``.

String filters are negotiated against the backend's advertised capabilities: a backend
that cannot do case-insensitive ``includes`` gets an exact match instead, and a field the
backend cannot filter on at all is dropped rather than failing the request. Dropping an
unsupported filter is the contract's choice; failing the request is not.

Ordering goes the other way and 400s -- see ``build_order_by`` for why the asymmetry is
deliberate rather than an oversight.

Two naming layers meet in this module and both are deliberate:

* The **wire** uses camelCase everywhere -- query parameters (``templateManagedBackend``)
  and capability keys (``fields.templateManagedBackend``).
* The **Python filter vocabulary** uses snake_case field names
  (``template_managed_backend``, ``created_at_range``) and snake_case string-lookup values
  (``starts_with``), because it is an in-process API that ``mypy`` checks and developers
  type by hand. See the module docstring of ``vintasend_managed_templates.filters``.

Everything a client sees is camelCase either way.
"""

import datetime
from collections.abc import Sequence
from typing import cast

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.filters import (
    DateRange,
    FilterLookup,
    ManagedTemplateFilterFields,
    ManagedTemplateOrderBy,
    ManagedTemplateStatusFilter,
    StringFieldFilter,
    StringFilterLookup,
)

from .capabilities import order_by_capability_key, supports
from .errors import ApiError, issue
from .query import TemplateListQuery


# Ascending is the SQL default, and the one a reader assumes when none is named.
DEFAULT_ORDER_BY_DIRECTION = "asc"

# Wire order-by field -> the Python filter vocabulary's field name. Only the two timestamps
# differ; the rest are spelled the same on both sides.
ORDER_BY_FIELD_TO_PYTHON: dict[str, str] = {
    "key": "key",
    "name": "name",
    "version": "version",
    "status": "status",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}


# Wire query parameter -> the capability key guarding it, and the Python filter field it
# becomes. Kept as one table so adding a filter cannot leave the capability check behind.
STRING_FIELDS: dict[str, tuple[str, str]] = {
    "key": ("fields.key", "key"),
    "name": ("fields.name", "name"),
    "description": ("fields.description", "description"),
    "templateManagedBackend": ("fields.templateManagedBackend", "template_managed_backend"),
}


# Wire query parameter -> capability key, and the Python filter field it becomes. Values pass
# through as the caller wrote them: the library slugifies filter values itself, so a tag may be
# named here by its slug or by the text behind it.
TAG_FIELDS: dict[str, tuple[str, str]] = {
    "includesAllTags": ("fields.includesAllTags", "includes_all_tags"),
    "includesAnyOfTags": ("fields.includesAnyOfTags", "includes_any_of_tags"),
}


def build_string_filter(value: str, capabilities: dict[str, bool]) -> StringFieldFilter:
    """Pick the most precise string lookup the backend supports.

    Case-insensitive ``includes`` when available, otherwise a case-insensitive exact
    match, otherwise a plain equality match (a bare string, which the filter vocabulary
    reads as a case-sensitive ``exact``).

    ``case_sensitive`` is always set rather than left off when the backend cannot fold
    case. All three keys of ``StringFilterLookup`` are required -- ``is_string_filter_lookup``
    returns False without them -- so an omitted one would produce a dict the library does
    not recognise as a lookup at all, rather than one that falls back to a default.
    """
    supports_includes = supports(capabilities, "stringLookups.includes")
    supports_case_insensitive = supports(capabilities, "stringLookups.caseInsensitive")

    if supports_includes:
        lookup: StringFilterLookup = {
            "lookup": "includes",
            "value": value,
            "case_sensitive": not supports_case_insensitive,
        }
        return lookup

    if supports_case_insensitive:
        return {"lookup": "exact", "value": value, "case_sensitive": False}

    return value


def _date_range(start: datetime.datetime | None, end: datetime.datetime | None) -> DateRange:
    # `DateRange` is `total=False`, so it can be started empty and filled in -- unlike the
    # filter TypedDict in `cast_filter`, this one needs no cast.
    date_range: DateRange = {}
    if start is not None:
        date_range["from"] = start
    if end is not None:
        date_range["to"] = end
    return date_range


def build_status_filter(statuses: Sequence[str]) -> ManagedTemplateStatusFilter:
    """One status becomes an exact match; several become an ``in`` lookup.

    A bare enum member is the filter vocabulary's exact match, so the single-status case
    needs no lookup wrapper.
    """
    members = [ManagedTemplateStatus(status) for status in statuses]
    if len(members) == 1:
        return members[0]
    return {"lookup": "in", "value": members}


def build_backend_filter(
    query: TemplateListQuery, capabilities: dict[str, bool]
) -> ManagedTemplateFilterFields:
    """Map the validated query onto the composable filter the backend evaluates.

    Filters are combined with AND, which is what a bare field filter means.
    """
    backend_filter: dict[str, FilterLookup] = {}

    for wire_name, (capability, field_name) in STRING_FIELDS.items():
        value = getattr(query, wire_name)
        if value and supports(capabilities, capability):
            backend_filter[field_name] = build_string_filter(value, capabilities)

    if query.version is not None and supports(capabilities, "fields.version"):
        backend_filter["version"] = query.version

    # Only ``True`` is ever sent. ``mostRecentActiveVersion=false`` is a client asking for
    # every version, which is the *absence* of this filter -- sending ``False`` would ask the
    # backend for the complement, the older and retired rows on their own, which is not what
    # switching a default off means.
    if query.mostRecentActiveVersion and supports(capabilities, "fields.mostRecentActiveVersion"):
        backend_filter["most_recent_active_version"] = True

    # ``is not None`` rather than truthiness: ``False`` is a filter here (the sendable
    # templates), not an absent parameter. Only ``None`` means "both".
    if query.isAbstract is not None and supports(capabilities, "fields.isAbstract"):
        backend_filter["is_abstract"] = query.isAbstract

    if query.status and supports(capabilities, "fields.status"):
        backend_filter["status"] = build_status_filter(query.status)

    if (query.createdAtFrom or query.createdAtTo) and supports(
        capabilities, "fields.createdAtRange"
    ):
        backend_filter["created_at_range"] = _date_range(query.createdAtFrom, query.createdAtTo)

    if (query.updatedAtFrom or query.updatedAtTo) and supports(
        capabilities, "fields.updatedAtRange"
    ):
        backend_filter["updated_at_range"] = _date_range(query.updatedAtFrom, query.updatedAtTo)

    for wire_name, (capability, field_name) in TAG_FIELDS.items():
        tags = getattr(query, wire_name)
        # `if tags` rather than `is not None`: query validation already rejects a list with
        # nothing usable in it, so an empty one here can only be an absent parameter. Passing
        # `[]` through would be a filter that matches everything or nothing depending on which
        # of the two it is -- a meaning no caller asked for.
        if tags and supports(capabilities, capability):
            backend_filter[field_name] = list(tags)

    return cast_filter(backend_filter)


def cast_filter(raw: dict[str, FilterLookup]) -> ManagedTemplateFilterFields:
    """Narrow a dict built key by key to the filter TypedDict.

    The one unavoidable cast in this module. ``ManagedTemplateFilterFields`` is a
    ``total=False`` TypedDict whose keys are decided by which query parameters were sent,
    and a TypedDict can only be built from a literal -- so a filter assembled conditionally
    cannot be typed as one without this. The input is ``dict[str, FilterLookup]`` rather
    than ``dict[str, Any]``, so every value going in is still checked against the union of
    lookup shapes the vocabulary allows; only the key-to-value pairing is asserted here.
    """
    return cast("ManagedTemplateFilterFields", raw)


def build_order_by(
    query: TemplateListQuery, capabilities: dict[str, bool]
) -> ManagedTemplateOrderBy | None:
    """Resolve ordering, refusing what the backend has said it cannot apply.

    This is the one negotiation on this endpoint that fails the request instead of quietly
    doing less, and the asymmetry with the filters above is the point rather than an
    inconsistency:

    * A dropped **filter** returns more rows than were asked for. The client can see that --
      the extra rows are right there in the response.
    * A dropped **order** returns exactly the rows that were asked for, in an arbitrary
      sequence. Nothing in the response says so. A client renders that page under a
      highlighted "sorted by name" column header and shows a sort that never happened.

    ``GET /capabilities`` publishes the ``orderBy.*`` keys, so a client builds its sortable
    columns from the report and never provokes this.

    param query: TemplateListQuery
    param capabilities: dict[str, bool] -- the merged report, not a backend's raw one.
    return: ManagedTemplateOrderBy | None -- None asks for the backend's own order.
    raises ApiError: 400 if the field is one this backend declares it cannot order by, or if
        a direction arrived with no field to apply it to.
    """
    if query.orderByField is None:
        # A direction on its own has nothing to order, and ignoring it looks exactly like a
        # backend that cannot sort -- which hides the client bug instead of reporting it.
        if query.orderByDirection is not None:
            message = (
                "orderByDirection was given without orderByField, so there is nothing to order by."
            )
            raise ApiError.bad_request(
                message,
                [issue("orderByDirection", message)],
                orderByDirection=query.orderByDirection,
            )
        return None

    field = ORDER_BY_FIELD_TO_PYTHON[query.orderByField]
    capability = order_by_capability_key(field)
    if not supports(capabilities, capability):
        message = (
            f"The configured template backend cannot order by '{query.orderByField}'. "
            f"GET /capabilities lists the fields it can order by."
        )
        raise ApiError.bad_request(
            message,
            [issue("orderByField", message)],
            orderByField=query.orderByField,
            capability=capability,
        )

    order_by: dict[str, str] = {
        "field": field,
        "direction": query.orderByDirection or DEFAULT_ORDER_BY_DIRECTION,
    }
    # Both values come from closed sets -- the field from the map above, the direction from
    # the contract's asc/desc literal -- so this narrowing is safe.
    return cast("ManagedTemplateOrderBy", order_by)
