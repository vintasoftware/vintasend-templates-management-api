"""Loads the operator-provided ``ManagedTemplateService`` and adapts it to what the API needs.

The API ships no template store of its own: which backend holds templates and which
renderer turns them into send input is a deployment decision.
``MANAGED_TEMPLATE_SERVICE_FACTORY`` names a callable that returns a configured
``ManagedTemplateService``.

Unlike ``vintasend-api``'s equivalent module, there is no sync/AsyncIO bridging here.
``ManagedTemplateService`` has no AsyncIO twin -- it composes two synchronous seams,
``BaseTemplateManagerBackend`` and ``ManagedTemplateRenderer`` -- so every call below is a
plain call and the view layer is synchronous throughout. If an AsyncIO twin ships later,
this module is the one place that has to learn about it.

What this class does absorb:

1. **Library exceptions become API errors.** Every route would otherwise repeat the same
   ``except ManagedTemplateNotFoundError`` dance.
2. **The transition table becomes data.** ``allowed_transitions`` asks the service the
   same question ``set_status`` asks, so a response can tell a UI which moves will work
   instead of leaving it to find out by catching a 409.
"""

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Literal

from django.conf import settings

from vintasend.services.helpers import _import_class
from vintasend.services.notification_template_renderers.base import NotificationSendInput
from vintasend_managed_templates.composition import TemplateReference
from vintasend_managed_templates.constants import ManagedTemplateStatus, ManagedTemplateTagStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateTag,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateChangeUserNotFoundError,
    ManagedTemplateCompositionError,
    ManagedTemplateDeletionNotAllowedError,
    ManagedTemplateError,
    ManagedTemplateInvalidFilterError,
    ManagedTemplateInvalidTagError,
    ManagedTemplateNotFoundError,
    ManagedTemplateStatusTransitionError,
    ManagedTemplateTagAlreadyExistsError,
    ManagedTemplateTagNotFoundError,
    ManagedTemplateUnsupportedOrderingError,
)
from vintasend_managed_templates.filters import ManagedTemplateFilter, ManagedTemplateOrderBy
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .errors import ApiError


if TYPE_CHECKING:
    from vintasend.services.dataclasses import (
        Notification,
        NotificationContextDict,
        OneOffNotification,
    )


logger = logging.getLogger(__name__)


class ServiceConfigurationError(RuntimeError):
    """Raised when ``MANAGED_TEMPLATE_SERVICE_FACTORY`` cannot be resolved into a service."""


def load_template_service(factory_path: str) -> ManagedTemplateService:
    """Import and call the configured factory, returning the service it builds."""
    if not factory_path:
        raise ServiceConfigurationError(
            "MANAGED_TEMPLATE_SERVICE_FACTORY is not set, so this API cannot build a "
            "managed-template service to read from. Point it at a callable that returns "
            "a configured ManagedTemplateService -- see vintasend_config.example.py."
        )

    try:
        factory = _import_class(factory_path)
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError) as error:
        raise ServiceConfigurationError(
            f"Could not import the managed-template service factory {factory_path!r}."
        ) from error

    if not callable(factory):
        raise ServiceConfigurationError(
            f"The managed-template service factory {factory_path!r} is not callable."
        )

    try:
        service = factory()
    except Exception as error:
        raise ServiceConfigurationError(
            f"Calling the managed-template service factory {factory_path!r} failed."
        ) from error

    if service is None:
        raise ServiceConfigurationError(
            f"The managed-template service factory {factory_path!r} did not return a service."
        )

    # The factory is operator-supplied and resolved by a dotted path, so its result is
    # untyped whatever the factory annotates. Checking it here is what stops that untyped
    # value spreading through every ServiceCaller method, and it turns "the factory
    # returned the wrong thing" into a startup failure naming what it got -- rather than an
    # AttributeError on the first request that reaches a method the object does not have.
    if not isinstance(service, ManagedTemplateService):
        raise ServiceConfigurationError(
            f"The managed-template service factory {factory_path!r} returned a "
            f"{type(service).__name__}, not a ManagedTemplateService."
        )

    return service


class ServiceCaller:
    """The slice of a ``ManagedTemplateService`` this API depends on, with the library's
    exceptions already translated into the contract's errors.

    Every method here except ``render_template`` raises ``ApiError`` and nothing else from
    the library's exception hierarchy, so a route never has to decide what a given failure
    means on the wire.
    """

    def __init__(self, service: ManagedTemplateService) -> None:
        self.service = service
        self._capabilities_cache: dict[str, bool] | None = None

    # --- capabilities --------------------------------------------------------------

    def get_capabilities(self) -> dict[str, bool]:
        """The backend's capability report, merged over the library default.

        Cached for the life of this caller -- which is the life of the process. A
        backend's capabilities are a static property of its implementation, so re-asking
        on every request would buy nothing.
        """
        if self._capabilities_cache is None:
            self._capabilities_cache = self.service.get_backend_supported_filter_capabilities()
        return self._capabilities_cache

    # --- reads ---------------------------------------------------------------------

    def get_template(self, template_key: str, version: int | None = None) -> ManagedTemplate:
        """One version of a template, or the latest when ``version`` is None."""
        with _not_found(template_key, version):
            return self.service.get_template(template_key, version)

    def get_template_versions(self, template_key: str) -> list[ManagedTemplate]:
        """Every version of a template, newest version first.

        The service implements this by filtering on the key, which matches nothing for an
        unknown key rather than raising -- so an empty list is how a missing key arrives
        here, and the route turns it into the 404 the contract documents.
        """
        with _invalid_filter():
            return self.service.get_template_versions(template_key)

    def get_paginated_filtered_templates(
        self,
        filters: ManagedTemplateFilter,
        page: int,
        page_size: int,
        order_by: ManagedTemplateOrderBy | None = None,
    ) -> list[ManagedTemplate]:
        """One page of the templates matching ``filters``, in ``order_by``'s order.

        Page numbers pass straight through: ``ManagedTemplateService`` validates
        ``page >= 1`` itself, so the wire's 1-indexing *is* the service's convention.
        There is no per-backend numbering to negotiate the way ``vintasend-api`` has to
        for notification backends, because no template call reaches the backend without
        going through that validation first.

        The route has already refused an order the capability report declines, so the
        service's own refusal here is the backstop for the two disagreeing -- which would
        otherwise surface as a 500 on a request that is not the client's fault.
        """
        with _invalid_filter(), _unsupported_ordering():
            return self.service.get_paginated_filtered_templates(filters, page, page_size, order_by)

    def get_status_history(
        self, template_key: str, version: int | None = None
    ) -> list[ManagedTemplateStatusHistory]:
        """The status audit trail, most recent change first."""
        with _not_found(template_key, version):
            return self.service.get_status_history(template_key, version)

    def allowed_transitions(self, template: ManagedTemplate) -> list[ManagedTemplateStatus]:
        """Which statuses ``template`` may move to right now, in a stable order.

        Asks the service rather than reading ``ALLOWED_STATUS_TRANSITIONS`` directly, so a
        subclass with its own lifecycle -- or one with ``validate_status_transitions``
        turned off, where every status is reachable -- is reported accurately.

        The version's current status is excluded even though ``can_transition_to`` returns
        True for it: setting a version to the status it already holds is a documented
        no-op, not a transition, and offering it as an action would be offering to do
        nothing.
        """
        return [
            status
            for status in ManagedTemplateStatus
            if status is not template.status and self.service.can_transition_to(template, status)
        ]

    # --- writes --------------------------------------------------------------------

    def create_template(self, data: ManagedTemplateCreateInput) -> ManagedTemplate:
        with _invalid_tag():
            return self.service.create_template(data)

    def update_template(
        self, template_key: str, data: ManagedTemplateUpdateInput
    ) -> ManagedTemplate:
        """Create a new version of an existing template from its latest one."""
        with _not_found(template_key, None), _invalid_tag():
            return self.service.update_template(template_key, data)

    def delete_template(self, template_key: str, version: int | None = None) -> None:
        """Delete one never-published version, reporting a published one as the contract's 409."""
        with _not_found(template_key, version), _deletion_not_allowed():
            self.service.delete_template(template_key, version)

    def set_status(
        self,
        template_key: str,
        status: ManagedTemplateStatus,
        version: int | None = None,
        changed_by: str | None = None,
    ) -> ManagedTemplate:
        """Move one version to ``status``, reporting a refused move as the contract's 409."""
        with _not_found(template_key, version):
            try:
                return self.service.set_status(template_key, status, version, changed_by)
            except ManagedTemplateStatusTransitionError as error:
                raise ApiError.invalid_transition(str(error)) from error
            except ManagedTemplateChangeUserNotFoundError as error:
                raise ApiError.bad_request(str(error)) from error

    # --- tags ----------------------------------------------------------------------

    def get_tags(
        self,
        status: list[ManagedTemplateTagStatus] | None = None,
        search: str | None = None,
        tenant: str | None = None,
    ) -> list[ManagedTemplateTag]:
        return self.service.get_tags(status, search, tenant)

    def get_tag(self, slug: str) -> ManagedTemplateTag:
        with _tag_not_found(slug):
            return self.service.get_tag(slug)

    def create_tag(self, text: str, tenant: str | None = None) -> ManagedTemplateTag:
        """Create a tag, reporting a duplicate as the contract's 409.

        A collision is a conflict rather than a validation error: the request was
        well-formed, and what it asked for is already there.
        """
        with _invalid_tag():
            try:
                return self.service.create_tag(text, tenant)
            except ManagedTemplateTagAlreadyExistsError as error:
                raise ApiError.conflict(str(error)) from error

    def update_tag(self, slug: str, text: str) -> ManagedTemplateTag:
        with _tag_not_found(slug), _invalid_tag():
            return self.service.update_tag(slug, text)

    def set_tag_status(self, slug: str, status: ManagedTemplateTagStatus) -> ManagedTemplateTag:
        with _tag_not_found(slug):
            return self.service.set_tag_status(slug, status)

    def delete_tag(self, slug: str) -> None:
        with _tag_not_found(slug):
            self.service.delete_tag(slug)

    def set_template_tags(
        self, template_key: str, tags: list[str], version: int | None = None
    ) -> ManagedTemplate:
        """Replace one version's tags in place -- no new version, no status change."""
        with _not_found(template_key, version), _invalid_tag():
            return self.service.set_template_tags(template_key, tags, version)

    # --- composition ---------------------------------------------------------------

    def compose_template(self, template: ManagedTemplate) -> ManagedTemplate:
        """A version already in hand, assembled the way the template engine will receive it.

        Takes the template rather than its key, so the caller's one read is the version that
        gets composed. Composing still reads the store, for whatever the template extends or
        includes.

        Composition failures are the template's, not the request's: a base that does not
        exist, a chain that loops, a malformed tag. They are reported as
        ``TEMPLATE_COMPOSITION_ERROR`` carrying the library's message, which names the chain
        it failed on -- the message is the point, since it is what makes the template
        fixable. A store that fails while composing is not translated, and reaches the
        unexpected-error handler as a 500.
        """
        with _not_found(template.key, template.version), _composition_error():
            return self.service.compose_template(template)

    def get_template_references(self, template: ManagedTemplate) -> list[TemplateReference]:
        """The templates this version directly extends or includes.

        Nothing is resolved, so a reference to a template that does not exist is reported
        rather than raising. A malformed tag still is -- there is no reference to report
        when the source cannot be parsed at all.
        """
        with _composition_error():
            return self.service.get_template_references(template)

    def is_abstract(self, template: ManagedTemplate) -> bool:
        """Whether a version is a base to build on, recomputed from its source.

        The authority behind the stored ``isAbstract`` flag on every template payload. Worth
        asking when the flag cannot be trusted -- a backend that predates it, or a template
        edited in memory since it was read.
        """
        with _composition_error():
            return self.service.is_abstract(template)

    # --- rendering -----------------------------------------------------------------

    def render_template(
        self,
        notification: "Notification | OneOffNotification",
        template: ManagedTemplate,
        context: "NotificationContextDict",
    ) -> NotificationSendInput:
        """Render a template already in hand, with no second backend read.

        The template is fetched by the caller so a preview can pin an explicit version --
        which is the point of previewing a draft that has not been activated.

        Unlike the methods above, this translates nothing: whatever the renderer raises is
        passed through, and ``build_template_preview`` decides what it means. Hand it a
        template from ``compose_template``; the library composes again before rendering,
        which for a composed template finds nothing to resolve and reads nothing.
        """
        return self.service.render_template(notification, template, context)


# --- exception translation ---------------------------------------------------------


class _translating(ABC):  # noqa: N801 - a context manager used as a statement, named like one
    """Base for the context managers that turn one library exception into one ApiError.

    Each subclass names the exception it translates and how to describe it. Everything
    else -- when to catch, when to stand aside, chaining the original as ``__cause__`` --
    is here, so a new translation is a class attribute and a one-line method rather than
    another copy of the same ``__exit__``.
    """

    #: The library exception this translates. Anything else propagates untouched.
    translates: ClassVar[type[ManagedTemplateError]]

    @abstractmethod
    def to_api_error(self, exc: ManagedTemplateError) -> ApiError:
        """Build the error to raise in place of ``exc``."""

    # Returns None rather than the instance: no call site uses `with ... as`, and handing
    # one back would suggest there is something on it worth reading.
    def __enter__(self) -> None:
        return None

    # `Literal[False]` rather than `bool`: this never swallows an exception -- it either
    # lets one through or replaces it with an ApiError. Typing it as `bool` would tell
    # mypy the `with` body may be exited by a suppressed exception, which makes every
    # caller that returns from inside one look like it has a path with no return.
    #
    # `isinstance` on the exception rather than `issubclass` on its type: it rules out the
    # no-exception case in the same check, and narrows `exc` for the call below.
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if not isinstance(exc, self.translates):
            return False
        raise self.to_api_error(exc) from exc


class _not_found(_translating):  # noqa: N801
    """Turn the library's "no such template" into the contract's 404.

    A context manager rather than a decorator so the message can name the key and version
    that were actually asked for, which is what makes a 404 body useful.

    ``ManagedTemplateNoActiveVersionError`` is a subclass and maps to 404 with it: for a send, a
    key with nothing published has nothing to show.
    """

    translates = ManagedTemplateNotFoundError

    def __init__(self, template_key: str, version: int | None) -> None:
        self.template_key = template_key
        self.version = version

    def to_api_error(self, exc: ManagedTemplateError) -> ApiError:
        return ApiError.not_found(describe_missing(self.template_key, self.version))


class _composition_error(_translating):  # noqa: N801
    """Turn a template that cannot be assembled into the contract's 409.

    Deliberately not a 500: composition runs before any template engine, so a failure here
    is a fact about the stored template -- exactly what the caller asked about -- and the
    library's message names the reference chain that broke.

    ``ManagedTemplateCompositionReferenceError`` is also a ``ManagedTemplateNotFoundError``,
    so this has to sit *inside* ``_not_found`` at every call site: a base that does not
    exist is a broken composition of a template that does, not a missing template.
    """

    translates = ManagedTemplateCompositionError

    def to_api_error(self, exc: ManagedTemplateError) -> ApiError:
        return ApiError.composition_error(str(exc))


class _deletion_not_allowed(_translating):  # noqa: N801
    """Turn a refused delete into the contract's 409 ``CONFLICT``.

    The version exists and the request was well formed; deleting it would erase what a pinned
    notification renders and who published it. The library's message says to archive instead.
    """

    translates = ManagedTemplateDeletionNotAllowedError

    def to_api_error(self, exc: ManagedTemplateError) -> ApiError:
        return ApiError.conflict(str(exc))


class _invalid_filter(_translating):  # noqa: N801
    """Turn a malformed or unknown-field filter into the contract's 400.

    ``ManagedTemplateService.validate_filter`` already produces a message naming the
    offending path and the known fields, so it is passed through as the error's message
    rather than replaced with something vaguer.
    """

    translates = ManagedTemplateInvalidFilterError

    def to_api_error(self, exc: ManagedTemplateError) -> ApiError:
        return ApiError.bad_request(str(exc))


class _unsupported_ordering(_translating):  # noqa: N801
    """Turn an order the backend cannot apply into the contract's 400.

    ``build_order_by`` reads the same capability report and refuses first, so reaching this
    means the report and the service disagreed. A 400 either way: the request named an order
    that cannot be served, which is the client's to fix, and the library's message already
    names the capability key to ask the report for.
    """

    translates = ManagedTemplateUnsupportedOrderingError

    def to_api_error(self, exc: ManagedTemplateError) -> ApiError:
        return ApiError.bad_request(str(exc))


class _tag_not_found(_translating):  # noqa: N801
    """Turn the library's "no such tag" into the contract's 404."""

    translates = ManagedTemplateTagNotFoundError

    def __init__(self, slug: str) -> None:
        self.slug = slug

    def to_api_error(self, exc: ManagedTemplateError) -> ApiError:
        return ApiError.not_found(f"No tag with slug '{self.slug}' was found.")


class _invalid_tag(_translating):  # noqa: N801
    """Turn unusable tag text into the contract's 400.

    Query validation rejects blank text, so what reaches here is text that is non-empty but
    has nothing sluggable in it -- ``"!!!"``, ``"---"`` -- which no length or presence check
    could have caught.
    """

    translates = ManagedTemplateInvalidTagError

    def to_api_error(self, exc: ManagedTemplateError) -> ApiError:
        return ApiError.bad_request(str(exc))


def describe_missing(template_key: str, version: int | None) -> str:
    if version is None:
        return f"No template with key '{template_key}' was found."
    return f"Template '{template_key}' has no version {version}."


def statuses_from(values: Iterable[str]) -> list[ManagedTemplateStatus]:
    """Parse wire status strings into the library's enum.

    Query validation has already restricted the values to the contract's literal, so an
    unknown one here would be a bug in this API rather than bad input.
    """
    return [ManagedTemplateStatus(value) for value in values]


# --- process-wide service ----------------------------------------------------------

_cache_lock = threading.Lock()
_cached_caller: ServiceCaller | None = None


def get_service_caller() -> ServiceCaller:
    """Build the service once per process and reuse it for every request.

    Failures are not cached: a transient misconfiguration (an unreachable database at
    boot, say) should be retried on the next request rather than poisoning the process.
    """
    global _cached_caller  # noqa: PLW0603

    if _cached_caller is not None:
        return _cached_caller

    with _cache_lock:
        if _cached_caller is not None:
            return _cached_caller

        service = load_template_service(settings.MANAGED_TEMPLATE_SERVICE_FACTORY)
        _cached_caller = ServiceCaller(service)
        return _cached_caller


def set_service_caller(caller: ServiceCaller | None) -> None:
    """Replace the cached service. The seam tests inject a fake service through."""
    global _cached_caller  # noqa: PLW0603

    with _cache_lock:
        _cached_caller = caller
