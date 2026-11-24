# accounts/agent_portability_esign.py
"""
UN PASS — Agentic Flows: export and import (.unpassagent files).

Same shape as the Flow Studio's .unpassflow packages: plain JSON with a
SHA-256 checksum, so a file that was edited by hand or damaged in transit is
refused rather than half-imported.

What travels:      cards, connections, context boxes, settings, knowledge links,
                   and every Policy card's rulebook and policy text (so the
                   importer never needs the original Word/PDF file)
What never does:   people (directory ids and email addresses), webhook
                   addresses (they are secrets), who it is shared with, runs.
The importer gets a list of the cards that need someone chosen again.
"""

from __future__ import annotations

import copy
import hashlib
import json

from django.utils import timezone

PACKAGE_KIND = "unpass-agent-flow"
PACKAGE_VERSION = 1
MAX_PACKAGE_BYTES = 2 * 1024 * 1024
EXTENSION = ".unpassagent"


class AgentPackageError(ValueError):
    pass


def _json_bytes(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sanitize(graph):
    """-> (graph without people or secrets, [what the importer must redo])."""
    graph = copy.deepcopy(graph or {})
    todo = []
    for node in graph.get("nodes") or []:
        cfg = node.get("config") or {}
        t, label = node.get("type"), node.get("label") or "A card"
        if t in ("email", "approval") and (cfg.get("users") or cfg.get("emails")):
            todo.append(f"{label}: choose who {'gets the email' if t == 'email' else 'checks the work'}")
        if t in ("email", "approval"):
            cfg["users"], cfg["emails"] = [], []
        if t == "webhook" and cfg.get("url"):
            todo.append(f"{label}: paste your own webhook address")
            cfg["url"] = ""
        if t == "policy":
            # The rulebook (and, unless switched off, the policy text) travels inside the agent —
            # nobody needs the original file. People and links to this account's agents/workflows don't.
            if not cfg.get("keep_text", True):
                cfg["text"] = ""
            for outcome, o in (cfg.get("outcomes") or {}).items():
                for a in o.get("actions") or []:
                    if a.get("users") or a.get("emails"):
                        todo.append(f"{label} → {outcome}: choose the people again")
                    a["users"], a["emails"] = [], []
                    if a.get("type") in ("agent", "workflow") and a.get("id"):
                        todo.append(f"{label} → {outcome}: choose the {'agent' if a['type'] == 'agent' else 'workflow'} to start")
                        a["id"] = None
            for r in cfg.get("rules") or []:
                then = r.get("then") or {}
                if then.get("type") in ("agent", "workflow") and then.get("id"):
                    todo.append(f"{label}: rule “{r.get('title')}” — choose the {then['type']} to start")
                    then["id"] = None
    return graph, todo


def export_package(flow, *, exported_by=None):
    from . import agent_engine_esign as E

    graph, todo = sanitize(E.clean_graph(flow.graph))
    payload = {
        "kind": PACKAGE_KIND,
        "package_version": PACKAGE_VERSION,
        "name": flow.name,
        "description": flow.description or "",
        "trigger": E.trigger_of(graph),
        "graph": graph,
        "needs": todo,
        "exported_at": timezone.now().isoformat(timespec="seconds"),
        "exported_by": ((exported_by.get_full_name() or exported_by.username) if exported_by else "")[:120],
        "card_count": len([n for n in graph["nodes"] if n["type"] not in ("start", "note")]),
    }
    payload["checksum"] = hashlib.sha256(_json_bytes(payload)).hexdigest()
    return payload


def dumps(flow, *, exported_by=None) -> bytes:
    return json.dumps(export_package(flow, exported_by=exported_by), indent=2, ensure_ascii=False).encode("utf-8")


def loads(raw) -> dict:
    """-> {name, description, graph, needs, exported_by, report}; raises AgentPackageError."""
    from . import agent_engine_esign as E

    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw or len(raw) > MAX_PACKAGE_BYTES:
        raise AgentPackageError("The file is empty or too large to be an agent.")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AgentPackageError("This isn't a UN PASS agent file (it isn't valid JSON).") from exc
    if not isinstance(data, dict) or data.get("kind") != PACKAGE_KIND:
        if isinstance(data, dict) and data.get("kind") == "unpass-esign-flow":
            raise AgentPackageError("That is a Flow Studio workflow (.unpassflow). Import it under Workflows instead.")
        raise AgentPackageError("This file is not a UN PASS agent (.unpassagent).")
    if int(data.get("package_version") or 0) != PACKAGE_VERSION:
        raise AgentPackageError(f"This agent was made by a newer version of UN PASS (format {data.get('package_version')!r}).")
    expected = data.get("checksum")
    if expected:
        check = dict(data)
        check.pop("checksum", None)
        if hashlib.sha256(_json_bytes(check)).hexdigest() != expected:
            raise AgentPackageError("The file has been changed or damaged since it was exported (its checksum doesn't match).")
    graph, todo = sanitize(E.clean_graph(data.get("graph") or {}))
    if not graph["nodes"]:
        raise AgentPackageError("The agent in this file has no cards.")
    return {
        "name": str(data.get("name") or "Imported agent").strip()[:150] or "Imported agent",
        "description": str(data.get("description") or "")[:2000],
        "graph": graph,
        "trigger": E.trigger_of(graph),
        "needs": [str(x)[:200] for x in (data.get("needs") or [])][:30] or todo,
        "exported_by": str(data.get("exported_by") or "")[:120],
        "exported_at": str(data.get("exported_at") or "")[:40],
        "report": E.validate_graph(graph),
    }
