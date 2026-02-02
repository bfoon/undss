from .models import AgencyServiceConfig

def agency_service_flags(request):
    user = getattr(request, "user", None)
    enabled = False

    if user and user.is_authenticated:
        agency = getattr(user, "agency", None)
        if agency:
            svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
            enabled = bool(svc.asset_mgmt_enabled)

    return {
        "asset_mgmt_enabled": enabled
    }
