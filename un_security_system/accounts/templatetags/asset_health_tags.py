from django import template
from django.db import OperationalError, ProgrammingError

from accounts.asset_health import get_health_context

register = template.Library()


@register.inclusion_tag(
    "accounts/assets/partials/_asset_health_summary.html",
    takes_context=True,
)
def asset_health_summary(context, asset):
    request = context.get("request")
    user = request.user if request else None

    can_manage = bool(
        user
        and (
            user.is_superuser
            or getattr(user, "role", "") == "ict_focal"
        )
    )

    try:
        data = get_health_context(asset.id)
    except (ProgrammingError, OperationalError):
        data = {
            "device": None,
            "latest": None,
            "health_history": [],
            "health_alerts": [],
            "schema_missing": True,
        }

    data.update(
        {
            "asset": asset,
            "can_manage_health": can_manage,
        }
    )
    return data
