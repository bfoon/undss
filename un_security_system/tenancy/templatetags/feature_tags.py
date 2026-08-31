"""
tenancy/templatetags/feature_tags.py
====================================

    {% load feature_tags %}

    {% if user|has_feature:"esign" %} ... {% endif %}

    {% feature_on "room_booking" as can_book %}
    {% if can_book %} ... {% endif %}

The ``features`` dict from the context processor covers most cases; these tags
are for templates rendered outside the request context, and for checking a
feature on a user who is not request.user.
"""

from django import template

from ..catalog import FEATURES_BY_CODE
from ..services import has_all, has_any, has_feature as _has_feature

register = template.Library()


@register.filter(name="has_feature")
def has_feature_filter(user, code):
    return _has_feature(user, code)


@register.simple_tag(takes_context=True)
def feature_on(context, code):
    request = context.get("request")
    user = getattr(request, "user", None) if request else context.get("user")
    return _has_feature(user, code)


@register.simple_tag(takes_context=True)
def features_all(context, *codes):
    request = context.get("request")
    user = getattr(request, "user", None) if request else context.get("user")
    return has_all(user, codes)


@register.simple_tag(takes_context=True)
def features_any(context, *codes):
    request = context.get("request")
    user = getattr(request, "user", None) if request else context.get("user")
    return has_any(user, codes)


@register.simple_tag
def feature_name(code):
    feat = FEATURES_BY_CODE.get(code)
    return feat.name if feat else code
