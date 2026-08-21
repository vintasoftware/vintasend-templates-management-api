"""Translates validated query parameters into a ``ManagedTemplateFilter``.

String filters are negotiated against the backend's advertised capabilities: a backend
that cannot do case-insensitive ``includes`` gets an exact match instead, and a field the
backend cannot filter on at all is dropped rather than failing the request. Dropping an
unsupported filter is the contract's choice; failing the request is not.

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
from typing import Any, cast

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.filters import (
    ManagedTemplateFilterFields,
    StringFieldFilter,
    StringFilterLookup,
)

from .capabilities import supports
from .query import TemplateListQuery


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


def _date_range(
    start: datetime.datetime | None, end: datetime.datetime | None
) -> dict[str, datetime.datetime]:
    date_range: dict[str, datetime.datetime] = {}
    if start is not None:
        date_range["from"] = start
    if end is not None:
        date_range["to"] = end
    return date_range


def build_status_filter(statuses: Sequence[str]) -> Any:
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
    backend_filter: dict[str, Any] = {}

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


def cast_filter(raw: dict[str, Any]) -> ManagedTemplateFilterFields:
    """Narrow a dict built key by key to the filter TypedDict.

    ``ManagedTemplateFilterFields`` is a ``total=False`` TypedDict, so it cannot be built
    incrementally under ``mypy`` without this one cast.
    """
    return cast("ManagedTemplateFilterFields", raw)
