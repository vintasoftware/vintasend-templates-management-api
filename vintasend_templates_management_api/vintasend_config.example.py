"""Example ManagedTemplateService factory.

Copy this file to ``vintasend_templates_management_api/vintasend_config.py`` (gitignored) and adapt it
to your own template-manager backend and renderer, then point the setting at it::

    MANAGED_TEMPLATE_SERVICE_FACTORY=vintasend_templates_management_api.vintasend_config.create_template_service

The only contract is: expose a callable that returns a configured ``ManagedTemplateService``.
The API calls it once per process and reuses the result, so it must be safe to call once and
the service it returns must be safe to share across requests.

``ManagedTemplateService`` composes two seams, and both are your choice:

* a ``BaseTemplateManagerBackend`` -- where template versions and their status history live;
* a ``ManagedTemplateRenderer`` -- how a stored template turns into rendered send input.
  Only the preview endpoint uses it, so a deployment that never previews can pass a renderer
  that raises.

The imports below are illustrative -- install the implementation packages your deployment
actually uses (``vintasend-django-templates-manager``, ``vintasend-jinja``, ...).
"""

from vintasend_managed_templates.managed_template_service import ManagedTemplateService


def create_template_service() -> ManagedTemplateService:
    raise NotImplementedError(
        "No managed-template service configured. Copy vintasend_config.example.py to "
        "vintasend_config.py, build your service there, and set "
        "MANAGED_TEMPLATE_SERVICE_FACTORY to point at it."
    )

    # A Django deployment, reading the templates your app already stores:
    #
    # from vintasend_managed_templates.managed_template_renderer import (
    #     ManagedTemplateEmailRenderer,
    # )
    # from vintasend_managed_templates.managed_template_service import ManagedTemplateService
    # from vintasend_django_templates_manager.backend import DjangoTemplateManagerBackend
    # from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (  # noqa: E501
    #     DjangoTemplatedEmailRenderer,
    # )
    #
    # backend = DjangoTemplateManagerBackend()
    #
    # # The managed renderer wraps an ordinary vintasend template renderer: the wrapped one
    # # decides how a template string is rendered (Django templates, Jinja, ...), and the
    # # managed one decides where the template string comes from.
    # renderer = ManagedTemplateEmailRenderer(backend, DjangoTemplatedEmailRenderer())
    #
    # return ManagedTemplateService(
    #     template_manager_backend=backend,
    #     template_renderer=renderer,
    # )

    # Hosts with a lifecycle of their own can turn the transition table off and let any
    # status move to any other, leaving the ordering entirely to the host:
    #
    # return ManagedTemplateService(
    #     template_manager_backend=backend,
    #     template_renderer=renderer,
    #     validate_status_transitions=False,
    # )
    #
    # `allowedTransitions` on every template payload reports whatever the service you build
    # here actually allows, so a UI reading it follows your lifecycle without being told.
