"""Converts ``vintasend_managed_templates`` dataclasses into the wire contract.

Dates always become ISO-8601 UTC strings, and absent dates are normalised to ``null``
(never omitted) so JSON responses are uniform.

Several field names differ between the Python dataclasses and the wire contract. The
mapping is small enough to keep in one place, which is here:

========================  ==========================================
wire                      ``ManagedTemplate``
========================  ==========================================
``createdAt``             ``created``
``updatedAt``             ``updated``
``bodyTemplate``          ``body_template``
``subjectTemplate``       ``subject_template``
``preheaderTemplate``     ``preheader_template``
``templateManagedBackend````template_managed_backend``
========================  ==========================================

On ``ManagedTemplateStatusHistory``, ``changedBy`` maps to ``created_by`` -- the record
stores it under one name and the service method that writes it takes the other. The wire
uses the name a caller sets.
"""

import datetime

from vintasend_managed_templates.composition import TemplateReference
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate,
    ManagedTemplateStatusHistory,
    ManagedTemplateTag,
)

from .contract import (
    ManagedTemplateOut,
    ManagedTemplateTagOut,
    TemplateCompositionOut,
    TemplateReferenceOut,
    TemplateSourceField,
    TemplateStatusHistoryOut,
)


def to_iso(value: datetime.datetime | None) -> str | None:
    """Render a timestamp as an ISO-8601 UTC string, or ``null`` when unset.

    Formatted with exactly three fractional digits and a ``Z`` suffix, which is what
    JavaScript's ``Date.prototype.toISOString`` produces, so a client can parse every
    timestamp this API emits the same way. Python's ``isoformat`` would give ``+00:00``
    and drop the fraction on a whole second.

    Naive datetimes are read as UTC rather than rejected: a backend that stores timestamps
    without a timezone would otherwise make every response unserialisable, and UTC is what
    these backends are expected to write.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    utc = value.astimezone(datetime.timezone.utc)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"


def serialize_tag(tag: ManagedTemplateTag) -> ManagedTemplateTagOut:
    """Serialize one tag. ``id`` is stringified like every other id on this wire, so a
    backend keying on an int, a str or a UUID all produce the same shape."""
    return ManagedTemplateTagOut(
        id=str(tag.id),
        text=tag.text,
        slug=tag.slug,
        status=tag.status.value,
        tenant=tag.tenant,
        createdAt=to_iso(tag.created),
        updatedAt=to_iso(tag.updated),
    )


def serialize_template(
    template: ManagedTemplate, allowed_transitions: list[ManagedTemplateStatus]
) -> ManagedTemplateOut:
    """Serialize one version of a template.

    ``allowed_transitions`` is passed in rather than computed here because answering it
    means asking the configured service, which serialization has no business reaching.
    """
    return ManagedTemplateOut(
        id=str(template.id),
        key=template.key,
        version=template.version,
        name=template.name,
        description=template.description,
        templateManagedBackend=template.template_managed_backend,
        bodyTemplate=template.body_template,
        subjectTemplate=template.subject_template,
        preheaderTemplate=template.preheader_template,
        status=template.status.value,
        tenant=template.tenant,
        createdAt=to_iso(template.created),
        updatedAt=to_iso(template.updated),
        tags=[serialize_tag(tag) for tag in template.tags],
        allowedTransitions=[status.value for status in allowed_transitions],
        # The backend's stored flag, read straight off the record: it is a denormalization
        # of the source that the backend maintains on every write, so serializing a listing
        # costs no parsing and cannot fail on a template with a malformed tag.
        isAbstract=template.is_abstract,
    )


def serialize_status_history(record: ManagedTemplateStatusHistory) -> TemplateStatusHistoryOut:
    return TemplateStatusHistoryOut(
        templateKey=record.template_key,
        version=record.version,
        status=record.status.value,
        changedBy=record.created_by,
        tenant=record.tenant,
        createdAt=to_iso(record.created),
    )


# The wire spelling of each source a composition reference can be written in. The library
# names them as the dataclass does; the wire names them as ``ManagedTemplateOut`` does.
# Typed as the wire literal rather than ``str`` so a field added to the library without a
# spelling here is a type error, not a response that fails validation at runtime.
FIELD_TO_WIRE: dict[str, TemplateSourceField] = {
    "body_template": "bodyTemplate",
    "subject_template": "subjectTemplate",
    "preheader_template": "preheaderTemplate",
}


def serialize_reference(reference: TemplateReference) -> TemplateReferenceOut:
    """Serialize one direct extends/include reference."""
    return TemplateReferenceOut(
        kind=reference.kind,
        key=reference.key,
        version=reference.version,
        field=FIELD_TO_WIRE[reference.field],
    )


def serialize_composition(
    template: ManagedTemplate,
    composed: ManagedTemplate,
    references: list[TemplateReference],
    is_abstract: bool,
) -> TemplateCompositionOut:
    """Serialize a version's assembled form.

    ``template`` supplies the identity and ``composed`` the assembled sources -- they are
    the same version, before and after composition, and the composer hands back the very
    same object when there was nothing to assemble.
    """
    return TemplateCompositionOut(
        key=template.key,
        version=template.version,
        isAbstract=is_abstract,
        references=[serialize_reference(reference) for reference in references],
        composedBodyTemplate=composed.body_template,
        composedSubjectTemplate=composed.subject_template,
        composedPreheaderTemplate=composed.preheader_template,
    )
