"""App configuration, including the startup checks that make a bad deployment fail fast."""

from django.apps import AppConfig
from django.core.checks import Error, register

from . import conf
from .hooks import configured_hook


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

    The shared key is only required when no ``VINTASEND_API_AUTHENTICATOR`` replaces it. An
    authenticator that cannot be used is reported as E005 and not as a missing key as well:
    one mistake, one error.
    """
    errors: list[Error] = []

    authenticator_configured = bool(conf.hook_setting(conf.AUTHENTICATOR))

    if not authenticator_configured and not conf.api_key():
        errors.append(
            Error(
                "VINTASEND_API_KEY is not set.",
                hint=(
                    "Every /api/v1 request must present this as a bearer token. Set it to "
                    "a long random string shared with the clients that call this API, or "
                    "set VINTASEND_API_AUTHENTICATOR to authenticate callers yourself."
                ),
                id="vintasend_templates_management_api.E001",
            )
        )

    if not conf.service_factory():
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

    for setting_name, check_id in (
        (conf.ACTOR_RESOLVER, "vintasend_templates_management_api.E003"),
        (conf.UNHANDLED_ERROR_HANDLER, "vintasend_templates_management_api.E004"),
        (conf.AUTHENTICATOR, "vintasend_templates_management_api.E005"),
    ):
        try:
            configured_hook(setting_name)
        except (ImportError, TypeError) as error:
            errors.append(
                Error(
                    f"{setting_name} cannot be used: {error}",
                    hint="Point it at an importable callable, or leave it empty.",
                    id=check_id,
                )
            )

    return errors
