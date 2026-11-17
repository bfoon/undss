# access/registry.py
"""
The capability catalog — every power a role can hold.

Two kinds of capability live here:

Legacy powers  (code "legacy.<role>")
    One per old fixed role: LSA, SOC, Registry, Reception, ICT Focal Point …
    UN PASS still checks those role codes in many places. A role that holds
    "legacy.lsa" counts as an LSA in every one of them — views, templates and
    database queries — without that code changing. Seeded system roles hold
    their own legacy power and can't lose it.

Capabilities  (code "<module>.<action>")
    Fine-grained powers checked with access.services.can(). These are the ones
    to grow: as a screen is moved off a fixed role it gets its own capability.

Any app can add capabilities by creating `<app>/capabilities.py` with:

    from access.registry import Capability
    CAPABILITIES = [
        Capability("visitors.approve", "Approve visitor requests", "Visitors",
                   "…", risk="medium", legacy=("lsa", "soc")),
    ]

They are picked up at start-up (see AccessConfig.ready()).
"""

from dataclasses import dataclass, field
from importlib import import_module

RISK_LEVELS = ("low", "medium", "high")


@dataclass(frozen=True)
class Capability:
    code: str
    label: str
    module: str
    description: str = ""
    risk: str = "low"
    #: Old fixed roles that had this power before — the seeded system roles get it.
    legacy: tuple = ()
    #: tenancy feature code; the capability is hidden where the module is off.
    feature: str = ""
    icon: str = "bi-check2-square"

    @property
    def is_legacy(self) -> bool:
        return self.code.startswith("legacy.")


# Separation of duties: holding both halves of a pair in one person is flagged.
CONFLICTS = [
    ("access.assign", "access.approve_requests",
     "Can grant roles and also approve the requests for them — one person could give themselves anything."),
    ("access.manage_roles", "access.approve_requests",
     "Can design a role and approve requests for it."),
]

LEGACY_ICONS = {
    "requester": "bi-person", "data_entry": "bi-door-open", "lsa": "bi-shield-check",
    "soc": "bi-broadcast", "reception": "bi-person-badge", "registry": "bi-journal-text",
    "ict_focal": "bi-pc-display", "csm": "bi-building-gear", "agency_hr": "bi-people",
}
LEGACY_COLORS = {
    "requester": "#64748B", "data_entry": "#0EA5E9", "lsa": "#1D4ED8", "soc": "#7C3AED",
    "reception": "#DB2777", "registry": "#0F766E", "ict_focal": "#EA580C", "csm": "#CA8A04",
    "agency_hr": "#16A34A",
}
HIGH_RISK_LEGACY = {"lsa", "soc", "ict_focal"}


def _legacy_powers():
    from django.contrib.auth import get_user_model

    choices = getattr(get_user_model(), "ROLE_CHOICES", [])
    out = []
    for code, label in choices:
        if code == "requester":
            continue  # everybody is a requester; it is the absence of a power
        out.append(Capability(
            code=f"legacy.{code}",
            label=f"Acts as {label}",
            module="Legacy powers",
            description=(f"Counts as “{label}” everywhere UN PASS still checks that role — "
                         "menus, screens, approvals and the people lists those screens use."),
            risk="high" if code in HIGH_RISK_LEGACY else "medium",
            legacy=(code,),
            icon=LEGACY_ICONS.get(code, "bi-person-badge"),
        ))
    return out


BUILTIN = [
    # ── Access itself ────────────────────────────────────────────────────────
    Capability("access.view", "Open Access Studio", "Access control",
               "See roles, groups and who holds what in your office.", "low",
               legacy=("ict_focal", "lsa"), icon="bi-eye"),
    Capability("access.manage_roles", "Design roles and groups", "Access control",
               "Create, edit and retire roles and groups for your office or agency.", "high",
               legacy=("ict_focal",), icon="bi-diagram-2"),
    Capability("access.assign", "Give and take away roles", "Access control",
               "Assign roles to people or groups, with an end date if needed. "
               "You can only hand out powers you hold yourself.", "high",
               legacy=("ict_focal",), icon="bi-person-plus"),
    Capability("access.approve_requests", "Approve access requests", "Access control",
               "Decide on roles people ask for from My access.", "medium",
               legacy=("lsa",), icon="bi-inbox"),
    Capability("access.delegate", "Hand over my roles while away", "Access control",
               "Temporarily pass your own roles to a colleague (up to 30 days).", "low",
               legacy=("lsa", "soc", "ict_focal", "csm", "registry", "reception", "agency_hr"),
               icon="bi-arrow-left-right"),
    Capability("access.audit", "Read the access audit log", "Access control",
               "See every change to roles, groups and assignments.", "low",
               legacy=("ict_focal", "lsa"), icon="bi-clock-history"),

    # ── People ───────────────────────────────────────────────────────────────
    Capability("users.view_agency", "View staff in my agency", "People",
               "Open profiles of people in your own agency.", "low",
               legacy=("ict_focal",), icon="bi-person-lines-fill"),
    Capability("users.manage_agency", "Manage staff in my agency", "People",
               "Edit, activate and deactivate accounts in your own agency.", "high",
               legacy=("ict_focal",), icon="bi-person-gear"),
    Capability("users.manage_all", "Manage staff in every agency", "People",
               "Edit any account on the platform.", "high",
               legacy=("lsa",), icon="bi-globe2"),

    # ── Packages & Mailroom ──────────────────────────────────────────────────
    Capability("packages.log_incoming", "Log incoming packages", "Packages & Mailroom",
               "Register mail and parcels arriving at the office.", "low",
               legacy=("reception", "registry"), feature="mailroom", icon="bi-box-arrow-in-down"),
    Capability("packages.see_office", "See all of the office's mail", "Packages & Mailroom",
               "See every package in the office, not just the ones you're part of.", "medium",
               legacy=("reception", "registry"), feature="mailroom", icon="bi-mailbox"),
    Capability("packages.manage_flows", "Set up package workflows and mail flows", "Packages & Mailroom",
               "Classic package workflows, the mail docket, mail rules and starter flows.", "medium",
               legacy=("ict_focal",), feature="mailroom", icon="bi-diagram-3"),
]

_REGISTRY = {}
_LOADED = False


def _register(cap: Capability):
    if cap.risk not in RISK_LEVELS:
        raise ValueError(f"{cap.code}: risk must be one of {RISK_LEVELS}")
    _REGISTRY[cap.code] = cap


def autodiscover():
    """Load built-ins, legacy powers and every installed app's capabilities.py."""
    global _LOADED
    from django.apps import apps

    _REGISTRY.clear()
    for cap in BUILTIN:
        _register(cap)
    for cap in _legacy_powers():
        _register(cap)
    for config in apps.get_app_configs():
        try:
            module = import_module(f"{config.name}.capabilities")
        except ModuleNotFoundError as exc:
            if exc.name != f"{config.name}.capabilities":
                raise
            continue
        for cap in getattr(module, "CAPABILITIES", []):
            _register(cap)
    _LOADED = True


def all_capabilities():
    if not _LOADED:
        autodiscover()
    return dict(_REGISTRY)


def get(code):
    return all_capabilities().get(code)


def grouped(feature_map=None):
    """[(module, [caps])] in a stable order; hides capabilities whose module is off."""
    modules = {}
    for cap in all_capabilities().values():
        if feature_map is not None and cap.feature and not feature_map.get(cap.feature, False):
            continue
        modules.setdefault(cap.module, []).append(cap)
    order = ["Access control", "People"]
    keys = sorted(modules, key=lambda m: (m == "Legacy powers", m not in order,
                                          order.index(m) if m in order else 0, m))
    return [(m, sorted(modules[m], key=lambda c: c.label)) for m in keys]


def conflicts_in(codes):
    codes = set(codes)
    return [(a, b, why) for a, b, why in CONFLICTS if a in codes and b in codes]


def legacy_defaults(legacy_code):
    """Capabilities the seeded system role for an old fixed role starts with."""
    return sorted(c.code for c in all_capabilities().values() if legacy_code in c.legacy)
