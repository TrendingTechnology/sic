from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_http_methods
from django.contrib.sites.models import Site
from .models import User

from email.utils import parseaddr


@require_http_methods(["GET"])
def webfinger(request):
    print(request.GET)
    if "resource" in request.GET:
        resource = request.GET["resource"]
    else:
        return HttpResponseBadRequest("Bad request: no resource parameter specified")
    if not resource.startswith("acct:"):
        return HttpResponseBadRequest(
            "Bad request: resource doesn't start with `acct:`"
        )
    resource = resource[len("acct:") :].split("@")
    domain = Site.objects.get_current().domain
    if resource[1] != domain:
        return HttpResponseBadRequest(
            f"Bad request: {resource[1]} is an unknown domain"
        )
    username = resource[0]
    if username == "sic":
        return JsonResponse(
            {
                "subject": f"acct:sic@{domain}",
                "links": [
                    {
                        "rel": "self",
                        "type": "application/activity+json",
                        "href": "https://{domain}/activity-pub/id.json",
                    }
                ],
            }
        )
    try:
        user = User.objects.filter(banned_by_user=None, is_active=True).get(
            username=username
        )
    except User.DoesNotExist:
        raise Http404("User does not exist") from User.DoesNotExist

    return JsonResponse(
        {
            "subject": f"acct:{username}@{domain}",
            "links": [
                {
                    "rel": "self",
                    "href": f"http://{domain}{user.get_absolute_url()}",
                }
            ],
        }
    )
