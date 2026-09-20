import sys, django, types
sys.path.insert(0, "/home/claude/undss/un_security_system")
from django.conf import settings
settings.configure(DEBUG=True, INSTALLED_APPS=["django.contrib.contenttypes","django.contrib.auth"],
                   USE_TZ=True, ESIGN_BRAND="UN PASS")
django.setup()
from datetime import datetime, timezone as tz

from accounts import module_events as ME
from accounts import trigger_engine_esign as TE
from accounts import form_pdf_esign as F

# ── a stand-in for a completed asset request ────────────────────────────────
class U:
    def __init__(s, name, email, pk=1): s._n, s.email, s.pk, s.username = name, email, pk, name.lower()
    def get_full_name(s): return s._n
class Obj:
    def __init__(s, **kw): s.__dict__.update(kw)
    def __str__(s): return getattr(s, "_label", "object")

requester = U("Baboucarr Foon", "b.foon@un.org", 7)
ict = U("Awa Sallah", "a.sallah@un.org", 9)
manager = U("Modou Jallow", "m.jallow@un.org", 3)
record = Obj(
    pk=412, _label="Asset request #412 — Laptop",
    assigned_asset=Obj(name="Dell Latitude 5440", asset_tag="UNDP-LT-0142", serial_number="JHK92P3"),
    category=Obj(name="Laptop"), unit=Obj(name="ICT"), agency=Obj(name="UNDP", code="UNDP"), agency_id=1,
    requester=requester, ict_assigned_by=ict, manager_approved_by=manager,
    ict_assigned_at=datetime(2026, 9, 14, 10, 30, tzinfo=tz.utc),
    requester_verified_at=datetime(2026, 9, 19, 8, 5, tzinfo=tz.utc),
    justification="Replacement for a failed machine.",
)

event = ME.get("asset_request.received")
print("event:", event.module, "—", event.label)
ctx = ME.context_for(event, record, actor=requester)
for k in sorted(ctx): print(f"   {k:18} = {ctx[k]!r}")
assert ctx["asset_tag"] == "UNDP-LT-0142"
assert ctx["requester_name"] == "Baboucarr Foon"
assert ctx["issued_by"] == "Awa Sallah"
assert ctx["confirmed_on"] == "2026-09-19"      # dates come through as plain dates
assert ctx["agency"] == "UNDP"
assert ctx["record_label"].startswith("Asset request #412")

# a dead path must be blank, never an exception
broken = Obj(pk=1, _label="x", agency=None, requester=None, assigned_asset=None,
             category=None, unit=None, ict_assigned_by=None, manager_approved_by=None,
             ict_assigned_at=None, requester_verified_at=None, justification="")
ME.context_for(event, broken)
print("\nmissing relations survive:", ME.walk(broken, "assigned_asset.name") == "")

# who it can be handed to
assert ME.person_for(event, record, "requester") is requester
assert ME.person_for(event, record, "issuer") is ict
assert ME.person_for(event, record, "nobody") is None
print("people:", {r: ME.person_for(event, record, r).get_full_name() for r in ("requester","issuer","manager")})

# ── the handover form it would start ────────────────────────────────────────
schema = F.clean_schema({"header": {"title": "Asset handover undertaking"}, "elements": [
    {"id":"a","type":"text","label":"Staff member","key":"staff_member"},
    {"id":"b","type":"text","label":"Equipment","key":"equipment"},
    {"id":"c","type":"text","label":"Asset tag","key":"asset_tag"},
    {"id":"d","type":"date","label":"Issued on","key":"issued_on"},
    {"id":"e","type":"select","label":"Condition","key":"condition","options":["New","Good","Fair"]},
    {"id":"f","type":"number","label":"Replacement value (GMD)","key":"value"},
    {"id":"g","type":"text","label":"Declaration","key":"declaration"},
    {"id":"h","type":"text","label":"Untouched","key":"untouched"},
]})

class Trig:
    WHO_FIXED, WHO_ACTOR = "fixed", "actor"
    ACTION_DRAFT, ACTION_SUBMIT, ACTION_FLOW = "draft", "submit", "flow"
    assign_to, assignee = "requester", None
    target_form_id = 1
    target_form = types.SimpleNamespace(schema=schema, name="Handover", reference_prefix="HAND")
    conditions = []
    field_map = [
        {"target":"staff_member","expr":"[requester_name]"},
        {"target":"equipment","expr":"[asset_name]"},
        {"target":"asset_tag","expr":"[asset_tag]"},
        {"target":"issued_on","expr":"[confirmed_on]"},
        {"target":"condition","expr":'"New"'},
        {"target":"value","expr":"45000"},
        {"target":"declaration","expr":'CONCAT("I, ", [requester_name], ", confirm receipt of ", [asset_name], " (", [asset_tag], ") issued by ", [issued_by], ".")'},
        {"target":"ghost","expr":"[asset_name]"},
    ]

values, problems = TE.build_values(Trig(), ctx)
print("\nprefilled:")
for k, v in values.items(): print(f"   {k:14} = {v!r}")
print("problems:", problems)
assert values["staff_member"] == "Baboucarr Foon"
assert values["equipment"] == "Dell Latitude 5440"
assert values["issued_on"] == "2026-09-19"
assert values["condition"] == "New"
assert values["value"] == "45000"
assert "confirm receipt of Dell Latitude 5440" in values["declaration"]
assert values.get("untouched", "") == ""
assert problems and "ghost" in problems[0]

# ── conditions ──────────────────────────────────────────────────────────────
def holds(rules):
    t = Trig(); t.conditions = rules
    return TE.conditions_hold(t, ctx)
print("\nconditions:")
for rules, expected in [
    ([], True),
    ([{"token":"asset_category","op":"eq","value":"Laptop"}], True),
    ([{"token":"asset_category","op":"eq","value":"Phone"}], False),
    ([{"token":"asset_category","op":"contains","value":"lap"}], True),
    ([{"token":"asset_tag","op":"filled","value":""}], True),
    ([{"token":"justification","op":"empty","value":""}], False),
    ([{"token":"asset_category","op":"eq","value":"Laptop"},
      {"token":"issued_by","op":"eq","value":"Nobody"}], False),
    ([{"token":"nosuch","op":"eq","value":"x"}], False),
]:
    got = holds(rules)
    mark = "ok " if got == expected else "FAIL"
    print(f"   {mark} {str(rules)[:70]:72} -> {got}")
    assert got == expected, rules

# the PDF of the started form must render with the carried-over answers
pdf, _ = F.render_form_pdf(schema, values, reference="HAND-20260919", submitter="Baboucarr Foon")
print("\nhandover pdf:", len(pdf), "bytes,", pdf[:4])
assert pdf[:4] == b"%PDF"
print("\nTRIGGER ENGINE OK")
