from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

class ForcePasswordChangeMiddleware:
    """
    If a user must_change_password, redirect them to the password change page
    (except on allowed URLs like logout/change pages).
    """
    def __init__(self, get_response):
        self.get_response = get_response

    #: The phone app talks JSON. Redirecting it to an HTML password page gives
    #: it a reply it cannot read, so those paths answer in JSON instead.
    API_PREFIX = "/accounts/api/"

    def __call__(self, request):
        if request.user.is_authenticated and getattr(request.user, "must_change_password", False):
            if request.path.startswith(self.API_PREFIX):
                # Let sign-in and the account call through — they report the flag
                # so the app can explain what to do — and refuse the rest plainly.
                if not request.path.startswith("/accounts/api/m/auth/") \
                        and not request.path.startswith("/accounts/api/m/me/") \
                        and not request.path.startswith("/accounts/api/m/ping/"):
                    return JsonResponse({
                        "ok": False,
                        "error": "You need to change your password before using the app. "
                                 "Sign in to UN PASS in a browser to set a new one.",
                        "must_change_password": True,
                    }, status=403)
                return self.get_response(request)

            allowed = {
                reverse("password_change"),
                reverse("password_change_done"),
                # This app's URLs are namespaced (app_name = "accounts"), so the
                # bare name does not resolve — reversing it raised NoReverseMatch
                # and turned every page into a 500 for anyone holding this flag.
                reverse("accounts:logout"),
            }
            if not any(request.path.startswith(p) for p in allowed):
                return redirect("password_change")
        return self.get_response(request)
