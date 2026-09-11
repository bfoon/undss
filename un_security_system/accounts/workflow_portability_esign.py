"""Portable import/export packages for UNPASS eSign Studio workflows."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, Mapping

PACKAGE_KIND = "unpass-esign-flow"
PACKAGE_VERSION = 1
MAX_PACKAGE_BYTES = 2 * 1024 * 1024


class FlowPackageError(ValueError):
    pass


def _json_bytes(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sanitize_graph(graph: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Remove account-specific identities from a graph.

    Specific-person assignments are turned into "chosen when starting" role
    slots so the importing user must deliberately remap the people.
    """
    graph = copy.deepcopy(graph or {})
    for node in graph.get("nodes") or []:
        cfg = node.get("config") or {}
        assign = cfg.get("assign")
        if not isinstance(assign, dict):
            continue
        if assign.get("mode") == "people":
            assign["mode"] = "chosen"
            assign["slot"] = (assign.get("slot") or f"{node.get('label') or 'Step'} assignee")[:60]
        assign["users"] = []
        assign["emails"] = []
    return graph


def export_workflow_package(workflow, *, include_form=True) -> Dict[str, Any]:
    from . import form_pdf_esign as F
    from . import workflow_engine_esign as E

    graph = E.clean_graph(_sanitize_graph(workflow.graph or {}))
    payload = {
        "kind": PACKAGE_KIND,
        "package_version": PACKAGE_VERSION,
        "name": workflow.name,
        "description": workflow.description or "",
        "graph": graph,
        "share_scope": "private",
        "monitor_runs": bool(workflow.monitor_runs),
        "form": None,
    }

    if include_form and getattr(workflow, "form_id", None):
        form = workflow.form
        payload["form"] = {
            "name": form.name,
            "description": form.description or "",
            "category": form.category or "",
            "schema": F.clean_schema(form.schema or {}),
            "reference_prefix": form.reference_prefix or "FRM",
            "submit_message": form.submit_message or "",
        }

    checksum_payload = dict(payload)
    checksum_payload.pop("checksum", None)
    payload["checksum"] = hashlib.sha256(_json_bytes(checksum_payload)).hexdigest()
    return payload


def dumps_workflow_package(workflow, *, include_form=True) -> bytes:
    return json.dumps(
        export_workflow_package(workflow, include_form=include_form),
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")


def loads_package(raw: bytes | str) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw or len(raw) > MAX_PACKAGE_BYTES:
        raise FlowPackageError("The flow package is empty or too large.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FlowPackageError("This is not a valid UNPASS flow package.") from exc

    if not isinstance(data, dict) or data.get("kind") != PACKAGE_KIND:
        raise FlowPackageError("This file is not a UNPASS eSign flow package.")
    if int(data.get("package_version") or 0) != PACKAGE_VERSION:
        raise FlowPackageError(
            f"Unsupported flow package version {data.get('package_version')!r}."
        )

    expected = data.get("checksum")
    if expected:
        check = dict(data)
        check.pop("checksum", None)
        actual = hashlib.sha256(_json_bytes(check)).hexdigest()
        if actual != expected:
            raise FlowPackageError("The flow package checksum does not match.")

    from . import form_pdf_esign as F
    from . import workflow_engine_esign as E

    clean = {
        "name": str(data.get("name") or "Imported flow").strip()[:150] or "Imported flow",
        "description": str(data.get("description") or "")[:2000],
        "graph": E.clean_graph(_sanitize_graph(data.get("graph") or {})),
        "monitor_runs": bool(data.get("monitor_runs", True)),
        "form": None,
    }

    f = data.get("form")
    if isinstance(f, dict) and f.get("schema"):
        clean["form"] = {
            "name": str(f.get("name") or "Imported form").strip()[:150] or "Imported form",
            "description": str(f.get("description") or "")[:2000],
            "category": str(f.get("category") or "")[:60],
            "schema": F.clean_schema(f.get("schema") or {}),
            "reference_prefix": "".join(
                ch for ch in str(f.get("reference_prefix") or "FRM").upper()
                if ch.isalnum() or ch == "-"
            )[:16] or "FRM",
            "submit_message": str(f.get("submit_message") or "")[:300],
        }
    return clean
