from django import template

from accounts.security_console import security_access_summary

register = template.Library()


@register.simple_tag(takes_context=True)
def security_console_access(context):
    request = context.get("request")
    user = getattr(request, "user", None)

    if request is not None and hasattr(request, "_security_console_access"):
        return request._security_console_access

    summary = security_access_summary(user)

    if request is not None:
        request._security_console_access = summary

    return summary
