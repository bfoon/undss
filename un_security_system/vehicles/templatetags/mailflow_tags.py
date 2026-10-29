# vehicles/templatetags/mailflow_tags.py
from django import template

register = template.Library()


@register.inclusion_tag("vehicles/packages/_studio_panel.html", takes_context=True)
def mail_studio_panel(context, package):
    """The Flow Studio panel on the package page. Renders nothing for classic packages."""
    from vehicles import panel_context

    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return {"panel": None}
    try:
        return {"panel": panel_context(package, user), "request": request}
    except Exception:  # noqa: BLE001 - never break the package page
        import logging

        logging.getLogger(__name__).exception("Mail flow panel failed for package %s", package.pk)
        return {"panel": None}


@register.filter
def mail_stage(package):
    """'Waiting on …' text for list rows of packages on Flow Studio, else ''."""
    try:
        link = package.studio_link
    except Exception:  # noqa: BLE001
        return ""
    return link.stage or ""
