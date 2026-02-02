def get_manager_emails_for_request(req) -> list[str]:
    """
    Unit Head + Unit Asset Managers OR Operations Manager (for core/unallocated).
    """
    emails = []

    # core/unallocated -> ops manager
    manager = req.get_required_manager()
    if manager and getattr(manager, "email", None):
        emails.append(manager.email)

    # if unit exists, include unit asset managers as backup
    if req.unit:
        for u in req.unit.asset_managers.all():
            if u.email:
                emails.append(u.email)

    # unique
    return sorted(set(emails))


def get_ict_custodian_emails(req=None, agency=None) -> list[str]:
    """
    Agency ICT custodians + ict_focal fallback.
    Accepts either:
      - req (must have .agency)
      - agency (direct)
    """
    emails = []

    # Resolve agency safely
    if agency is None and req is not None:
        agency = getattr(req, "agency", None)

    if agency is None:
        return []

    roles = getattr(agency, "asset_roles", None)

    if roles:
        for u in roles.ict_custodian.all():
            if u.email:
                emails.append(u.email)

    # fallback: if you have a known "ict_focal" role users in your User model
    try:
        ict_users = agency.users.filter(role="ict_focal")
        for u in ict_users:
            if u.email:
                emails.append(u.email)
    except Exception:
        pass

    return sorted(set(emails))


def get_manager_emails_for_asset(asset):
    emails = set()

    # core/unallocated -> ops manager
    if asset.unit is None or getattr(asset.unit, "is_core_unit", False):
        roles = getattr(asset.agency, "asset_roles", None)
        if roles and roles.operations_manager and roles.operations_manager.email:
            emails.add(roles.operations_manager.email)
        return list(emails)

    # unit head + asset managers
    if asset.unit:
        if asset.unit.unit_head and asset.unit.unit_head.email:
            emails.add(asset.unit.unit_head.email)
        for u in asset.unit.asset_managers.all():
            if u.email:
                emails.add(u.email)

    return list(emails)


def can_user_approve_asset_change(user, asset, agency_roles) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True

    # Core/unallocated -> operations manager
    if asset.unit is None or getattr(asset.unit, "is_core_unit", False):
        return bool(agency_roles and agency_roles.operations_manager_id == user.id)

    # unit head or asset manager for that unit
    if asset.unit and asset.unit.unit_head_id == user.id:
        return True
    if asset.unit and asset.unit.asset_managers.filter(id=user.id).exists():
        return True

    return False

# -------------------------------------------------------------------
# Return approval helper (Manager / Ops / Superuser)
# -------------------------------------------------------------------
def can_user_approve_return(user, agency_roles, asset) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True

    # Core/unallocated assets -> Ops Manager
    if asset.unit is None or getattr(asset.unit, "is_core_unit", False):
        return bool(agency_roles and agency_roles.operations_manager_id == user.id)

    # Unit assets -> unit head or asset manager
    if asset.unit and asset.unit.unit_head_id == user.id:
        return True
    if asset.unit and asset.unit.asset_managers.filter(id=user.id).exists():
        return True

    return False

