# accounts/report_bi_esign.py
"""
UN PASS eSign Report Builder.

A BI-style reporting layer over the existing eSign Form and Workflow data.

Design goals
------------
* A report may use a Form or a Workflow as its source.
* Operational records stay in their existing models. Reporting never copies or
  edits FormSubmission / WorkflowRun / WorkflowTask data.
* Saved dashboard definitions live in a small isolated table created by
  `ensure_report_tables()`. This deliberately avoids adding another migration
  to installations whose historic accounts migrations are already fragile.
* Every query is evaluated with the CURRENT viewer's normal UN PASS visibility
  rules. Sharing a report never grants access to records that person could not
  otherwise see.
* Form table fields become their own datasets, so line items can be charted,
  counted and aggregated properly instead of being treated as one text blob.

The front-end designer lives in:
  static/accounts/esign/report_builder.js
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from .models_esign_studio import (
    DocumentWorkflow,
    FormSubmission,
    FormTemplate,
    WorkflowRun,
    WorkflowTask,
    WorkflowEvent,
)
from .studio_common_esign import (
    can_use_template,
    shared_template_q,
    user_agency_id,
    visible_runs,
    visible_submissions,
)

REPORT_TABLE = "accounts_esign_report_definition"

MAX_WIDGETS = 80
MAX_FILTERS = 50
MAX_CONFIG_BYTES = 700_000

VISUAL_TYPES = {
    "card",
    "bar",
    "line",
    "area",
    "pie",
    "donut",
    "table",
    "matrix",
    "gauge",
    "funnel",
    "scatter",
    "slicer",
    "text",
}
AGGREGATIONS = {
    "count",
    "count_distinct",
    "sum",
    "avg",
    "min",
    "max",
    "percentage",
}
FILTER_OPS = {
    "eq",
    "neq",
    "contains",
    "not_contains",
    "in",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "is_blank",
    "not_blank",
}


class ReportError(ValueError):
    pass


def _json_load(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return deepcopy(default)


def _json_dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ensure_report_tables():
    """
    Create the isolated report-definition table when it does not exist.

    No existing model/table is modified and no Django migration record is
    touched. Safe to call repeatedly.
    """
    vendor = connection.vendor

    with connection.cursor() as cur:
        if vendor == "postgresql":
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {REPORT_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    agency_id BIGINT NOT NULL,
                    office_id BIGINT NULL,
                    created_by_id BIGINT NOT NULL,
                    source_type VARCHAR(16) NOT NULL,
                    source_id BIGINT NOT NULL,
                    name VARCHAR(160) NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    config JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    share_scope VARCHAR(12) NOT NULL DEFAULT 'private',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {REPORT_TABLE}_agency_idx "
                f"ON {REPORT_TABLE}(agency_id, is_active)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {REPORT_TABLE}_owner_idx "
                f"ON {REPORT_TABLE}(created_by_id, is_active)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {REPORT_TABLE}_source_idx "
                f"ON {REPORT_TABLE}(source_type, source_id, is_active)"
            )
        else:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {REPORT_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agency_id INTEGER NOT NULL,
                    office_id INTEGER NULL,
                    created_by_id INTEGER NOT NULL,
                    source_type VARCHAR(16) NOT NULL,
                    source_id INTEGER NOT NULL,
                    name VARCHAR(160) NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    config TEXT NOT NULL DEFAULT '{{}}',
                    share_scope VARCHAR(12) NOT NULL DEFAULT 'private',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {REPORT_TABLE}_agency_idx "
                f"ON {REPORT_TABLE}(agency_id, is_active)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {REPORT_TABLE}_owner_idx "
                f"ON {REPORT_TABLE}(created_by_id, is_active)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {REPORT_TABLE}_source_idx "
                f"ON {REPORT_TABLE}(source_type, source_id, is_active)"
            )


def _dict_row(cur, row):
    keys = [c[0] for c in cur.description]
    return dict(zip(keys, row))


def _normalize_report(row):
    if not row:
        return None
    out = dict(row)
    out["config"] = _json_load(out.get("config"), default_config("form", None))
    out["is_active"] = bool(out.get("is_active"))
    return out


def get_report(report_id):
    ensure_report_tables()
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {REPORT_TABLE} WHERE id=%s AND is_active=TRUE",
            [int(report_id)],
        )
        row = cur.fetchone()
        return _normalize_report(_dict_row(cur, row)) if row else None


def list_reports(user):
    ensure_report_tables()
    agency_id = user_agency_id(user)
    office_id = getattr(user, "country_office_id", None)

    if not agency_id and not getattr(user, "is_superuser", False):
        return []

    sql = f"""
        SELECT *
        FROM {REPORT_TABLE}
        WHERE is_active=TRUE
          AND (
            created_by_id=%s
            OR (
              agency_id=%s
              AND (
                share_scope='agency'
                OR (share_scope='office' AND office_id=%s)
              )
            )
          )
        ORDER BY updated_at DESC, id DESC
    """
    with connection.cursor() as cur:
        cur.execute(sql, [user.id, agency_id or -1, office_id])
        rows = [_normalize_report(_dict_row(cur, row)) for row in cur.fetchall()]

    # A shared report never overrides normal source visibility.
    return [r for r in rows if report_access(user, r, edit=False, check_source=True)]


def _source_object(source_type, source_id):
    source_type = str(source_type or "").lower()
    if source_type == "form":
        return FormTemplate.objects.filter(pk=source_id).select_related("workflow", "created_by").first()
    if source_type == "flow":
        return DocumentWorkflow.objects.filter(pk=source_id).select_related("form", "created_by").first()
    return None


def report_access(user, report, *, edit=False, check_source=True):
    if not report or not getattr(user, "is_authenticated", False):
        return False

    if getattr(user, "is_superuser", False):
        allowed = True
    elif int(report.get("created_by_id") or 0) == int(user.id):
        allowed = True
    elif edit:
        allowed = False
    else:
        agency_id = user_agency_id(user)
        scope = report.get("share_scope")
        allowed = bool(
            agency_id
            and int(report.get("agency_id") or 0) == int(agency_id)
            and (
                scope == "agency"
                or (
                    scope == "office"
                    and getattr(user, "country_office_id", None)
                    and int(report.get("office_id") or 0)
                    == int(getattr(user, "country_office_id", 0))
                )
            )
        )

    if not allowed or not check_source:
        return allowed

    source = _source_object(report.get("source_type"), report.get("source_id"))
    if source is None:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return can_use_template(user, source)


def create_report(user, agency, *, source_type, source_id, name=""):
    ensure_report_tables()
    source_type = str(source_type or "").strip().lower()
    if source_type not in {"form", "flow"}:
        raise ReportError("Choose a Form or Workflow as the report source.")

    source = _source_object(source_type, source_id)
    if source is None:
        raise ReportError("That report source no longer exists.")
    if not getattr(user, "is_superuser", False) and not can_use_template(user, source):
        raise ReportError("You do not have access to that report source.")

    report_name = (name or f"{source.name} report").strip()[:160]
    config = default_config(source_type, source)
    office_id = getattr(user, "country_office_id", None)
    agency_id = getattr(agency, "pk", agency)

    with connection.cursor() as cur:
        if connection.vendor == "postgresql":
            cur.execute(
                f"""
                INSERT INTO {REPORT_TABLE}
                    (agency_id, office_id, created_by_id, source_type, source_id,
                     name, description, config, share_scope)
                VALUES (%s,%s,%s,%s,%s,%s,'',%s::jsonb,'private')
                RETURNING id
                """,
                [
                    agency_id,
                    office_id,
                    user.id,
                    source_type,
                    source.pk,
                    report_name,
                    _json_dump(config),
                ],
            )
            report_id = cur.fetchone()[0]
        else:
            cur.execute(
                f"""
                INSERT INTO {REPORT_TABLE}
                    (agency_id, office_id, created_by_id, source_type, source_id,
                     name, description, config, share_scope)
                VALUES (%s,%s,%s,%s,%s,%s,'',%s,'private')
                """,
                [
                    agency_id,
                    office_id,
                    user.id,
                    source_type,
                    source.pk,
                    report_name,
                    _json_dump(config),
                ],
            )
            report_id = cur.lastrowid
    return get_report(report_id)


def update_report(report_id, user, *, name, description, share_scope, config):
    report = get_report(report_id)
    if not report_access(user, report, edit=True):
        raise ReportError("You cannot edit this report.")

    clean = clean_config(config)
    name = (name or report["name"] or "Untitled report").strip()[:160]
    description = str(description or "").strip()[:3000]
    share_scope = share_scope if share_scope in {"private", "office", "agency"} else "private"

    with connection.cursor() as cur:
        if connection.vendor == "postgresql":
            cur.execute(
                f"""
                UPDATE {REPORT_TABLE}
                   SET name=%s, description=%s, share_scope=%s,
                       config=%s::jsonb, updated_at=NOW()
                 WHERE id=%s
                """,
                [name, description, share_scope, _json_dump(clean), report_id],
            )
        else:
            cur.execute(
                f"""
                UPDATE {REPORT_TABLE}
                   SET name=%s, description=%s, share_scope=%s,
                       config=%s, updated_at=CURRENT_TIMESTAMP
                 WHERE id=%s
                """,
                [name, description, share_scope, _json_dump(clean), report_id],
            )
    return get_report(report_id)


def duplicate_report(report_id, user, agency):
    src = get_report(report_id)
    if not report_access(user, src, edit=False):
        raise ReportError("You cannot open this report.")

    new = create_report(
        user,
        agency,
        source_type=src["source_type"],
        source_id=src["source_id"],
        name=f"Copy of {src['name']}"[:160],
    )
    return update_report(
        new["id"],
        user,
        name=new["name"],
        description=src.get("description") or "",
        share_scope="private",
        config=deepcopy(src.get("config") or {}),
    )


def archive_report(report_id, user):
    report = get_report(report_id)
    if not report_access(user, report, edit=True):
        raise ReportError("You cannot delete this report.")
    with connection.cursor() as cur:
        if connection.vendor == "postgresql":
            cur.execute(
                f"UPDATE {REPORT_TABLE} SET is_active=FALSE, updated_at=NOW() WHERE id=%s",
                [report_id],
            )
        else:
            cur.execute(
                f"UPDATE {REPORT_TABLE} SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                [report_id],
            )


def _safe_widget(widget, index):
    if not isinstance(widget, dict):
        return None
    visual = str(widget.get("type") or "card").lower()
    if visual not in VISUAL_TYPES:
        visual = "card"

    layout = widget.get("layout") if isinstance(widget.get("layout"), dict) else {}
    x = max(0, min(11, int(layout.get("x") or 0)))
    y = max(0, min(500, int(layout.get("y") or 0)))
    w = max(1, min(12 - x, int(layout.get("w") or 4)))
    h = max(1, min(10, int(layout.get("h") or 3)))

    out = deepcopy(widget)
    out["id"] = str(widget.get("id") or f"w{index}")[:60]
    out["type"] = visual
    out["title"] = str(widget.get("title") or visual.title())[:160]
    out["dataset"] = str(widget.get("dataset") or "")[:120]
    out["layout"] = {"x": x, "y": y, "w": w, "h": h}
    out["filters"] = [
        f for f in (widget.get("filters") or [])[:MAX_FILTERS] if isinstance(f, dict)
    ]
    return out


def clean_config(config):
    if not isinstance(config, dict):
        config = {}

    widgets = []
    for i, widget in enumerate((config.get("widgets") or [])[:MAX_WIDGETS], start=1):
        clean = _safe_widget(widget, i)
        if clean:
            widgets.append(clean)

    filters = [
        f for f in (config.get("filters") or [])[:MAX_FILTERS]
        if isinstance(f, dict)
    ]
    theme = config.get("theme") if isinstance(config.get("theme"), dict) else {}
    page = config.get("page") if isinstance(config.get("page"), dict) else {}

    out = {
        "version": 1,
        "page": {
            "title": str(page.get("title") or "")[:160],
            "subtitle": str(page.get("subtitle") or "")[:300],
            "columns": 12,
            "row_height": 82,
        },
        "theme": {
            "accent": str(theme.get("accent") or "#009EDB")[:20],
            "background": str(theme.get("background") or "#F5F7FA")[:20],
            "card_background": str(theme.get("card_background") or "#FFFFFF")[:20],
        },
        "filters": filters,
        "widgets": widgets,
    }

    if len(_json_dump(out).encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ReportError("This dashboard is too large to save.")
    return out


def default_config(source_type, source):
    name = getattr(source, "name", "Report") if source else "Report"

    if source_type == "flow":
        widgets = [
            {
                "id": "runs",
                "type": "card",
                "title": "Total runs",
                "dataset": "runs",
                "measure": {"field": "__rows__", "agg": "count"},
                "layout": {"x": 0, "y": 0, "w": 3, "h": 2},
            },
            {
                "id": "complete",
                "type": "card",
                "title": "Completed",
                "dataset": "runs",
                "measure": {"field": "__rows__", "agg": "count"},
                "filters": [{"field": "status", "op": "eq", "value": "completed"}],
                "layout": {"x": 3, "y": 0, "w": 3, "h": 2},
            },
            {
                "id": "duration",
                "type": "card",
                "title": "Avg. duration (hours)",
                "dataset": "runs",
                "measure": {"field": "duration_hours", "agg": "avg"},
                "format": {"decimals": 1},
                "layout": {"x": 6, "y": 0, "w": 3, "h": 2},
            },
            {
                "id": "overdue",
                "type": "card",
                "title": "Overdue tasks",
                "dataset": "tasks",
                "measure": {"field": "__rows__", "agg": "count"},
                "filters": [{"field": "is_overdue", "op": "eq", "value": True}],
                "layout": {"x": 9, "y": 0, "w": 3, "h": 2},
            },
            {
                "id": "status",
                "type": "donut",
                "title": "Runs by status",
                "dataset": "runs",
                "dimension": {"field": "status"},
                "measure": {"field": "__rows__", "agg": "count"},
                "layout": {"x": 0, "y": 2, "w": 5, "h": 4},
            },
            {
                "id": "trend",
                "type": "line",
                "title": "Runs started by month",
                "dataset": "runs",
                "dimension": {"field": "started_at", "bin": "month"},
                "measure": {"field": "__rows__", "agg": "count"},
                "layout": {"x": 5, "y": 2, "w": 7, "h": 4},
            },
            {
                "id": "task_status",
                "type": "bar",
                "title": "Tasks by status",
                "dataset": "tasks",
                "dimension": {"field": "task_status"},
                "measure": {"field": "__rows__", "agg": "count"},
                "layout": {"x": 0, "y": 6, "w": 5, "h": 4},
            },
            {
                "id": "recent",
                "type": "table",
                "title": "Recent runs",
                "dataset": "runs",
                "columns": ["reference", "subject", "status", "initiator", "started_at", "progress_percent"],
                "limit": 50,
                "layout": {"x": 5, "y": 6, "w": 7, "h": 4},
            },
        ]
    else:
        widgets = [
            {
                "id": "submissions",
                "type": "card",
                "title": "Total submissions",
                "dataset": "submissions",
                "measure": {"field": "__rows__", "agg": "count"},
                "layout": {"x": 0, "y": 0, "w": 3, "h": 2},
            },
            {
                "id": "completed",
                "type": "card",
                "title": "Completed",
                "dataset": "submissions",
                "measure": {"field": "__rows__", "agg": "count"},
                "filters": [{"field": "status", "op": "eq", "value": "completed"}],
                "layout": {"x": 3, "y": 0, "w": 3, "h": 2},
            },
            {
                "id": "inflow",
                "type": "card",
                "title": "In workflow",
                "dataset": "submissions",
                "measure": {"field": "__rows__", "agg": "count"},
                "filters": [{"field": "status", "op": "eq", "value": "in_flow"}],
                "layout": {"x": 6, "y": 0, "w": 3, "h": 2},
            },
            {
                "id": "rejected",
                "type": "card",
                "title": "Rejected",
                "dataset": "submissions",
                "measure": {"field": "__rows__", "agg": "count"},
                "filters": [{"field": "status", "op": "eq", "value": "rejected"}],
                "layout": {"x": 9, "y": 0, "w": 3, "h": 2},
            },
            {
                "id": "status",
                "type": "donut",
                "title": "Submissions by status",
                "dataset": "submissions",
                "dimension": {"field": "status"},
                "measure": {"field": "__rows__", "agg": "count"},
                "layout": {"x": 0, "y": 2, "w": 5, "h": 4},
            },
            {
                "id": "trend",
                "type": "line",
                "title": "Submissions by month",
                "dataset": "submissions",
                "dimension": {"field": "submitted_at", "bin": "month"},
                "measure": {"field": "__rows__", "agg": "count"},
                "layout": {"x": 5, "y": 2, "w": 7, "h": 4},
            },
            {
                "id": "recent",
                "type": "table",
                "title": "Recent submissions",
                "dataset": "submissions",
                "columns": ["reference", "submitter_name", "status", "submitted_at"],
                "limit": 50,
                "layout": {"x": 0, "y": 6, "w": 12, "h": 4},
            },
        ]

    return clean_config(
        {
            "version": 1,
            "page": {
                "title": f"{name} dashboard",
                "subtitle": "",
                "columns": 12,
                "row_height": 82,
            },
            "theme": {
                "accent": "#009EDB",
                "background": "#F5F7FA",
                "card_background": "#FFFFFF",
            },
            "filters": [],
            "widgets": widgets,
        }
    )


def _field(fid, label, kind="text", *, role="dimension", choices=None, hidden=False):
    return {
        "id": fid,
        "label": label,
        "kind": kind,
        "role": role,
        "choices": choices or [],
        "hidden": bool(hidden),
    }


def _form_scalar_fields(form):
    out = []
    schema = form.schema or {}
    for el in schema.get("elements") or []:
        key = el.get("key")
        kind = el.get("type")
        if not key or kind == "table":
            continue
        if kind not in {
            "text",
            "textarea",
            "number",
            "email",
            "date",
            "select",
            "radio",
            "checkboxes",
            "yesno",
        }:
            continue
        data_kind = "number" if kind == "number" else "date" if kind == "date" else "category" if kind in {"select", "radio", "yesno", "checkboxes"} else "text"
        role = "measure" if kind == "number" else "dimension"
        out.append(
            _field(
                f"f__{key}",
                el.get("label") or key,
                data_kind,
                role=role,
                choices=el.get("options") or [],
            )
        )
    return out


def _table_datasets(form, context_fields):
    datasets = []
    for el in (form.schema or {}).get("elements") or []:
        if el.get("type") != "table" or not el.get("key"):
            continue
        fields = [
            _field("parent_reference", "Submission reference"),
            _field("parent_submitted_at", "Submitted", "datetime"),
            _field("parent_status", "Submission status", "category"),
            _field("parent_submitter", "Submitter"),
            _field("row_number", "Row number", "number", role="measure"),
        ]
        fields += deepcopy(context_fields)
        for col in el.get("columns") or []:
            key = col.get("key")
            if not key:
                continue
            kind = col.get("kind") or "text"
            data_kind = "number" if kind == "number" else "date" if kind == "date" else "text"
            fields.append(
                _field(
                    f"c__{key}",
                    col.get("label") or key,
                    data_kind,
                    role="measure" if data_kind == "number" else "dimension",
                )
            )
        datasets.append(
            {
                "id": f"table__{el['key']}",
                "name": el.get("label") or el["key"],
                "description": "One record per table row / item.",
                "fields": fields,
                "table_key": el["key"],
            }
        )
    return datasets


def _linked_form(flow):
    if flow.form_id:
        return flow.form
    return (
        FormTemplate.objects.filter(workflow=flow)
        .order_by("id")
        .first()
    )


def source_catalog(user, source_type, source_id):
    source = _source_object(source_type, source_id)
    if source is None:
        raise ReportError("The report source no longer exists.")
    if not getattr(user, "is_superuser", False) and not can_use_template(user, source):
        raise ReportError("You do not have access to the report source.")

    if source_type == "form":
        scalar = _form_scalar_fields(source)
        datasets = [
            {
                "id": "submissions",
                "name": "Submissions",
                "description": "One record per submitted form.",
                "fields": [
                    _field("reference", "Reference"),
                    _field("submitted_at", "Submitted", "datetime"),
                    _field("submitter_name", "Submitted by"),
                    _field("submitter_email", "Submitter email"),
                    _field(
                        "status",
                        "Status",
                        "category",
                        choices=[
                            "submitted",
                            "in_flow",
                            "completed",
                            "rejected",
                            "cancelled",
                        ],
                    ),
                ]
                + scalar,
            }
        ]
        datasets += _table_datasets(source, scalar)
        return {
            "type": "form",
            "id": source.id,
            "name": source.name,
            "datasets": datasets,
        }

    form = _linked_form(source)
    form_fields = _form_scalar_fields(form) if form else []

    datasets = [
        {
            "id": "runs",
            "name": "Workflow runs",
            "description": "One record per workflow run.",
            "fields": [
                _field("reference", "Run reference"),
                _field("subject", "Subject"),
                _field(
                    "status",
                    "Run status",
                    "category",
                    choices=["running", "returned", "blocked", "completed", "rejected", "cancelled"],
                ),
                _field("initiator", "Initiator"),
                _field("started_at", "Started", "datetime"),
                _field("completed_at", "Completed", "datetime"),
                _field("duration_hours", "Duration (hours)", "number", role="measure"),
                _field("progress_percent", "Progress %", "number", role="measure"),
                _field("workflow_version", "Workflow version", "number", role="measure"),
            ]
            + form_fields,
        },
        {
            "id": "tasks",
            "name": "Workflow tasks",
            "description": "One record per approval, review, fill, signature or workflow task.",
            "fields": [
                _field("run_reference", "Run reference"),
                _field("run_subject", "Run subject"),
                _field("run_status", "Run status", "category"),
                _field("initiator", "Initiator"),
                _field("step", "Step"),
                _field(
                    "kind",
                    "Step type",
                    "category",
                    choices=["approval", "review", "fill", "signature", "prepare", "resubmit"],
                ),
                _field("assignee", "Assignee"),
                _field("assignee_email", "Assignee email"),
                _field(
                    "task_status",
                    "Task status",
                    "category",
                    choices=["pending", "waiting", "approved", "rejected", "returned", "done", "skipped", "cancelled"],
                ),
                _field("round", "Round", "number", role="measure"),
                _field("created_at", "Assigned", "datetime"),
                _field("decided_at", "Decided", "datetime"),
                _field("turnaround_hours", "Turnaround (hours)", "number", role="measure"),
                _field("due_at", "Due", "datetime"),
                _field("is_overdue", "Overdue", "boolean"),
            ]
            + form_fields,
        },
        {
            "id": "events",
            "name": "Workflow events",
            "description": "One record per immutable workflow audit-trail event.",
            "fields": [
                _field("run_reference", "Run reference"),
                _field("run_subject", "Run subject"),
                _field("run_status", "Run status", "category"),
                _field("event", "Event", "category"),
                _field("step", "Step / node"),
                _field("actor", "Actor"),
                _field("note", "Note"),
                _field("at", "Event time", "datetime"),
            ]
            + form_fields,
        },
    ]
    return {
        "type": "flow",
        "id": source.id,
        "name": source.name,
        "linked_form": {"id": form.id, "name": form.name} if form else None,
        "datasets": datasets,
    }


def _display_json(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_display_json(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_display_json(v)}" for k, v in value.items())
    if isinstance(value, Decimal):
        return float(value)
    return value


def _iso(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime):
            try:
                value = timezone.localtime(value)
            except Exception:
                pass
        return value.isoformat()
    return str(value)


def _hours_between(start, end):
    if not start or not end:
        return None
    try:
        return round((end - start).total_seconds() / 3600.0, 4)
    except Exception:
        return None


def _form_values(values, fields):
    values = values or {}
    out = {}
    for f in fields:
        if not f["id"].startswith("f__"):
            continue
        key = f["id"][3:]
        value = values.get(key)
        if f["kind"] == "number":
            out[f["id"]] = _number(value)
        else:
            out[f["id"]] = _display_json(value)
    return out


def dataset_rows(user, source_type, source_id, dataset_id):
    """
    Materialize a reporting dataset using the viewer's normal visibility.

    Defaults are deliberately generous for a country-office dashboard while
    preventing an accidental browser request from materialising millions of
    JSON rows. The API returns `truncated=True` when a limit is hit.
    """
    catalog = source_catalog(user, source_type, source_id)
    ds = next((d for d in catalog["datasets"] if d["id"] == dataset_id), None)
    if ds is None:
        raise ReportError("Choose a valid dataset for this report.")

    max_rows = int(getattr(settings, "ESIGN_REPORT_MAX_SOURCE_ROWS", 20_000))
    max_table_rows = int(getattr(settings, "ESIGN_REPORT_MAX_TABLE_ROWS", 50_000))
    source = _source_object(source_type, source_id)

    rows = []
    truncated = False

    if source_type == "form":
        scalar_fields = _form_scalar_fields(source)
        subs = (
            visible_submissions(user)
            .filter(form_id=source.id)
            .select_related("submitted_by")
            .order_by("-created_at")
        )

        if dataset_id == "submissions":
            for index, sub in enumerate(subs.iterator(chunk_size=500)):
                if index >= max_rows:
                    truncated = True
                    break
                row = {
                    "reference": sub.reference,
                    "submitted_at": _iso(sub.created_at),
                    "submitter_name": sub.submitter_name or "",
                    "submitter_email": sub.submitter_email or "",
                    "status": sub.status,
                }
                row.update(_form_values(sub.values, scalar_fields))
                rows.append(row)
        else:
            table_key = ds.get("table_key")
            count = 0
            for sub in subs.iterator(chunk_size=300):
                context = _form_values(sub.values, scalar_fields)
                for row_index, item in enumerate((sub.values or {}).get(table_key) or [], start=1):
                    if not isinstance(item, dict):
                        continue
                    if count >= max_table_rows:
                        truncated = True
                        break
                    row = {
                        "parent_reference": sub.reference,
                        "parent_submitted_at": _iso(sub.created_at),
                        "parent_status": sub.status,
                        "parent_submitter": sub.submitter_name or "",
                        "row_number": row_index,
                    }
                    row.update(context)
                    for field in ds["fields"]:
                        if not field["id"].startswith("c__"):
                            continue
                        key = field["id"][3:]
                        value = item.get(key)
                        row[field["id"]] = _number(value) if field["kind"] == "number" else _display_json(value)
                    rows.append(row)
                    count += 1
                if truncated:
                    break

    else:
        form = _linked_form(source)
        form_fields = _form_scalar_fields(form) if form else []
        runs = (
            visible_runs(user)
            .filter(workflow_id=source.id)
            .select_related("initiator", "submission")
            .order_by("-started_at")
        )

        if dataset_id == "runs":
            for index, run in enumerate(runs.iterator(chunk_size=400)):
                if index >= max_rows:
                    truncated = True
                    break
                progress = run.progress()
                row = {
                    "reference": run.reference,
                    "subject": run.subject,
                    "status": run.status,
                    "initiator": (
                        run.initiator.get_full_name()
                        or run.initiator.username
                        if run.initiator
                        else ""
                    ),
                    "started_at": _iso(run.started_at),
                    "completed_at": _iso(run.completed_at),
                    "duration_hours": _hours_between(run.started_at, run.completed_at),
                    "progress_percent": progress["percent"],
                    "workflow_version": run.workflow_version,
                }
                if run.submission_id:
                    row.update(_form_values(run.submission.values, form_fields))
                rows.append(row)
        elif dataset_id == "tasks":
            # Keep the same visibility boundary by selecting tasks only from
            # visible run IDs.
            run_ids = runs.values_list("id", flat=True)
            tasks = (
                WorkflowTask.objects.filter(run_id__in=run_ids)
                .select_related("run", "run__initiator", "run__submission", "user")
                .order_by("-created_at", "-id")
            )
            now = timezone.now()
            for index, task in enumerate(tasks.iterator(chunk_size=500)):
                if index >= max_table_rows:
                    truncated = True
                    break
                run = task.run
                assignee = task.name or (
                    (task.user.get_full_name() or task.user.username) if task.user else ""
                )
                row = {
                    "run_reference": run.reference,
                    "run_subject": run.subject,
                    "run_status": run.status,
                    "initiator": (
                        run.initiator.get_full_name()
                        or run.initiator.username
                        if run.initiator
                        else ""
                    ),
                    "step": task.node_label or task.node_id,
                    "kind": task.kind,
                    "assignee": assignee,
                    "assignee_email": task.email or "",
                    "task_status": task.status,
                    "round": task.round,
                    "created_at": _iso(task.created_at),
                    "decided_at": _iso(task.decided_at),
                    "turnaround_hours": _hours_between(task.created_at, task.decided_at),
                    "due_at": _iso(task.due_at),
                    "is_overdue": bool(task.status in WorkflowTask.OPEN and task.due_at and task.due_at < now),
                }
                if run.submission_id:
                    row.update(_form_values(run.submission.values, form_fields))
                rows.append(row)
        else:
            run_ids = runs.values_list("id", flat=True)
            events = (
                WorkflowEvent.objects.filter(run_id__in=run_ids)
                .select_related("run", "run__submission", "actor")
                .order_by("-at", "-id")
            )
            for index, event in enumerate(events.iterator(chunk_size=500)):
                if index >= max_table_rows:
                    truncated = True
                    break
                run = event.run
                actor = event.actor_name or (
                    (event.actor.get_full_name() or event.actor.username)
                    if event.actor else ""
                )
                row = {
                    "run_reference": run.reference,
                    "run_subject": run.subject,
                    "run_status": run.status,
                    "event": event.event,
                    "step": event.node_id or "",
                    "actor": actor,
                    "note": event.note or "",
                    "at": _iso(event.at),
                }
                if run.submission_id:
                    row.update(_form_values(run.submission.values, form_fields))
                rows.append(row)

    return rows, {"truncated": truncated, "row_count": len(rows)}


def _number(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, Decimal)):
        try:
            n = float(value)
            return n if math.isfinite(n) else None
        except Exception:
            return None
    try:
        n = float(str(value).strip().replace(",", "").replace("%", ""))
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def _blank(value):
    return value is None or value == "" or value == [] or value == {}


def _coerce_compare(value):
    n = _number(value)
    if n is not None:
        return ("number", n)
    if isinstance(value, bool):
        return ("bool", value)
    return ("text", str(value or "").strip().casefold())


def _one_filter(row, flt):
    field = str(flt.get("field") or "")
    op = str(flt.get("op") or "eq")
    if not field or op not in FILTER_OPS:
        return True

    value = row.get(field)
    target = flt.get("value")

    if op == "is_blank":
        return _blank(value)
    if op == "not_blank":
        return not _blank(value)

    if op in {"contains", "not_contains"}:
        hay = str(value or "").casefold()
        needle = str(target or "").casefold()
        found = needle in hay
        return found if op == "contains" else not found

    if op == "in":
        choices = target if isinstance(target, list) else [x.strip() for x in str(target or "").split(",")]
        choices = {str(x).casefold() for x in choices}
        return str(value or "").casefold() in choices

    if op == "between":
        parts = target if isinstance(target, list) else str(target or "").split(",", 1)
        if len(parts) < 2:
            return True
        n = _number(value)
        lo, hi = _number(parts[0]), _number(parts[1])
        if n is not None and lo is not None and hi is not None:
            return lo <= n <= hi
        v = str(value or "")
        return str(parts[0]) <= v <= str(parts[1])

    kind_a, a = _coerce_compare(value)
    kind_b, b = _coerce_compare(target)
    if kind_a == "number" and kind_b == "number":
        left, right = a, b
    else:
        left, right = str(value or "").casefold(), str(target or "").casefold()

    if op == "eq":
        return left == right
    if op == "neq":
        return left != right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    return True


def apply_filters(rows, filters, *, dataset_id):
    relevant = []
    for flt in filters or []:
        if not isinstance(flt, dict):
            continue
        target_dataset = str(flt.get("dataset") or "")
        if target_dataset and target_dataset != dataset_id:
            continue
        relevant.append(flt)
    if not relevant:
        return rows
    return [row for row in rows if all(_one_filter(row, flt) for flt in relevant)]


def _parse_dt(value):
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _bucket(value, mode):
    if value in (None, ""):
        return "(Blank)"
    if not mode:
        return _display_json(value)

    dt = _parse_dt(value)
    if not dt:
        return _display_json(value)

    if mode == "year":
        return f"{dt.year:04d}"
    if mode == "quarter":
        return f"{dt.year:04d} Q{((dt.month - 1) // 3) + 1}"
    if mode == "month":
        return f"{dt.year:04d}-{dt.month:02d}"
    if mode == "week":
        iso = dt.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    if mode == "day":
        return dt.strftime("%Y-%m-%d")
    return _display_json(value)


def _agg(rows, measure):
    measure = measure if isinstance(measure, dict) else {}
    field = str(measure.get("field") or "__rows__")
    agg = str(measure.get("agg") or "count").lower()
    if agg not in AGGREGATIONS:
        agg = "count"

    if field == "__rows__":
        raw = [1 for _ in rows]
        values = [1.0 for _ in rows]
    else:
        raw = [r.get(field) for r in rows if not _blank(r.get(field))]
        values = [n for n in (_number(v) for v in raw) if n is not None]

    if agg == "count":
        return len(rows) if field == "__rows__" else len(raw)
    if agg == "count_distinct":
        return len({str(v) for v in raw})
    if agg == "sum":
        return sum(values)
    if agg == "avg":
        return statistics.fmean(values) if values else 0
    if agg == "min":
        return min(values) if values else 0
    if agg == "max":
        return max(values) if values else 0
    # "percentage" is resolved after grouping because it needs the grand total.
    # Numeric measures contribute their sum; categorical measures contribute a
    # count of non-empty records.
    if field == "__rows__":
        return len(rows)
    return sum(values) if values else len(raw)


def _sort_rows(items, *, sort="value_desc", limit=30):
    if sort == "value_asc":
        items.sort(key=lambda x: (x.get("value") is None, x.get("value") or 0))
    elif sort == "label_asc":
        items.sort(key=lambda x: str(x.get("label") or "").casefold())
    elif sort == "label_desc":
        items.sort(key=lambda x: str(x.get("label") or "").casefold(), reverse=True)
    else:
        items.sort(key=lambda x: x.get("value") or 0, reverse=True)
    return items[: max(1, min(200, int(limit or 30)))]


def query_widget(rows, widget):
    visual = str(widget.get("type") or "card")
    measure = widget.get("measure") or {"field": "__rows__", "agg": "count"}
    dimension = widget.get("dimension") or {}
    series_spec = widget.get("series") or {}

    if visual == "text":
        return {"kind": "text", "text": str(widget.get("text") or "")}

    if visual in {"card", "gauge"}:
        value = _agg(rows, measure)
        return {
            "kind": "scalar",
            "value": value,
            "row_count": len(rows),
            "target": widget.get("target"),
            "minimum": widget.get("minimum", 0),
            "maximum": widget.get("maximum"),
        }

    if visual == "table":
        columns = [str(c) for c in (widget.get("columns") or []) if c]
        if not columns:
            columns = list(rows[0].keys())[:8] if rows else []
        limit = max(1, min(500, int(widget.get("limit") or 100)))
        return {
            "kind": "table",
            "columns": columns,
            "rows": [{c: _display_json(row.get(c)) for c in columns} for row in rows[:limit]],
            "total_rows": len(rows),
        }

    if visual == "slicer":
        field = str(dimension.get("field") or "")
        counts = defaultdict(int)
        for row in rows:
            counts[str(_display_json(row.get(field)) or "(Blank)")] += 1
        items = [{"label": label, "value": count, "filter_value": label} for label, count in counts.items()]
        return {"kind": "slicer", "field": field, "items": _sort_rows(items, sort="value_desc", limit=200)}

    if visual == "scatter":
        xfield = str((widget.get("x") or {}).get("field") or dimension.get("field") or "")
        yfield = str((widget.get("y") or {}).get("field") or measure.get("field") or "")
        points = []
        for row in rows:
            x = _number(row.get(xfield))
            y = _number(row.get(yfield))
            if x is None or y is None:
                continue
            points.append(
                {
                    "x": x,
                    "y": y,
                    "label": str(row.get(widget.get("label_field") or "") or ""),
                }
            )
            if len(points) >= 1000:
                break
        return {"kind": "scatter", "points": points, "x_field": xfield, "y_field": yfield}

    if visual == "matrix":
        row_field = str((widget.get("matrix_row") or {}).get("field") or dimension.get("field") or "")
        col_field = str((widget.get("matrix_col") or {}).get("field") or series_spec.get("field") or "")
        buckets = defaultdict(list)
        row_labels, col_labels = set(), set()
        for row in rows:
            rlabel = str(_display_json(row.get(row_field)) or "(Blank)")
            clabel = str(_display_json(row.get(col_field)) or "(Blank)")
            row_labels.add(rlabel)
            col_labels.add(clabel)
            buckets[(rlabel, clabel)].append(row)
        rlist = sorted(row_labels)[:80]
        clist = sorted(col_labels)[:40]
        cells = {
            f"{r}\u241f{c}": _agg(buckets[(r, c)], measure)
            for r in rlist
            for c in clist
            if (r, c) in buckets
        }
        return {"kind": "matrix", "rows": rlist, "columns": clist, "cells": cells}

    field = str(dimension.get("field") or "")
    date_bin = str(dimension.get("bin") or "")
    series_field = str(series_spec.get("field") or "")
    grouped = defaultdict(list)

    for row in rows:
        label = _bucket(row.get(field), date_bin)
        series = str(_display_json(row.get(series_field)) or "") if series_field else ""
        grouped[(str(label), series)].append(row)

    data = []
    for (label, series), members in grouped.items():
        data.append(
            {
                "label": label,
                "series": series,
                "value": _agg(members, measure),
                "filter_value": label,
            }
        )

    if measure.get("agg") == "percentage":
        total = sum(float(item["value"] or 0) for item in data) or 1.0
        for item in data:
            item["value"] = float(item["value"] or 0) * 100.0 / total

    # Time buckets need chronological order; other visuals default to ranking.
    if date_bin:
        data.sort(key=lambda item: item["label"])
        data = data[:200]
    else:
        data = _sort_rows(
            data,
            sort=str(widget.get("sort") or "value_desc"),
            limit=int(widget.get("top_n") or 30),
        )

    series_names = sorted({d["series"] for d in data if d["series"]})
    return {
        "kind": "series",
        "data": data,
        "series_names": series_names,
        "dimension_field": field,
        "series_field": series_field,
        "measure": measure,
    }


def query_report(user, report, widgets, *, runtime_filters=None):
    if not report_access(user, report, edit=False):
        raise ReportError("You cannot view this report.")

    config = report.get("config") or {}
    global_filters = list(config.get("filters") or [])
    runtime_filters = runtime_filters if isinstance(runtime_filters, list) else []
    source_type = report["source_type"]
    source_id = report["source_id"]

    cache = {}
    results = {}
    meta = {}

    for widget in (widgets or [])[:MAX_WIDGETS]:
        if not isinstance(widget, dict):
            continue
        wid = str(widget.get("id") or "")
        dataset_id = str(widget.get("dataset") or "")
        if not wid or not dataset_id:
            continue

        if dataset_id not in cache:
            rows, info = dataset_rows(user, source_type, source_id, dataset_id)
            cache[dataset_id] = rows
            meta[dataset_id] = info

        rows = cache[dataset_id]
        filters = global_filters + list(widget.get("filters") or []) + runtime_filters
        filtered = apply_filters(rows, filters, dataset_id=dataset_id)
        results[wid] = query_widget(filtered, widget)
        results[wid]["filtered_rows"] = len(filtered)

    return {"results": results, "datasets": meta}


def raw_export_rows(user, report, dataset_id):
    if not report_access(user, report, edit=False):
        raise ReportError("You cannot view this report.")
    rows, meta = dataset_rows(user, report["source_type"], report["source_id"], dataset_id)
    rows = apply_filters(rows, (report.get("config") or {}).get("filters") or [], dataset_id=dataset_id)
    return rows, meta
