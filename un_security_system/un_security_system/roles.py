def is_lsa_or_soc(user):
    return getattr(user, "role", "") in ("lsa", "soc")

def is_guard(user):
    return getattr(user, "role", "") in ("guard", "data_entry")

def is_not_guard(user):
    return user.is_authenticated and not is_guard(user)
