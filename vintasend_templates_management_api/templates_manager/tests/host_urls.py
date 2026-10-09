"""A host project's URLconf, for the embedding tests.

The app is mounted under a prefix next to a route of the host's own, the way a project that
embeds it would do. It is never the root URLconf outside the tests that select it with
``override_settings(ROOT_URLCONF=...)``.
"""

from django.http import HttpRequest, JsonResponse
from django.urls import include, path


def host_home(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"host": "home"})


urlpatterns = [
    path("", host_home),
    path("templates-api/", include("vintasend_templates_management_api.templates_manager.urls")),
]
