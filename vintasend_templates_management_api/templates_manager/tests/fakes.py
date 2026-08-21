"""In-memory test doubles for the two seams a ``ManagedTemplateService`` composes.

The tests drive the *real* ``ManagedTemplateService`` over these, rather than faking the
service itself. That is deliberate: version resolution, the transition table and filter
validation are the service's behaviour, and this API's whole job is to expose it faithfully
-- a fake service would let a route drift from the thing it is supposed to be exposing and
the suite would never notice.

``InMemoryTemplateManagerBackend`` implements every abstract method of
``BaseTemplateManagerBackend``, so a method added to that seam breaks these tests at import
time, which is the reminder to decide what the API should do about it.
"""

import dataclasses
import datetime
import itertools
from collections.abc import Iterable
from typing import Any

from vintasend.services.notification_template_renderers.base import TemplateContent
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    EmailTemplateContent,
    TemplatedEmail,
)
from vintasend_managed_templates.base_template_manager_backend import (
    BaseTemplateManagerBackend,
)
from vintasend_managed_templates.composition import is_abstract
from vintasend_managed_templates.constants import (
    MOST_RECENT_ACTIVE_VERSION_STATUSES,
    ManagedTemplateStatus,
    ManagedTemplateTagStatus,
)
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateTag,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateCompositionError,
    ManagedTemplateInvalidTagError,
    ManagedTemplateNotFoundError,
    ManagedTemplateTagAlreadyExistsError,
    ManagedTemplateTagNotFoundError,
)
from vintasend_managed_templates.filters import (
    ManagedTemplateFilter,
    is_field_filter,
)
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateRenderer
from vintasend_managed_templates.tags import next_available_slug, slugify_tag


TEST_API_KEY = "test-api-key"

AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY}"}

FIXED_NOW = datetime.datetime(2024, 1, 15, 9, 0, tzinfo=datetime.timezone.utc)


class InMemoryTemplateManagerBackend(BaseTemplateManagerBackend):
    """A dict-backed template store with just enough filtering to exercise the API.

    Ordering is insertion order throughout, which is the honest thing for a backend with no
    ordering parameter to take -- see ``capabilities.py``.
    """

    # `capabilities` is typed loosely on purpose: one test reports a truthy non-boolean to
    # prove the API coerces it, which a `dict[str, bool]` would forbid at the call site.
    def __init__(self, capabilities: dict[str, Any] | None = None) -> None:
        self.templates: list[ManagedTemplate] = []
        self.history: list[ManagedTemplateStatusHistory] = []
        # slug -> tag. Templates hold references to these same objects, so a rename or a
        # status change is visible through every template carrying the tag.
        self.tags: dict[str, ManagedTemplateTag] = {}
        self._ids = itertools.count(1)
        # `BaseTemplateManagerBackend` declares no capability report, so the default this
        # suite exercises is a backend with no such attribute at all. One is grafted on
        # only when a test asks for it -- defining the method on the class and raising
        # inside it would not model absence, since `getattr` would still find it.
        if capabilities is not None:
            self.get_filter_capabilities = lambda: capabilities  # type: ignore[method-assign]
        # Records what the service actually asked for, so a test can assert on the call and
        # not only on the response.
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    # --- versions ------------------------------------------------------------------

    def create_template(self, data: ManagedTemplateCreateInput) -> ManagedTemplate:
        self.calls.append(("create_template", (data,)))
        existing = self._versions(data.key)
        template = ManagedTemplate(
            id=next(self._ids),
            name=data.name,
            description=data.description,
            key=data.key,
            template_managed_backend=data.template_managed_backend,
            body_template=data.template_body,
            subject_template=data.template_subject,
            preheader_template=data.template_preheader,
            version=len(existing) + 1,
            status=ManagedTemplateStatus.DRAFT,
            created=FIXED_NOW,
            updated=FIXED_NOW,
            tenant=data.tenant,
            tags=self.get_or_create_tags(data.tags or [], data.tenant),
        )
        template = _with_derived_flags(template)
        self.templates.append(template)
        return template

    def get_template(self, template_key: str, version: int | None = None) -> ManagedTemplate:
        self.calls.append(("get_template", (template_key, version)))
        versions = self._versions(template_key)
        if not versions:
            raise ManagedTemplateNotFoundError(f"No template with key {template_key!r}.")

        if version is None:
            return max(versions, key=lambda template: template.version)

        for template in versions:
            if template.version == version:
                return template
        raise ManagedTemplateNotFoundError(f"Template {template_key!r} has no version {version}.")

    def update_template(
        self, template_key: str, data: ManagedTemplateUpdateInput
    ) -> ManagedTemplate:
        self.calls.append(("update_template", (template_key, data)))
        latest = self.get_template(template_key)
        template = ManagedTemplate(
            id=next(self._ids),
            name=data.name if data.name is not None else latest.name,
            description=(data.description if data.description is not None else latest.description),
            key=latest.key,
            template_managed_backend=latest.template_managed_backend,
            body_template=(
                data.template_body if data.template_body is not None else latest.body_template
            ),
            subject_template=(
                data.template_subject
                if data.template_subject is not None
                else latest.subject_template
            ),
            preheader_template=(
                data.template_preheader
                if data.template_preheader is not None
                else latest.preheader_template
            ),
            version=latest.version + 1,
            status=ManagedTemplateStatus.DRAFT,
            created=FIXED_NOW,
            updated=FIXED_NOW,
            tenant=latest.tenant,
            # None carries the previous version's tags forward; [] clears them.
            tags=(
                list(latest.tags)
                if data.tags is None
                else self.get_or_create_tags(data.tags, latest.tenant)
            ),
        )
        self.templates.append(template)
        return template

    def delete_template(self, template_key: str, version: int | None = None) -> None:
        self.calls.append(("delete_template", (template_key, version)))
        target = self.get_template(template_key, version)
        self.templates.remove(target)

    # --- statuses ------------------------------------------------------------------

    def create_template_status_update(
        self,
        template_key: str,
        version: int,
        status: ManagedTemplateStatus,
        changed_by: str | None = None,
    ) -> None:
        self.calls.append(
            ("create_template_status_update", (template_key, version, status, changed_by))
        )
        target = self.get_template(template_key, version)
        target.status = status
        self.history.append(
            ManagedTemplateStatusHistory(
                template_key=template_key,
                version=version,
                status=status,
                created=FIXED_NOW + datetime.timedelta(seconds=len(self.history)),
                created_by=changed_by,
                tenant=target.tenant,
            )
        )

    def get_template_status_history(
        self, template_key: str, version: int | None = None
    ) -> Iterable[ManagedTemplateStatusHistory]:
        self.calls.append(("get_template_status_history", (template_key, version)))
        if not self._versions(template_key):
            raise ManagedTemplateNotFoundError(f"No template with key {template_key!r}.")
        return [
            record
            for record in self.history
            if record.template_key == template_key
            and (version is None or record.version == version)
        ]

    def get_templates_by_status(
        self, status: Iterable[ManagedTemplateStatus]
    ) -> Iterable[ManagedTemplate]:
        wanted = set(status)
        return [template for template in self.templates if template.status in wanted]

    # --- tags ----------------------------------------------------------------------

    def _slug_or_raise(self, text: str) -> str:
        slug = slugify_tag(text)
        if not slug:
            raise ManagedTemplateInvalidTagError(f"Tag text {text!r} cannot be slugified.")
        return slug

    def _find_tag(self, slug: str) -> ManagedTemplateTag:
        tag = self.tags.get(slugify_tag(slug))
        if tag is None:
            raise ManagedTemplateTagNotFoundError(f"Tag {slug!r} does not exist.")
        return tag

    def get_or_create_tags(
        self, texts: Iterable[str], tenant: str | None = None
    ) -> list[ManagedTemplateTag]:
        self.calls.append(("get_or_create_tags", (tuple(texts), tenant)))
        resolved: list[ManagedTemplateTag] = []
        for text in texts:
            slug = self._slug_or_raise(text)
            tag = self.tags.get(slug) or self._store_tag(text, slug, tenant)
            if tag not in resolved:
                resolved.append(tag)
        return resolved

    def _store_tag(self, text: str, slug: str, tenant: str | None) -> ManagedTemplateTag:
        tag = ManagedTemplateTag(
            id=next(self._ids),
            text=text,
            slug=slug,
            status=ManagedTemplateTagStatus.ACTIVE,
            created=FIXED_NOW,
            updated=FIXED_NOW,
            tenant=tenant,
        )
        self.tags[slug] = tag
        return tag

    def create_tag(self, text: str, tenant: str | None = None) -> ManagedTemplateTag:
        self.calls.append(("create_tag", (text, tenant)))
        slug = self._slug_or_raise(text)
        if slug in self.tags:
            raise ManagedTemplateTagAlreadyExistsError(f"Tag {slug!r} already exists.")
        return self._store_tag(text, slug, tenant)

    def get_tag(self, slug: str) -> ManagedTemplateTag:
        self.calls.append(("get_tag", (slug,)))
        return self._find_tag(slug)

    def update_tag(self, slug: str, text: str) -> ManagedTemplateTag:
        self.calls.append(("update_tag", (slug, text)))
        tag = self._find_tag(slug)
        new_slug = next_available_slug(
            self._slug_or_raise(text),
            lambda candidate: candidate in self.tags and candidate != tag.slug,
        )
        del self.tags[tag.slug]
        # Mutated rather than replaced, so the templates holding this object see the rename.
        tag.text = text
        tag.slug = new_slug
        self.tags[new_slug] = tag
        return tag

    def set_tag_status(self, slug: str, status: ManagedTemplateTagStatus) -> ManagedTemplateTag:
        self.calls.append(("set_tag_status", (slug, status)))
        tag = self._find_tag(slug)
        tag.status = status
        return tag

    def delete_tag(self, slug: str) -> None:
        self.calls.append(("delete_tag", (slug,)))
        tag = self._find_tag(slug)
        del self.tags[tag.slug]
        for template in self.templates:
            template.tags = [t for t in template.tags if t.slug != tag.slug]

    def get_tags(
        self,
        status: Iterable[ManagedTemplateTagStatus] | None = None,
        search: str | None = None,
        tenant: str | None = None,
    ) -> Iterable[ManagedTemplateTag]:
        self.calls.append(("get_tags", (status, search, tenant)))
        wanted = set(status) if status is not None else None
        needle = search.lower() if search else None
        return [
            tag
            for tag in self.tags.values()
            if (wanted is None or tag.status in wanted)
            and (tenant is None or tag.tenant == tenant)
            and (needle is None or needle in tag.text.lower() or needle in tag.slug.lower())
        ]

    def get_template_tags(
        self, template_key: str, version: int | None = None
    ) -> Iterable[ManagedTemplateTag]:
        self.calls.append(("get_template_tags", (template_key, version)))
        return list(self.get_template(template_key, version).tags)

    def set_template_tags(
        self, template_key: str, tags: Iterable[str], version: int | None = None
    ) -> ManagedTemplate:
        self.calls.append(("set_template_tags", (template_key, tuple(tags), version)))
        template = self.get_template(template_key, version)
        template.tags = self.get_or_create_tags(tags, template.tenant)
        return template

    # --- queries -------------------------------------------------------------------

    def get_all_templates(self) -> Iterable[ManagedTemplate]:
        return list(self.templates)

    def get_filtered_templates(self, filters: ManagedTemplateFilter) -> Iterable[ManagedTemplate]:
        self.calls.append(("get_filtered_templates", (filters,)))
        current = current_versions(self.templates)
        return [template for template in self.templates if matches(template, filters, current)]

    def get_paginated_templates(self, page: int, page_size: int) -> Iterable[ManagedTemplate]:
        return _page(list(self.templates), page, page_size)

    def get_paginated_filtered_templates(
        self, filters: ManagedTemplateFilter, page: int, page_size: int
    ) -> Iterable[ManagedTemplate]:
        self.calls.append(("get_paginated_filtered_templates", (filters, page, page_size)))
        return _page(list(self.get_filtered_templates(filters)), page, page_size)

    # --- helpers -------------------------------------------------------------------

    def _versions(self, template_key: str) -> list[ManagedTemplate]:
        return [template for template in self.templates if template.key == template_key]

    def add(self, **overrides: Any) -> ManagedTemplate:
        """Seed a template directly, bypassing version numbering."""
        defaults: dict[str, Any] = {
            "id": next(self._ids),
            "name": "Welcome email",
            "description": "Sent when an account is created.",
            "key": "welcome-email",
            "template_managed_backend": "django",
            "body_template": "<p>Hello {{ name }}</p>",
            "subject_template": "Welcome, {{ name }}",
            "preheader_template": None,
            "version": 1,
            "status": ManagedTemplateStatus.DRAFT,
            "created": FIXED_NOW,
            "updated": FIXED_NOW,
            "tenant": None,
            "tags": [],
        }
        defaults.update(overrides)
        template = ManagedTemplate(**defaults)
        if "is_abstract" not in overrides:
            template = _with_derived_flags(template)
        self.templates.append(template)
        return template


def _with_derived_flags(template: ManagedTemplate) -> ManagedTemplate:
    """Fill in the fields a backend derives rather than stores as it was given them.

    Just ``is_abstract`` so far. The storage seam asks every backend to derive it from the
    source on write, so a fake that skipped it would let the API's ``isAbstract`` filter and
    payload field pass their tests against a value nothing produced.
    """
    try:
        derived = is_abstract(template)
    except ManagedTemplateCompositionError:
        # A template whose tags are malformed has no answer, and a write is not the place to
        # report a syntax error -- the flag is a search convenience, and a template nobody
        # can parse cannot be extended either. Reads as concrete, which is the rule every
        # backend in this project follows.
        derived = False
    return dataclasses.replace(template, is_abstract=derived)


def _page(templates: list[ManagedTemplate], page: int, page_size: int) -> list[ManagedTemplate]:
    """Slice a 1-indexed page, which is what every backend in this library does."""
    start = (page - 1) * page_size
    return templates[start : start + page_size]


# --- filter evaluation ---------------------------------------------------------------


def current_versions(templates: list[ManagedTemplate]) -> set[tuple[str, int]]:
    """``(key, version)`` of the highest ACTIVE-or-DRAFT version of each key.

    Computed over the whole store because ``most_recent_active_version`` is the one filter
    field that is about the key rather than the row -- see the library's filter vocabulary.
    """
    highest: dict[str, int] = {}
    for template in templates:
        if template.status not in MOST_RECENT_ACTIVE_VERSION_STATUSES:
            continue
        if template.version > highest.get(template.key, 0):
            highest[template.key] = template.version
    return set(highest.items())


def matches(
    template: ManagedTemplate, filters: Any, current: set[tuple[str, int]] | None = None
) -> bool:
    """Evaluate a ``ManagedTemplateFilter`` against one template.

    Covers the subset this API can build -- field filters combined with AND, plus the
    logical groups a hand-written filter could carry -- which is enough to prove the query
    translation in ``filters.py`` produces something a backend evaluates as intended.

    ``current`` is the ``(key, version)`` set ``current_versions`` builds, threaded through
    because no single template can answer ``most_recent_active_version`` on its own.
    """
    if not is_field_filter(filters):
        if "and" in filters:
            return all(matches(template, sub, current) for sub in filters["and"])
        if "or" in filters:
            return any(matches(template, sub, current) for sub in filters["or"])
        return not matches(template, filters["not"], current)

    return all(
        _one_field_matches(template, field, lookup, current) for field, lookup in filters.items()
    )


def _one_field_matches(
    template: ManagedTemplate, field: str, lookup: Any, current: set[tuple[str, int]] | None
) -> bool:
    if field == "is_abstract":
        # The stored flag, not a fresh parse -- querying the denormalization is the point of
        # having one, and a store whose flag has drifted should show that here.
        return template.is_abstract is bool(lookup)
    if field == "most_recent_active_version":
        is_current = (template.key, template.version) in (current or set())
        return is_current is bool(lookup)
    if field in ("includes_all_tags", "includes_any_of_tags"):
        # Slugified so a filter may name a tag by its text, which is what the library does.
        wanted = {slugify_tag(tag) for tag in lookup}
        carried = {tag.slug for tag in template.tags}
        return wanted <= carried if field == "includes_all_tags" else bool(wanted & carried)
    return _field_matches(getattr(template, _attribute(field)), lookup)


# `created_at_range` / `updated_at_range` filter on fields the dataclass spells `created`
# and `updated`.
_FIELD_ATTRIBUTES = {"created_at_range": "created", "updated_at_range": "updated"}


def _attribute(field: str) -> str:
    return _FIELD_ATTRIBUTES.get(field, field)


def _field_matches(value: Any, lookup: Any) -> bool:
    if isinstance(lookup, ManagedTemplateStatus):
        return value is lookup

    if isinstance(lookup, dict):
        if "from" in lookup or "to" in lookup:
            return (lookup.get("from") is None or value >= lookup["from"]) and (
                lookup.get("to") is None or value <= lookup["to"]
            )
        return _lookup_matches(value, lookup)

    return bool(value == lookup)


def _lookup_matches(value: Any, lookup: dict[str, Any]) -> bool:
    wanted = lookup["value"]
    operator = lookup["lookup"]

    if operator == "in":
        return value in wanted

    if isinstance(value, str) and isinstance(wanted, str):
        if lookup.get("case_sensitive") is False:
            value, wanted = value.lower(), wanted.lower()
        if operator == "includes":
            return wanted in value
        if operator == "starts_with":
            return value.startswith(wanted)
        if operator == "ends_with":
            return value.endswith(wanted)

    if operator == "gt":
        return bool(value > wanted)
    if operator == "gte":
        return bool(value >= wanted)
    if operator == "lt":
        return bool(value < wanted)
    if operator == "lte":
        return bool(value <= wanted)

    return bool(value == wanted)


# --- renderers -----------------------------------------------------------------------


class FakeEmailRenderer(ManagedTemplateRenderer[EmailTemplateContent]):
    """Renders by substituting ``{{ key }}`` placeholders, so a preview is assertable.

    Real renderers (Django templates, Jinja) are what a deployment configures; this one
    exists to prove the API drives the seam correctly, not to reimplement templating.
    """

    def __init__(self, raises: Exception | None = None) -> None:
        # Deliberately does not call super().__init__: the API never touches the wrapped
        # renderer or the backend on this object, and leaving them unset proves it.
        self.raises = raises

    def create_template_content(self, template: ManagedTemplate) -> EmailTemplateContent:
        return EmailTemplateContent(
            subject_template=template.subject_template or "",
            body_template=template.body_template,
            preheader_template=template.preheader_template,
        )

    def render_from_template_content(
        self,
        notification: Any,
        template_content: EmailTemplateContent,
        context: Any,
        **kwargs: Any,
    ) -> TemplatedEmail:
        if self.raises is not None:
            raise self.raises
        return TemplatedEmail(
            subject=_substitute(template_content.subject_template, context),
            body=_substitute(template_content.body_template, context),
            preheader=(
                _substitute(template_content.preheader_template, context)
                if template_content.preheader_template
                else None
            ),
        )


class FakeSMSRenderer(ManagedTemplateRenderer[TemplateContent]):
    """A body-only renderer, standing in for the SMS side of the seam."""

    def __init__(self) -> None:
        pass

    def create_template_content(self, template: ManagedTemplate) -> TemplateContent:
        return TemplateContent(body_template=template.body_template)

    def render_from_template_content(
        self, notification: Any, template_content: TemplateContent, context: Any, **kwargs: Any
    ) -> Any:
        return _BodyOnly(_substitute(template_content.body_template, context))


class _BodyOnly:
    def __init__(self, body: str) -> None:
        self.body = body


class BodylessRenderer(ManagedTemplateRenderer[TemplateContent]):
    """Produces send input with no text body, which the preview endpoint cannot show."""

    def __init__(self) -> None:
        pass

    def create_template_content(self, template: ManagedTemplate) -> TemplateContent:
        return TemplateContent(body_template=template.body_template)

    def render_from_template_content(
        self, notification: Any, template_content: TemplateContent, context: Any, **kwargs: Any
    ) -> Any:
        return object()


def _substitute(template: str, context: Any) -> str:
    rendered = template
    for key, value in dict(context).items():
        rendered = rendered.replace("{{ " + str(key) + " }}", str(value))
    return rendered
