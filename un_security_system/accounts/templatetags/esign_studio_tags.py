# accounts/templatetags/esign_studio_tags.py
"""
Template helpers for eSign Studio.

    {% load esign_studio_tags %}

    {{ values|get_item:el.key }}             a form value by its dynamic key
    {{ el|form_display:value }}              a value as a person reads it
    {% esign_studio_summary as studio %}     dashboard strip: tasks and counts
"""

import json

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def get_item(mapping, key):
    if isinstance(mapping, dict):
        return mapping.get(key, "")
    return ""


@register.filter
def form_display(el, value):
    from ..form_pdf_esign import display_value

    return display_value(el, value)


@register.filter
def form_cell(el, values):
    """The display value of one element out of a whole values dict."""
    from ..form_pdf_esign import display_value

    if not isinstance(values, dict):
        return ""
    return display_value(el, values.get(el.get("key")))


@register.filter
def in_list(value, items):
    return isinstance(items, (list, tuple)) and value in items


@register.filter
def to_json(value):
    """
    A JS literal for use inside <script>. <, > and & are unicode-escaped so the
    value can never close the script tag or start markup.
    """
    raw = json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return mark_safe(raw)


@register.filter
def table_total(rows, col_key):
    from ..form_pdf_esign import _num, table_total as total

    return _num(total(None, rows, col_key))


@register.filter
def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return ""


@register.filter
def blank_rows(rows, minimum):
    """Existing table rows padded with empty ones up to `minimum`, for the fill page."""
    rows = list(rows) if isinstance(rows, list) else []
    try:
        minimum = int(minimum)
    except (TypeError, ValueError):
        minimum = 1
    return rows + [{} for _ in range(max(0, minimum - len(rows)))]


@register.simple_tag
def form_logic_payload(schema, values=None, scope="submitter", extras=None, hide_keys=None):
    """What the browser needs to keep conditional states up to date as people type."""
    from ..form_logic_esign import logic_payload

    if not isinstance(schema, dict):
        return {"elements": [], "values": {}, "scope": scope, "extras": {}}
    return logic_payload(schema, values or {}, scope=scope, extras=extras or {}, hide_keys=hide_keys or ())


@register.simple_tag
def esign_max_post_fields():
    """Django's DATA_UPLOAD_MAX_NUMBER_FIELDS, or 0 when unlimited — fill pages stay under it."""
    from django.conf import settings

    return getattr(settings, "DATA_UPLOAD_MAX_NUMBER_FIELDS", 1000) or 0


@register.simple_tag(takes_context=True)
def esign_studio_summary(context):
    """What the eSign dashboard shows about the studio. Two cheap counts and a short task list."""
    request = context.get("request")
    user = getattr(request, "user", None)
    empty = {"tasks": [], "task_count": 0, "open_runs": 0, "files": 0, "forms": 0, "flows": 0}
    if not user or not user.is_authenticated:
        return empty
    try:
        from ..models_esign_studio import DocumentWorkflow, FormTemplate, StudioFile, WorkflowRun
        from ..studio_common_esign import shared_template_q, visible_runs
        from ..views_esign_workflow import _my_open_tasks

        tasks = list(_my_open_tasks(user)[:6])
        return {
            "tasks": tasks,
            "task_count": _my_open_tasks(user).count(),
            "open_runs": visible_runs(user).filter(status__in=WorkflowRun.OPEN_STATUSES).count(),
            "files": StudioFile.objects.filter(owner=user).count(),
            "forms": FormTemplate.objects.filter(shared_template_q(user), is_published=True).count(),
            "flows": DocumentWorkflow.objects.filter(shared_template_q(user), is_active=True).count(),
        }
    except Exception:  # noqa: BLE001 - never break the dashboard
        return empty
