"""
tenancy/catalog.py
==================

The single source of truth for every toggleable module in UN PASS.

Add a module here and it immediately appears in the superuser console, in the
template context, in the middleware gate and in the admin — nothing else to
edit.

Field notes
-----------
code            Stable string key. NEVER rename after go-live; grants store it.
name            Label shown to the superuser.
category        Grouping in the console UI.
description     One line of plain English for the console.
requires        Parent codes. The feature only resolves ON if every parent is
                also ON. Used for sub-modules (e.g. visitor_cards needs
                visitor_access).
default_enabled Fallback when no agency or country-office grant exists.
shareable       May be linked across offices/agencies (see DirectoryShare).
delegable       A country-office main admin may flip it without the superuser.
url_rules       fnmatch patterns of "<namespace>:<url_name>" that the
                FeatureGateMiddleware blocks when the feature is off. Where two
                patterns both match a URL name the LONGEST one wins, so
                "vehicles:package_flow_*" beats "vehicles:package_*".

URL rules below come from the real urls.py of accounts, incidents, comms,
dashboard, visitors and vehicles. Anything not listed is simply not gated —
the middleware never blocks by accident.
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple

CATEGORIES = (
    ("security", "Security & Access"),
    ("facilities", "Facilities & Common Services"),
    ("ict", "ICT & Assets"),
    ("people", "People & HR"),
    ("platform", "Platform"),
)


@dataclass(frozen=True)
class FeatureDef:
    code: str
    name: str
    category: str
    description: str = ""
    requires: Tuple[str, ...] = ()
    default_enabled: bool = False
    shareable: bool = False
    delegable: bool = False
    url_rules: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

FEATURES: Tuple[FeatureDef, ...] = (
    # ── Security & Access ──────────────────────────────────────────────────
    FeatureDef(
        code="visitor_access",
        name="Visitor access",
        category="security",
        description="Visitor registration, approval, gate check-in/out and group members.",
        url_rules=(
            "visitors:visitor_list", "visitors:visitor_create", "visitors:visitor_detail",
            "visitors:visitor_edit", "visitors:visitor_export", "visitors:visitor_reports",
            "visitors:visitor_logs", "visitors:visitor_logs_detail",
            "visitors:visitor_request_clearance", "visitors:visitor_lsa_approve",
            "visitors:visitor_lsa_reject", "visitors:visitor_cancel_request",
            "visitors:visitor_search_api", "visitors:visitor_stats_api",
            "visitors:visitor_status_api", "visitors:visitor_verify_page",
            "visitors:visitor_verify_lookup_api", "visitors:visitor_gate_flags",
            "visitors:approve_visitor", "visitors:bulk_approve", "visitors:export_visitors",
            "visitors:pending_approvals", "visitors:approved_visitors",
            "visitors:rejected_visitors", "visitors:active_visitors",
            "visitors:check_in_visitor", "visitors:check_out_visitor",
            "visitors:quick_check", "visitors:quick_check_page",
            "visitors:gate_check", "visitors:booking_gate_flags",
            "visitors:member_*", "visitors:delete_group_member",
        ),
    ),
    FeatureDef(
        code="visitor_cards",
        name="Visitor cards",
        category="security",
        description="Physical visitor badge register: issue, return and availability checks.",
        requires=("visitor_access",),
        delegable=True,
        url_rules=("visitors:visitor_card_*",),
    ),
    FeatureDef(
        code="visitor_meeting_link",
        name="Meeting-linked visitors",
        category="security",
        description="Tie an access request to a room booking and sync group members "
                    "from accepted meeting registrants.",
        requires=("visitor_access", "room_booking", "room_attendance"),
        delegable=True,
        url_rules=("visitors:sync_meeting_members", "visitors:booking_info_api"),
    ),
    FeatureDef(
        code="vehicle_access",
        name="Vehicle access",
        category="security",
        description="Vehicle register, gate movements and compound occupancy.",
        url_rules=(
            "vehicles:vehicle_list", "vehicles:vehicle_create", "vehicles:vehicle_detail",
            "vehicles:vehicle_edit", "vehicles:vehicle_delete", "vehicles:vehicle_lookup",
            "vehicles:vehicle_stats_api", "vehicles:movement_list",
            "vehicles:movement_detail", "vehicles:movement_reports",
            "vehicles:record_movement", "vehicles:quick_movement",
            "vehicles:recent_movements_api", "vehicles:compound_status_api",
            "vehicles:export_movements", "vehicles:reports",
        ),
    ),
    FeatureDef(
        code="parking_cards",
        name="Parking cards",
        category="security",
        description="Parking card register plus the staff request and LSA approval queue.",
        url_rules=(
            "vehicles:parking_card_*", "vehicles:pc_request_*",
            "vehicles:my_pc_requests", "vehicles:pc_requests_pending",
            "vehicles:validate_parking_card", "vehicles:export_parking_cards",
        ),
    ),
    FeatureDef(
        code="key_control",
        name="Key control",
        category="security",
        description="Issue, return and audit physical keys.",
        url_rules=("vehicles:key_*", "vehicles:quick_key"),
    ),
    FeatureDef(
        code="asset_exit",
        name="Asset exit clearance",
        category="security",
        description="Gate pass for taking equipment off the compound: agency approval, "
                    "LSA clearance, guard sign-out and sign-in.",
        url_rules=("vehicles:asset_exit_*", "vehicles:my_asset_exits"),
    ),
    FeatureDef(
        code="security_incidents",
        name="Security incidents",
        category="security",
        description="Guard-logged security incidents and the LSA/SOC triage queue.",
        url_rules=("accounts:incident_*", "accounts:security_incident_*"),
    ),
    FeatureDef(
        code="incident_reporting",
        name="Staff incident reporting",
        category="security",
        description="Staff-submitted incident reports with updates and status flow.",
        url_rules=(
            "incidents:new", "incidents:my_incidents", "incidents:triage",
            "incidents:incident_detail", "incidents:add_update", "incidents:change_status",
        ),
    ),
    FeatureDef(
        code="comms_devices",
        name="Radios & sat phones",
        category="security",
        description="Radio and satellite phone register, plus radio check sessions.",
        url_rules=("comms:*",),
    ),

    # ── Facilities & Common Services ───────────────────────────────────────
    FeatureDef(
        code="common_services",
        name="Common services requests",
        category="facilities",
        description="CSR workflow: cash power, electrical, plumbing, cleaning, grounds.",
        url_rules=("incidents:cs_*", "incidents:csr_*", "incidents:my_csr"),
    ),
    FeatureDef(
        code="room_booking",
        name="Room booking",
        category="facilities",
        description="Meeting rooms, recurring series and approvals.",
        url_rules=(
            "accounts:room_*", "accounts:rooms_*", "accounts:booking_*",
            "accounts:series_*", "accounts:cancel_*", "accounts:my_bookings",
            "accounts:reschedule_booking", "accounts:check_availability_api",
        ),
    ),
    FeatureDef(
        code="room_attendance",
        name="Meeting attendance & QR",
        category="facilities",
        description="QR registration, walk-in decisions and attendance exports.",
        requires=("room_booking",),
        delegable=True,
        url_rules=(
            "accounts:meeting_*", "accounts:attendance_*",
            "accounts:agenda_document_qr", "accounts:accept_registration",
            "accounts:walkin_decision",
        ),
    ),
    FeatureDef(
        code="mailroom",
        name="Packages & mailroom",
        category="facilities",
        description="Incoming and outgoing package logging, custody and delivery.",
        url_rules=(
            "vehicles:package_list", "vehicles:package_detail",
            "vehicles:package_log_new", "vehicles:package_log_outgoing",
            "vehicles:package_advance_step",
        ),
    ),
    FeatureDef(
        code="mailroom_flow",
        name="Mailroom workflow builder",
        category="facilities",
        description="Per-agency package routing templates and step configuration.",
        requires=("mailroom",),
        delegable=True,
        url_rules=("vehicles:package_flow_*",),
    ),
    FeatureDef(
        code="mailroom_signing",
        name="Mailroom document signing",
        category="facilities",
        description="Prepare package-step documents in the shared eSign engine, including "
                    "recipients, field placement, signing, audit trail and certificate.",
        # Package signing is a Mailroom capability, but the signing machinery is eSign.
        # tenancy.services._apply_dependencies() therefore switches this OFF whenever
        # either parent is unavailable.
        requires=("mailroom", "esign"),
        delegable=True,
        url_rules=("vehicles:document_*", "vehicles:signature_*"),
    ),

    # ── ICT & Assets ───────────────────────────────────────────────────────
    FeatureDef(
        code="ict_console",
        name="ICT console",
        category="ict",
        description="User management, password resets and registration links.",
        default_enabled=True,
        url_rules=(
            "accounts:ict_*", "accounts:registration_link*",
            "accounts:create_registration_link", "accounts:invite_qr_download",
        ),
    ),
    FeatureDef(
        code="asset_mgmt",
        name="Asset management",
        category="ict",
        description="Asset register, requests, assignment, verification and labels.",
        shareable=True,
        url_rules=("accounts:asset_*", "accounts:assets_*", "accounts:batch_*"),
    ),
    FeatureDef(
        code="consumables",
        name="Consumables & supplies",
        category="ict",
        description="Stock levels, supply requests and stock history.",
        requires=("asset_mgmt",),
        delegable=True,
        url_rules=("accounts:consumable*", "accounts:consumables_*"),
    ),
    FeatureDef(
        code="exit_clearance",
        name="Exit from organisation",
        category="ict",
        description="Staff separation clearance against assets still held.",
        requires=("asset_mgmt",),
        delegable=True,
        url_rules=("accounts:exit_organization",),
    ),
    FeatureDef(
        code="mobile_lines",
        name="Mobile lines",
        category="ict",
        description="Cell line register, reactivation requests and focal points.",
        url_rules=("accounts:mobile_*", "accounts:cell_*"),
    ),
    FeatureDef(
        code="esign",
        name="eSign",
        category="ict",
        description="Electronic signature envelopes, recipients and audit trail.",
        shareable=True,
        url_rules=("accounts:esign_*",),
    ),
    FeatureDef(
        code="esign_markup",
        name="eSign document markup",
        category="ict",
        description="Highlight, freehand and area-select comments on envelopes.",
        requires=("esign",),
        delegable=True,
        url_rules=("accounts:esign_markup_*",),
    ),

    # ── People & HR ────────────────────────────────────────────────────────
    FeatureDef(
        code="id_cards",
        name="Employee ID cards",
        category="people",
        description="ID card requests, approvals, printing and expiry tracking.",
        url_rules=("accounts:hr_*", "accounts:id_card_*", "accounts:employee_id_*"),
    ),

    # ── Platform ───────────────────────────────────────────────────────────
    FeatureDef(
        code="analytics",
        name="Analytics & reports",
        category="platform",
        description="Cross-module dashboards, exports and periodic reports.",
        default_enabled=True,
        url_rules=(
            "accounts:analytics*", "dashboard:analytics", "dashboard:reports",
            "dashboard:daily_report", "dashboard:weekly_report",
            "dashboard:monthly_report", "dashboard:export_*",
        ),
    ),
    FeatureDef(
        code="activity_log",
        name="Activity log",
        category="platform",
        description="Per-user audit trail of actions across the platform.",
        default_enabled=True,
        url_rules=("accounts:activity_*",),
    ),
    FeatureDef(
        code="sso_microsoft",
        name="Microsoft SSO",
        category="platform",
        description="Sign in with a Microsoft Entra ID work account. Needs an SSO "
                    "configuration before it will do anything.",
        url_rules=("tenancy:sso_start", "tenancy:sso_callback"),
    ),
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

FEATURES_BY_CODE: Dict[str, FeatureDef] = {f.code: f for f in FEATURES}
FEATURE_CODES: Tuple[str, ...] = tuple(FEATURES_BY_CODE)
FEATURE_CHOICES = tuple((f.code, f.name) for f in FEATURES)
CATEGORY_LABELS = dict(CATEGORIES)

DEFAULT_ENABLED_CODES = frozenset(f.code for f in FEATURES if f.default_enabled)
SHAREABLE_CODES = frozenset(f.code for f in FEATURES if f.shareable)
DELEGABLE_CODES = frozenset(f.code for f in FEATURES if f.delegable)
SHAREABLE_CHOICES = tuple((f.code, f.name) for f in FEATURES if f.shareable)


def get_feature(code: str) -> FeatureDef:
    """Return the FeatureDef for ``code`` or raise KeyError."""
    return FEATURES_BY_CODE[code]


def is_known(code: str) -> bool:
    return code in FEATURES_BY_CODE


def features_by_category() -> List[Tuple[str, str, List[FeatureDef]]]:
    """[(category_code, category_label, [FeatureDef, ...]), ...] in catalogue order."""
    out = []
    for cat_code, cat_label in CATEGORIES:
        items = [f for f in FEATURES if f.category == cat_code]
        if items:
            out.append((cat_code, cat_label, items))
    return out


def children_of(code: str) -> List[FeatureDef]:
    """Features that declare ``code`` as a parent."""
    return [f for f in FEATURES if code in f.requires]


def url_rule_index() -> List[Tuple[str, str]]:
    """
    [(pattern, feature_code), ...] sorted longest pattern first.

    The ordering matters: "vehicles:package_flow_*" and "vehicles:package_*"
    both match ``package_flow_config``, and the more specific rule has to win
    or the workflow builder would end up gated by the wrong switch.
    """
    rules = []
    for f in FEATURES:
        for rule in f.url_rules:
            rules.append((rule, f.code))
    rules.sort(key=lambda pair: len(pair[0]), reverse=True)
    return rules
