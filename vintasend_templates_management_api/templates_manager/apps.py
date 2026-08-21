"""App configuration, including the startup checks that make a bad deployment fail fast."""

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register


class TemplatesManagerConfig(AppConfig):
    name = "vintasend_templates_management_api.templates_manager"
    label = "vintasend_templates_manager"
    verbose_name = "VintaSend Managed Templates API"


@register()
def check_api_configuration(app_configs: object, **kwargs: object) -> list[Error]:
    """Refuse to start without the settings every request depends on.

    Registered as a Django system check rather than raised at import time so ``manage.py``
    stays usable and the message arrives as a readable checklist. Both ``runserver`` and
    ``manage.py check`` run these; ``gunicorn`` deployments should run ``manage.py check``
    in their release step to get the same guarantee.
    """
    errors: list[Error] = []

    if not settings.VINTASEND_API_KEY:
        errors.append(
            Error(
                "VINTASEND_API_KEY is not set.",
                hint=(
                    "Every /api/v1 request must present this as a bearer token. Set it to "
                    "a long random string shared with the clients that call this API."
                ),
                id="vintasend_templates_management_api.E001",
            )
        )

    if not settings.MANAGED_TEMPLATE_SERVICE_FACTORY:
        errors.append(
            Error(
                "MANAGED_TEMPLATE_SERVICE_FACTORY is not set.",
                hint=(
                    "Point it at a callable returning a configured ManagedTemplateService, "
                    "for example "
                    "'vintasend_templates_management_api.vintasend_config.create_template_service'. "
                    "Start from vintasend_config.example.py."
                ),
                id="vintasend_templates_management_api.E002",
            )
        )

    return errors
