# accounts/permissions.py
def is_lsa(user):
    # True if logged-in and is LSA (or superuser)
    return user.is_authenticated and (getattr(user, "role", "") == "lsa" or user.is_superuser)

def is_data_entry(user):
    return user.is_authenticated and (getattr(user, "role", "") == "data_entry" or user.is_superuser)

def is_agency_approver(user):
    # adjust as needed for your designated approvers
    return user.is_authenticated and (getattr(user, "role", "") in ["approver", "soc", "lsa"] or user.is_staff or user.is_superuser)

def is_ict_focal(user):
    return bool(user.is_authenticated and user.role == 'ict_focal')

def is_lsa_or_soc(user):
    return bool(user.is_authenticated and user.role in ('lsa', 'soc'))
