from django import template

from tenancy.scope_control import user_scope_status as resolve_user_scope_status

register = template.Library()


@register.simple_tag(name="user_scope_status")
def user_scope_status_tag(user):
    """
    Return the user's effective UNPASS scope status for templates.

    This does not change the user's Django is_active flag. It only reports
    whether the user's Agency / Country Office currently permits access.
    """
    try:
        return resolve_user_scope_status(user)
    except Exception:
        # Never allow a status badge to break the ICT user list.
        return {
            "active": True,
            "agency_active": True,
            "office_active": True,
            "agency": getattr(user, "agency", None),
            "office": getattr(user, "country_office", None),
            "reason": "",
            "message": "",
        }
