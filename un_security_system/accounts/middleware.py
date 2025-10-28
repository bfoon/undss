from django.shortcuts import redirect
from django.urls import reverse

class ForcePasswordChangeMiddleware:
    """
    If a user must_change_password, redirect them to the password change page
    (except on allowed URLs like logout/change pages).
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and getattr(request.user, "must_change_password", False):
            allowed = {
                reverse("password_change"),
                reverse("password_change_done"),
                reverse("logout"),
            }
            if not any(request.path.startswith(p) for p in allowed):
                return redirect("password_change")
        return self.get_response(request)
