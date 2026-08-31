# UN PASS — per-office module switches

Adds a tenancy and entitlements layer to UN PASS so you, as superuser, can turn
any module on or off for one agency or one country office without touching the
others. An office that only wants the ICT console and eSign gets exactly that;
an office that only wants room booking gets exactly that.

Everything below was written against your uploaded `accounts`, `incidents`,
`comms` and `dashboard` code and verified on Django 4.2 with a 45-case
functional test.

---

## What you get

| Need | Where it lives |
|---|---|
| Turn any module on/off per agency | `FeatureGrant` scoped to `Agency` |
| Turn any module on/off per country office | `FeatureGrant` scoped to `CountryOffice` |
| Same agency, several country offices | `CountryOffice` + `User.country_office` |
| Main admins who create sub admins | `OfficeAdmin` with `level` main/sub |
| Sub admins who manage users in their CO | `services.manageable_users()` |
| Link COs or agencies inside one module | `DirectoryShare` + `services.visible_users_for()` |
| Room for Microsoft SSO later | `SSOConfiguration` + `tenancy/sso.py` |
| Keep one office's records out of another's | `tenancy/scoping.py` |
| Feature checks in signals and tasks | `services.office_has_feature()` |
| A record of who changed what | `FeatureAuditLog` |

The catalogue of switchable modules is one file: `tenancy/catalog.py`. Add an
entry there and it appears in the console, the template context, the middleware
gate and the admin — nothing else to edit.

---

## Install

### 1. Drop the app in

Copy the `tenancy/` folder next to your other apps.

### 2. Settings

```python
INSTALLED_APPS = [
    ...
    "accounts",
    "tenancy",
]

MIDDLEWARE = [
    ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "tenancy.middleware.FeatureGateMiddleware",   # after auth
    ...
]

TEMPLATES = [{
    ...
    "OPTIONS": {"context_processors": [
        ...
        "tenancy.context_processors.tenancy",
    ]},
}]

# Optional
TENANCY_CACHE_TTL = 300          # seconds to cache a resolved feature set
TENANCY_SUPERUSER_BYPASS = True  # False makes you see what an office really has
```

### 3. One field on your User model

In `accounts/models.py`, inside `class User(AbstractUser)`, next to the existing
`agency` field:

```python
    country_office = models.ForeignKey(
        "tenancy.CountryOffice",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="users",
        help_text="Country office this user belongs to",
    )
```

This is the only change to an existing file. Everything else is additive.

### 4. Project URLs

```python
path("platform/", include("tenancy.urls", namespace="tenancy")),
```

### 5. Migrate and bootstrap

```bash
python manage.py makemigrations tenancy accounts
python manage.py migrate

python manage.py bootstrap_tenancy --dry-run          # see what would happen
python manage.py bootstrap_tenancy --preserve-current # then do it
```

`bootstrap_tenancy` creates a default country office per agency, assigns every
user to one, copies your existing `AgencyServiceConfig.asset_mgmt_enabled` and
`esign_enabled` into the new system, and switches on a starter module set so an
existing deployment does not go dark. It is idempotent — safe to re-run.

Then open `/platform/`.

---

## How resolution works

Highest wins:

1. A grant on the user's **country office**
2. A grant on the user's **agency**
3. `default_enabled` in the catalogue

A feature also stays off if any of its `requires` parents are off — so
`consumables` cannot be on while `asset_mgmt` is off, and switching a parent off
cascades its children off.

```python
from tenancy.services import has_feature

has_feature(request.user, "esign")   # -> bool
```

---

## Using it in code

### Function views

```python
from tenancy.decorators import feature_required

@login_required
@feature_required("esign")
def esign_dashboard(request):
    ...

@feature_required("room_booking", "room_attendance")          # both
@feature_required("asset_mgmt", "consumables", mode="any")    # either
```

### Class-based views

```python
from tenancy.mixins import FeatureRequiredMixin

class RoomListView(FeatureRequiredMixin, ListView):
    required_features = ["room_booking"]
```

### Templates

The context processor gives you a `features` dict everywhere:

```django
{% if features.esign %}
  <li><a href="{% url 'accounts:esign_dashboard' %}">eSign</a></li>
{% endif %}

{% if features.room_booking and features.room_attendance %} ... {% endif %}
```

Your current `base.html` keeps working unchanged — `asset_mgmt_enabled` and
`esign_enabled` are still injected. Replace them with `features.asset_mgmt` and
`features.esign` when convenient.

For templates outside the request context:

```django
{% load feature_tags %}
{% feature_on "mailroom" as can_mail %}
{{ some_other_user|has_feature:"esign" }}
```

### The middleware safety net

You have several hundred URLs. Decorating them one at a time is a long job, so
`FeatureGateMiddleware` blocks by URL name using the `url_rules` in the
catalogue — matched as `"<namespace>:<url_name>"`, so it does not care where
each app is mounted.

Anything matching no rule is allowed through. The gate never blocks by accident;
it only ever blocks what you have explicitly listed. Check the `url_rules` in
`catalog.py` against your real URL names and adjust — I inferred them from your
`urls.py` files, and any name I guessed wrong simply falls through unguarded
rather than breaking.

Decorators are still worth adding to sensitive views. Use both.

---

## Country offices and admins

**Superuser** — `/platform/offices/` creates offices, `/platform/features/<scope>/`
switches modules, and `/platform/offices/<pk>/admins/` appoints **main admins**.

**Main admin** — appoints and revokes **sub admins** in their own office, manages
every user there, and may flip the modules the catalogue marks `delegable=True`
(currently the sub-modules: `room_attendance`, `asset_exit`, `consumables`,
`esign_markup`).

**Sub admin** — manages ordinary users in their office. Cannot touch other
admins, cannot reach another office, cannot change modules.

To wire this into your existing ICT views, replace the agency filter in
`views_ict.py`:

```python
# before
qs = qs.filter(agency_id=user.agency_id)

# after
from tenancy.services import manageable_users, can_manage_user
qs = manageable_users(user, qs)
```

and in `ICTUserAccessMixin.get_object`:

```python
if not can_manage_user(self.request.user, obj):
    raise Http404("User not found in your office.")
```

---

## Linking offices so they can see each other

`/platform/sharing/` links two scopes inside one module. A link is per-module —
linking UNDP Gambia and UNICEF Gambia on `esign` does not let them see each
other anywhere else.

```python
from tenancy.services import visible_users_for

recipients = visible_users_for(request.user, "esign")
```

Use that anywhere you currently build a user dropdown: eSign recipients, asset
assignees, CSR fulfillers. Only modules marked `shareable=True` in the catalogue
can be linked — currently `esign` and `asset_mgmt`. Add the flag to any other
module you want linkable.

Without a link, a user sees only their own country office.

---

## Microsoft SSO

`SSOConfiguration` stores Entra ID settings per agency or per office, and
`/platform/sso/` is the page to fill them in. The three OIDC URLs are reserved
and named now, so the redirect URI you register in Entra today stays valid when
the flow goes live.

The token exchange is deliberately not built. `tenancy/sso.py` contains the
seven-step list of what remains, and every step reads configuration that already
exists — no schema change needed to finish it. Until then `sso_start` sends
people back to password login with a clear message rather than failing oddly.

`enforce_sso` and `bypass_for_superusers` are stored and ready for when you wire
the login template.

---

## Data scoping — read this before you open a second office

The switches decide *whether* a module appears. They do not decide *whose
records* a person sees inside it.

`Visitor`, `Vehicle`, `Key`, `ParkingCard`, `AssetExit` and `Package` carry no
agency or country-office field, and their list views run `Model.objects.all()`.
The moment UNDP Gambia and UNDP Senegal both have `visitor_access` on, they read
one shared visitor list. Switching the module off per office does not help — an
office either sees everything or nothing.

(`PackageFlowTemplate` is the exception. It already has a real `agency` FK and
its views filter on it.)

`tenancy/scoping.py` closes the gap. Per model, three steps:

```python
# 1. visitors/models.py
from tenancy.scoping import OfficeOwnedModel, OfficeScopedManager

class Visitor(OfficeOwnedModel):        # was models.Model
    ...
    objects = OfficeScopedManager()
```

```python
# 2. visitors/views.py
from tenancy.scoping import OfficeFilterMixin, OfficeStampMixin

class VisitorListView(OfficeFilterMixin, LoginRequiredMixin, ListView):
    ...                                  # your search and status filters survive

class VisitorCreateView(OfficeStampMixin, LoginRequiredMixin, CreateView):
    ...                                  # new records get the creator's office
```

```bash
# 3. backfill the rows that predate the field
python manage.py makemigrations visitors
python manage.py migrate
python manage.py backfill_office --dry-run
python manage.py backfill_office
```

`backfill_office` works out each record's office from whoever created it —
`registered_by` for visitors, `logged_by` for packages, `requester` for asset
exits, and so on. `Vehicle` and `Key` have no user field, so it falls back to
their most recent movement or key log; anything still unresolved can be assigned
with `--office <pk>`.

While you migrate, rows with no office stay visible to everyone. That is
deliberate — adding the field cannot make live data disappear mid-shift. Once
`Model.objects.unclaimed().count()` is zero everywhere, set:

```python
TENANCY_SHOW_UNSCOPED_RECORDS = False
```

and an unstamped record can never leak into the wrong office again.

For detail views and POST handlers, guard the single object:

```python
from tenancy.scoping import same_office

if not same_office(request.user, visitor):
    raise Http404
```

---

## Feature checks outside a request

`visitors/signals.py` connects to `MeetingAttendee` globally. Once a second
office exists, an office with room booking on but meeting-linked visitors off
would still get group members written into its visitor records every time an
attendee is accepted. A URL gate cannot catch that — the signal has no request.

`services.office_has_feature(office, code)` is the check for signals, management
commands and Celery tasks:

```python
from tenancy.services import office_has_feature

if office_has_feature(visitor.registered_by.country_office, "visitor_meeting_link"):
    visitor.sync_members_from_booking()
```

`patches/visitors_signals.py` is a drop-in replacement for your current
`visitors/signals.py` with that guard in place. It fails open when tenancy is not
installed, so it is safe to deploy before or after the app.

---

## Two corrections from the first pass

**Asset exit no longer requires asset management.** I had `asset_exit` as a child
of `asset_mgmt`. Reading `vehicles/models.py`, `AssetExit` works off a free-text
`agency_name` and an item list, with no link to the asset register at all — it is
a guard and LSA gate-pass workflow. It is now an independent module under
Security, and `accounts:exit_organization` (the separate staff-separation
clearance, which *does* read held assets) became `exit_clearance` under ICT.

**URL rules were in the wrong namespace.** Visitor, vehicle, parking, key,
mailroom and asset-exit URLs live in the `visitors` and `vehicles` apps, not
`accounts`. All rules are now taken from your real `urls.py` files. The one
namespace I still have not seen is `accounts:hr_*` / `id_card_*` for employee ID
cards — run the coverage check below to confirm those.

---

## Checking rule coverage

Run this once after installing to see which URL names the gate does not cover:

```python
# python manage.py shell
from django.urls import get_resolver
from fnmatch import fnmatch
from tenancy.catalog import url_rule_index

rules = url_rule_index()
for key in get_resolver().reverse_dict.keys():
    if not isinstance(key, str):
        continue
    if not any(fnmatch(key, p) for p, _ in rules):
        print("ungated:", key)
```

Anything printed is a URL the middleware will not gate. Add a pattern to the
right feature in `catalog.py`, or decorate the view directly. Where two patterns
both match a name, the longer one wins — that is what keeps
`vehicles:package_flow_*` out of the hands of the `mailroom` switch.

---

## Files

```
tenancy/
  catalog.py                the module catalogue — start here
  models.py                 CountryOffice, FeatureGrant, OfficeAdmin,
                            DirectoryShare, SSOConfiguration, FeatureAuditLog
  services.py               resolution, caching, sharing, permissions
  scoping.py                office-level data scoping for module records
  decorators.py             @feature_required, @main_admin_required
  mixins.py                 FeatureRequiredMixin, OfficeScopedQuerysetMixin
  middleware.py             FeatureGateMiddleware
  context_processors.py     the `features` dict
  templatetags/feature_tags.py
  views.py                  superuser console
  urls.py
  sso.py                    Entra ID stubs and the remaining steps
  admin.py
  signals.py                cache invalidation
  apps.py
  management/commands/bootstrap_tenancy.py
  management/commands/backfill_office.py
  templates/tenancy/*.html  console pages and the friendly 403
```
