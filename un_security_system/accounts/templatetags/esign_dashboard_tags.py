from django import template

from accounts.dashboard_esign_access import access_summary

register = template.Library()


@register.simple_tag(takes_context=True)
def esign_dashboard_access(context):
    request = context.get("request")
    user = getattr(request, "user", None)

    # _nav_items.html renders once in the desktop sidebar and once in the
    # mobile drawer. Resolve only once per request.
    if request is not None and hasattr(request, "_esign_dashboard_access"):
        return request._esign_dashboard_access

    summary = access_summary(user)

    if request is not None:
        request._esign_dashboard_access = summary

    return summary
