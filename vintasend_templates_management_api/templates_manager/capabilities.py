"""What the configured template-manager backend can and cannot be asked for.

The vocabulary itself lives in ``vintasend_managed_templates.filters``, next to the filters
it describes, and ``ManagedTemplateService`` does the merging. This module is the thin API-side
view of it: one function to read a single key, and the wire names for the ordering vocabulary.

The rule is the library's: **a backend declares only what it cannot do.** Its report is merged
*over* ``DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES``, so a filter field added in a later
release does not force every backend to re-declare support for it, and a missing key means
"supported".

The ``orderBy.*`` keys are the one exception, and they default to **False**. Ordering is newer
vocabulary than the filters, so a ``True`` default would have every backend that shipped before
it existed claim an order it silently ignores. A backend that can sort says so.

That asymmetry is what makes the list endpoint's two negotiations differ:

* an unsupported **filter** is dropped and the request succeeds, because the caller sees the
  extra rows it gets back;
* an unsupported **order** is a ``400``, because an ignored order returns exactly the rows that
  were asked for in an arbitrary sequence and nothing downstream can tell.

Keys are camelCase dotted and spelled the same way ``vintasend`` spells the equivalent
notification capability, so a client that already reads one map needs no translation.
"""

from vintasend_managed_templates.filters import (
    DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES,
    MANAGED_TEMPLATE_ORDER_BY_FIELDS,
    order_by_capability_key,
    supports_capability,
)


__all__ = [
    "DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES",
    "MANAGED_TEMPLATE_ORDER_BY_FIELDS",
    "order_by_capability_key",
    "supports",
]


def supports(capabilities: dict[str, bool], key: str) -> bool:
    """Read one capability, defaulting to supported.

    A missing key means "supported": backends declare only what they *cannot* do, so a
    capability added in a later release does not force every backend to re-declare it. Pass
    the *merged* report -- ``ServiceCaller.get_capabilities()`` -- rather than a backend's raw
    one, or the ``orderBy.*`` keys, which are absent from most reports and default to False in
    the merge, would read here as supported.
    """
    return supports_capability(capabilities, key)
