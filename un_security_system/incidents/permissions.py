def can_user_manage_csr(user, csr) -> bool:
    if not user or not user.is_authenticated:
        return False

    # Agency scope
    if not getattr(user, "agency_id", None) or user.agency_id != csr.agency_id:
        return False

    # Superuser override
    if user.is_superuser:
        return True

    # Optional: role override for escalation teams
    if getattr(user, "role", "") in ("soc", "lsa", "ict_focal", "common_services_manager"):
        return True

    # Config level 1 manager override
    cfg = getattr(csr.agency, "common_service_config", None)
    if cfg and cfg.level_1_manager_id and user.id == cfg.level_1_manager_id:
        return True

    # Any configured approver can manage (or restrict to current_level only if you prefer)
    from .models import CommonServiceApprover
    return CommonServiceApprover.objects.filter(
        agency_id=csr.agency_id,
        user_id=user.id,
        is_active=True
    ).exists()

def is_common_services_manager(user) -> bool:
    return bool(user and user.is_authenticated and (user.is_superuser or getattr(user, "role", "") == "common_services_manager"))
