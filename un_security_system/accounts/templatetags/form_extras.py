"""
accounts/templatetags/form_extras.py
====================================

Add widget attributes from a template.

Django's own auth forms — `SetPasswordForm`, `PasswordResetForm`,
`PasswordChangeForm` — build their widgets without CSS classes:

    <input type="password" name="new_password1" required id="id_new_password1">

No `form-control`. So on every password page the inputs render as unstyled
browser defaults, and the `.form-control:focus` and `.form-control.is-invalid`
rules in those templates match nothing at all — the red border on a rejected
password never appeared.

You cannot pass attributes to `{{ field }}` in a template, and these are
Django's own forms, so there is nowhere to set them without subclassing every
one. Hence this filter.

    {% load form_extras %}
    {{ form.new_password1|add_class:"form-control form-control-lg" }}
    {{ form.email|add_class:"form-control"|add_attr:"placeholder:you@undp.org" }}

Existing attributes are preserved and classes are merged, so a field that
already carries `form-select` from its own widget keeps it.
"""

from django import template
from django.forms.boundfield import BoundField

register = template.Library()


@register.filter(name="add_class")
def add_class(field, css):
    """Merge CSS classes into a field's widget, keeping whatever it already has."""
    if not isinstance(field, BoundField):
        return field

    attrs = dict(field.field.widget.attrs)
    existing = attrs.get("class", "").split()
    for cls in css.split():
        if cls not in existing:
            existing.append(cls)
    attrs["class"] = " ".join(existing)
    return field.as_widget(attrs=attrs)


@register.filter(name="add_attr")
def add_attr(field, arg):
    """
    Set one attribute: `{{ field|add_attr:"placeholder:Your name" }}`.

    Splits on the first colon only, so values containing colons survive.
    """
    if not isinstance(field, BoundField) or ":" not in arg:
        return field

    name, _, value = arg.partition(":")
    attrs = dict(field.field.widget.attrs)
    attrs[name.strip()] = value.strip()
    return field.as_widget(attrs=attrs)


@register.filter(name="field_class")
def field_class(field, css="form-control"):
    """
    Like add_class, but picks the right Bootstrap class for the widget type,
    so one call handles inputs, selects and checkboxes.
    """
    if not isinstance(field, BoundField):
        return field

    widget = field.field.widget
    name = widget.__class__.__name__

    if name in ("Select", "SelectMultiple", "NullBooleanSelect"):
        css = "form-select"
    elif name in ("CheckboxInput",):
        css = "form-check-input"
    elif name in ("RadioSelect", "CheckboxSelectMultiple"):
        return field

    return add_class(field, css)
