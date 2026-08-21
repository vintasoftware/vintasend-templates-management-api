"""ASGI entrypoint. Point uvicorn/daphne at ``vintasend_templates_management_api.asgi:application``.

``ManagedTemplateService`` is synchronous and has no AsyncIO twin, so this API's view
layer is synchronous throughout and running under ASGI buys nothing on its own. It is
here for deployments that already standardise on an ASGI server.
"""

import os

from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vintasend_templates_management_api.settings")

application = get_asgi_application()
