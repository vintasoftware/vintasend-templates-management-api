"""What the configured template-manager backend can and cannot be asked for.

``vintasend``'s notification backends answer this themselves: ``BaseNotificationBackend``
declares ``get_filter_capabilities``, and ``NotificationService`` merges it over a library
default. ``BaseTemplateManagerBackend`` declares no such method, so this module supplies
the default map and reads a backend's report only if it happens to expose one. That keeps
the endpoint useful today without inventing a new abstract method on a published seam --
a backend that adds ``get_filter_capabilities`` later is picked up with no change here.

The map follows the same rule as the notification one: **a backend declares only what it
cannot do.** Its report is merged *over* an all-``True`` default, so a filter field added
in a later release does not force every backend to re-declare support for it, and a
missing key means "supported".

Keys are camelCase dotted and spelled the same way ``vintasend`` spells the equivalent
notification capability, so a client that already reads one map needs no translation to
read this one.
"""

from vintasend_managed_templates.base_template_manager_backend import (
    BaseTemplateManagerBackend,
)


# All capabilities default to True. See the module docstring for why.
DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES: dict[str, bool] = {
    # Composition. A backend that can evaluate field filters but not assemble them into
    # and/or/not groups declines these. ``notNested`` is the narrower question of whether
    # ``not`` may wrap a *group* rather than a single field filter.
    "logical.and": True,
    "logical.or": True,
    "logical.not": True,
    "logical.notNested": True,
    # One key per field of ``ManagedTemplateFilterFields``.
    "fields.name": True,
    "fields.description": True,
    "fields.key": True,
    "fields.version": True,
    "fields.templateManagedBackend": True,
    "fields.status": True,
    "fields.createdAtRange": True,
    "fields.updatedAtRange": True,
    # Tag membership. A backend that stores no tags -- or stores them but cannot query
    # across them -- declines these, and the list endpoint then drops the parameter rather
    # than failing the request. They are separate keys because "every tag" and "at least
    # one tag" are different queries: the first needs a per-template count over the tags
    # asked for, the second only needs membership.
    "fields.includesAllTags": True,
    "fields.includesAnyOfTags": True,
    # One row per key rather than one per version. Its own key because it is a different
    # question from the rest: a backend answers it by comparing a row against the other
    # versions of its key, which a store that keeps no version history cannot do. A backend
    # that declines it gets the unfiltered listing -- every version -- rather than an error.
    "fields.mostRecentActiveVersion": True,
    # Bases versus templates to send, answered from the stored ``is_abstract`` flag the
    # backend derives on every write. A backend written before composition existed keeps no
    # such flag and declines this; the list endpoint then drops the parameter rather than
    # failing, and a UI filters client-side or does without.
    "fields.isAbstract": True,
    "stringLookups.exact": True,
    "stringLookups.startsWith": True,
    "stringLookups.endsWith": True,
    "stringLookups.includes": True,
    # These two are independent capabilities, not a flag and its negation:
    #
    # * ``caseSensitive: False`` -- everything is forced case-insensitive, which is what a
    #   store on a case-insensitive collation (MySQL's ``*_ci``) does. It cannot honour
    #   ``case_sensitive: True``, nor a bare ``str`` filter, which means the same thing.
    # * ``caseInsensitive: False`` -- only exact-case matching is available, e.g. a store
    #   with ``LIKE`` but no ``ILIKE``. It cannot honour ``case_sensitive: False``.
    #
    # Deriving either from the other inverts the answer for exactly the backends that had
    # a constraint worth reporting, and would decline the one lookup they support. Read
    # the key you actually mean.
    "stringLookups.caseSensitive": True,
    "stringLookups.caseInsensitive": True,
}

# Ordering is absent from the map above, and that is the whole statement: there is no
# ``orderBy.*`` capability to negotiate because there is nothing to negotiate it against.
#
# ``BaseTemplateManagerBackend.get_paginated_filtered_templates`` takes ``filters``,
# ``page`` and ``page_size`` and no ordering argument, so an order can never reach the
# store. Sorting the page this API received would order rows *within* a page while the
# rows chosen *for* that page stayed in the backend's own order -- correct-looking on
# page 1 and wrong everywhere after it, with nothing raised. So the list endpoint accepts
# no ordering parameter at all.
#
# The two places an order is guaranteed are the ones ``ManagedTemplateService`` sorts
# itself over a complete result set: version listings (newest version first) and status
# history (most recent change first). Both are unpaginated for exactly that reason.
#
# ``vintasend_managed_templates.filters`` does define ``ManagedTemplateOrderBy``. It is
# unused by the seam; if a future release threads it through
# ``get_paginated_filtered_templates``, add ``orderBy.createdAt`` / ``orderBy.updatedAt``
# here and the query parameters in ``query.py``.
ORDERING_IS_UNSUPPORTED = True


def get_backend_capabilities(backend: BaseTemplateManagerBackend) -> dict[str, bool]:
    """Merge a backend's capability report over the library default.

    Backends are not required to have a report: ``BaseTemplateManagerBackend`` declares no
    such method, so one that says nothing is taken at the default -- fully capable -- which
    is the same reading a notification backend returning ``{}`` gets.

    Values are coerced to ``bool`` so a backend returning a truthy non-boolean cannot put a
    non-boolean into a response the contract types as ``boolean``.
    """
    reported: dict[str, object] = {}

    report = getattr(backend, "get_filter_capabilities", None)
    if callable(report):
        reported = dict(report() or {})

    return {
        **DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES,
        **{key: bool(value) for key, value in reported.items()},
    }


def supports(capabilities: dict[str, bool], key: str) -> bool:
    """Read one capability, defaulting to supported.

    A missing key means "supported": backends declare only what they *cannot* do, so a
    capability added in a later release does not force every backend to re-declare it.
    """
    return capabilities.get(key, True)
