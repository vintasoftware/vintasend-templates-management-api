"""Renders one version of a template against a caller-supplied context.

This is the endpoint's reason to exist. ``ManagedTemplateRenderer.render`` is the send path:
it resolves an unpinned key to the newest *active* version, so it never shows an unpublished
draft, and for a key with nothing published it may render a default the application
registered instead of anything stored. ``ManagedTemplateService.render_template`` takes the
template as an argument and never falls back, so fetching an explicit version first is what
makes previewing a draft -- before anyone activates it -- possible.

Two things this module has to supply that a real send would already have.

**A notification.** The renderer seam is defined in terms of a ``Notification``, and a
preview has none: nothing is being sent. So one is fabricated. Only its template fields
carry meaning, and even those are ignored by the call this makes --
``render_template`` drives ``create_template_content`` / ``render_from_template_content``
from the ``ManagedTemplate``, so the fabricated notification's own templates are never
looked up. It exists to satisfy the signature and to give a renderer that reads
non-template fields (attachments, adapter extras) something well-formed and empty to read.

**A context.** ``render_template`` takes a materialised context and generates none, and
this API has no notification to resolve a registered ``@register_context`` generator from.
So the caller's ``context`` is rendered verbatim -- which is also what a preview is for:
seeing what a given context produces.
"""

import datetime
import uuid
from typing import cast

from pydantic import JsonValue
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.dataclasses import Notification, NotificationContextDict
from vintasend.services.notification_template_renderers.base import NotificationSendInput
from vintasend_managed_templates.dataclasses import ManagedTemplate

from .contract import TemplatePreviewOut
from .errors import ApiError
from .service import ServiceCaller


# Marks the notification below as the fabrication it is, so a renderer that logs or
# annotates what it rendered does not report a preview as a real notification.
PREVIEW_CONTEXT_NAME = "vintasend_templates_management_api.preview"


def build_preview_notification(template: ManagedTemplate) -> Notification:
    """A well-formed, empty ``Notification`` standing in for the one a real send would have.

    ``id`` is a fresh UUID rather than a fixed sentinel so two concurrent previews are
    distinguishable in a renderer's logs. Its status is ``PENDING_SEND``: the notification
    was never sent, and claiming otherwise would be the one field a renderer might
    reasonably branch on.
    """
    return Notification(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        notification_type=NotificationTypes.EMAIL.value,
        title=template.name,
        body_template=template.key,
        context_name=PREVIEW_CONTEXT_NAME,
        context_kwargs={},
        send_after=None,
        subject_template=template.subject_template or "",
        preheader_template=template.preheader_template or "",
        status=NotificationStatus.PENDING_SEND.value,
        tenant=template.tenant,
        created=datetime.datetime.now(tz=datetime.timezone.utc),
    )


def build_template_preview(
    service: ServiceCaller, template: ManagedTemplate, context: dict[str, JsonValue]
) -> TemplatePreviewOut:
    """Render ``template`` with ``context`` and shape the result for the wire.

    Composed first, through the service caller, so the two failures a broken template can
    have come back under different codes: one that cannot be composed is a
    ``TEMPLATE_COMPOSITION_ERROR`` (409), exactly as ``GET /composition`` reports it. Composing
    reads the store, and a store failure there is not translated -- it is a 500, so a
    backend's message never reaches the client.

    A rendering failure is reported as ``PREVIEW_UNAVAILABLE`` (409) rather than a 500: a
    template that does not compile, or a context missing a variable the template needs, is
    a fact about the *template being previewed*, which is exactly what the caller asked to
    find out. Letting it fall through as an internal error would hide the message that
    makes the draft fixable. Only the render is caught, so nothing else is reported that way.
    """
    composed = service.compose_template(template)
    try:
        rendered = service.render_template(
            build_preview_notification(template),
            composed,
            _as_context(context),
        )
    except Exception as error:
        raise ApiError.preview_unavailable(
            f"Template '{template.key}' v{template.version} could not be rendered: {error}"
        ) from error

    body = getattr(rendered, "body", None)
    if not isinstance(body, str):
        # Every renderer in the library produces a `body` -- `TemplatedEmail` and
        # `TemplatedSMS` both do. A custom `NotificationSendInput` that does not has
        # nothing this endpoint can show, and saying so beats returning an empty preview
        # that looks like a template rendering to nothing.
        raise ApiError.preview_unavailable(
            "The configured template renderer produced a result with no text body, so "
            "there is nothing to preview."
        )

    return TemplatePreviewOut(
        key=template.key,
        version=template.version,
        renderedBody=body,
        # Absent on renderers that do not produce them: an SMS renderer has no subject,
        # and a preheader is a Python-only concept many renderers skip.
        renderedSubject=_optional_str(rendered, "subject"),
        renderedPreheader=_optional_str(rendered, "preheader"),
    )


def _as_context(context: dict[str, JsonValue]) -> NotificationContextDict:
    """Hand the request's JSON object to the renderer as the context, unchanged.

    The seam types a context as ``NotificationContextDict``, but building one here would
    reject most real contexts. That class validates on assignment, and its rule for a
    nested ``dict`` is that every value inside it must itself be a
    ``NotificationContextDict`` -- with no base case for a scalar. So
    ``{"user": {"name": "Ana"}}`` cannot be expressed at all: the inner ``"Ana"`` fails the
    check however the outer dict is built. Plain ``None`` values and lists of strings are
    refused for similar reasons, and both are ordinary things for a stored context to hold.

    ``NotificationContextDict`` is a ``dict`` subclass and renderers only ever read from a
    context, so a plain dict is what actually flows at runtime. ``vintasend-api`` passes one
    into ``render_email_template_from_content`` for the same reason: a notification's stored
    ``context_used`` is a plain dict, and a preview that refused what a real send carries
    would be a worse guide to production than no preview.

    The cast is where that reasoning is load-bearing -- it asserts a compatibility the type
    system cannot check, and it is confined to this one line.
    """
    return cast("NotificationContextDict", context)


def _optional_str(rendered: NotificationSendInput, attribute: str) -> str | None:
    """Read an optional rendered field off send input that may not define it.

    ``NotificationSendInput`` is a bare marker class -- ``TemplatedEmail`` carries a subject
    and preheader, ``TemplatedSMS`` carries neither -- so which attributes exist depends on
    the renderer the deployment configured. getattr is the honest way to ask.
    """
    value = getattr(rendered, attribute, None)
    return value if isinstance(value, str) else None
