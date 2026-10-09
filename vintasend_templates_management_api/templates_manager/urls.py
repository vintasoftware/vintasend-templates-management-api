"""The app's URLconf: ``health`` and ``api/v1/``, relative to wherever it is included.

A host mounts it under a prefix of its choosing::

    path("templates-api/", include("vintasend_templates_management_api.templates_manager.urls"))

which serves ``/templates-api/health`` and ``/templates-api/api/v1/...``. The bundled project
includes it at the root. ``health`` is unauthenticated, for load balancers and container
health checks; everything under ``api/v1/`` is behind ``ApiKeyAuth``.

Include it once per project. Ninja registers each API's URL namespace the first time its
patterns are built and refuses a second, so this module is the one place that builds them.
"""

from django.urls import path

from .api import api, health_api


urlpatterns = [
    path("", health_api.urls),
    path("api/v1/", api.urls),
]
