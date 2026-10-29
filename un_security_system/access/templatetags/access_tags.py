# access/templatetags/access_tags.py
"""
Registered as a template builtin (settings.TEMPLATES ... "builtins"), so no
{% load %} is needed.

    {% if user|can:"packages.log_incoming" %} … {% endif %}
    {% if user|acts_as:"lsa,soc" %} … {% endif %}      ← replaces  user.role in "lsa,soc"
    {% access_roles user as roles %}                   ← the roles a person holds, with why
"""
from django import template

register = template.Library()


@register.filter(name="can")
def can_filter(user, code):
    from access.services import can

    try:
        return can(user, str(code))
    except Exception:  # noqa: BLE001 - never break a template
        return False


@register.filter(name="acts_as")
def acts_as_filter(user, codes):
    from access.services import acts_as

    if not getattr(user, "is_authenticated", False):
        return False
    wanted = [c.strip() for c in str(codes).replace(" ", ",").split(",") if c.strip()]
    try:
        return acts_as(user, *wanted)
    except Exception:  # noqa: BLE001
        return False


@register.simple_tag
def access_roles(user):
    from access.models import AccessRole
    from access.services import snapshot

    snap = snapshot(user)
    roles = {r.pk: r for r in AccessRole.objects.filter(pk__in=list(snap["roles"]))}
    out = []
    for rid, scopes in snap["roles"].items():
        if rid in roles:
            out.append({"role": roles[rid], "via": [v for *_s, v in scopes]})
    return sorted(out, key=lambda x: (not x["role"].is_system, x["role"].name))
