def access(request):
    """Nav flags: can this person open Access Studio, and how many requests wait for them."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    from .services import can

    out = {"access_can_view": can(user, "access.view")}
    if can(user, "access.approve_requests"):
        from .models import AccessRequest

        qs = AccessRequest.objects.filter(status="pending").exclude(user=user)
        if not user.is_superuser:
            qs = qs.filter(user__agency_id=user.agency_id)
        out["access_pending_requests"] = qs.count()
    return out
