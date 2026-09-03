"""
accounts/room_access.py
=======================

Who can see and book which room.

The problem
-----------
`Room` had no owner at all. `RoomListView` ran

    Room.objects.filter(is_active=True)

so every room in the database appeared to every signed-in user. With one agency
in one compound that was harmless. With several country offices it means UNDP
Senegal sees UNICEF Gambia's boardroom and can book it.

Two comments already in `views_room_booking.py` acknowledge the gap:

    # Optional: restrict rooms per agency if you have agency field
    # if hasattr(Room, "agency_id") and request.user.agency_id:

This module is that field, plus the sharing rules a UN compound actually needs.

The three levels
----------------
``office``   Only the owning agency's own country office. A UNDP Gambia
             meeting room that UNDP Gambia staff book among themselves.

``agency``   Every office of the owning agency. Useful for a regional hub
             room that any office in the agency may request.

``country``  Every agency that has an office in the same country. This is the
             common-premises case: a shared compound conference room that
             UNDP, UNICEF and WFP all book, because they share the building.

``country`` is matched on the office's ``country`` field, compared
case-insensitively and ignoring surrounding spaces, because "The Gambia" and
"the gambia " are the same place and someone will eventually type both.

Rooms with no owner
-------------------
A room whose ``owner_office`` is empty stays visible to everyone, exactly as
before. That is deliberate: adding this field must not make every existing
room vanish from every list the moment you migrate. Set
``ROOMS_HIDE_UNOWNED = True`` once you have assigned owners, and an unowned
room becomes superuser-only.
"""

from django.conf import settings
from django.db.models import Q

#: While False, a room with no owning office behaves as it always has —
#: visible to everyone. Flip it once every room has an owner.
HIDE_UNOWNED = getattr(settings, "ROOMS_HIDE_UNOWNED", False)

VISIBILITY_OFFICE = "office"
VISIBILITY_AGENCY = "agency"
VISIBILITY_COUNTRY = "country"

VISIBILITY_CHOICES = (
    (VISIBILITY_OFFICE, "This country office only"),
    (VISIBILITY_AGENCY, "All offices of this agency"),
    (VISIBILITY_COUNTRY, "All agencies in this country (shared compound)"),
)

VISIBILITY_HELP = {
    VISIBILITY_OFFICE: "Only staff in the owning country office can see or book it.",
    VISIBILITY_AGENCY: "Any office of the owning agency can see and book it.",
    VISIBILITY_COUNTRY: "Every agency with an office in the same country can see and book it.",
}


# ---------------------------------------------------------------------------
# Reading the viewer's position
# ---------------------------------------------------------------------------

def viewer_scope(user):
    """
    (agency_id, office_id, country) for a user. Any part may be None.

    The country comes from the user's office, so a user with no office has no
    country and therefore cannot reach country-shared rooms — which is correct:
    we do not know which compound they are in.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None, None, None

    office = getattr(user, "country_office", None)
    office_id = getattr(user, "country_office_id", None)
    agency_id = getattr(user, "agency_id", None)

    if agency_id is None and office is not None:
        agency_id = getattr(office, "agency_id", None)

    country = ""
    if office is not None:
        country = (getattr(office, "country", "") or "").strip()

    return agency_id, office_id, country


def _country_office_ids(country: str):
    """Every active office in this country, across all agencies."""
    if not country:
        return []
    from tenancy.models import CountryOffice

    return list(
        CountryOffice.objects.filter(country__iexact=country.strip(), is_active=True)
        .values_list("pk", flat=True)
    )


# ---------------------------------------------------------------------------
# The queryset
# ---------------------------------------------------------------------------

def visible_rooms(user, base=None):
    """
    Rooms this user may see and book.

    Use anywhere you would previously have written
    ``Room.objects.filter(is_active=True)``.
    """
    from .models import Room

    qs = base if base is not None else Room.objects.all()

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()
    if getattr(user, "is_superuser", False):
        return qs

    agency_id, office_id, country = viewer_scope(user)

    # Rooms with no owner: legacy data, everyone's until you say otherwise.
    q = Q(owner_office__isnull=True) if not HIDE_UNOWNED else Q(pk__in=[])

    if office_id:
        # Own office, whatever the room's visibility says.
        q |= Q(owner_office_id=office_id)

    if agency_id:
        # Any office of my agency, when the room is shared that widely.
        q |= Q(visibility=VISIBILITY_AGENCY, owner_office__agency_id=agency_id)

    if country:
        # Any agency in my country, when the room is shared compound-wide.
        sibling_offices = _country_office_ids(country)
        if sibling_offices:
            q |= Q(visibility=VISIBILITY_COUNTRY, owner_office_id__in=sibling_offices)

    return qs.filter(q).distinct()


def can_view_room(user, room) -> bool:
    """Single-room check, for detail views and booking validation."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True

    owner_office_id = getattr(room, "owner_office_id", None)
    if owner_office_id is None:
        return not HIDE_UNOWNED

    agency_id, office_id, country = viewer_scope(user)

    if office_id and owner_office_id == office_id:
        return True

    visibility = getattr(room, "visibility", VISIBILITY_OFFICE)

    if visibility == VISIBILITY_AGENCY and agency_id:
        owner = getattr(room, "owner_office", None)
        return bool(owner and owner.agency_id == agency_id)

    if visibility == VISIBILITY_COUNTRY and country:
        owner = getattr(room, "owner_office", None)
        owner_country = (getattr(owner, "country", "") or "").strip()
        return bool(owner_country and owner_country.lower() == country.lower())

    return False


def can_manage_room(user, room=None) -> bool:
    """
    Who may create or edit a room.

    Superusers anywhere; a main admin for rooms owned by their own office.
    The existing views are superuser-only, so this widens nothing unless you
    choose to use it.
    """
    if getattr(user, "is_superuser", False):
        return True
    try:
        from tenancy.services import is_main_admin
    except ImportError:
        return False
    if not is_main_admin(user):
        return False
    if room is None:
        return True
    return getattr(room, "owner_office_id", None) == getattr(user, "country_office_id", None)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def sharing_label(room) -> str:
    """A short phrase for the room card, e.g. 'Shared · The Gambia'."""
    owner = getattr(room, "owner_office", None)
    if owner is None:
        return "Unassigned"

    visibility = getattr(room, "visibility", VISIBILITY_OFFICE)
    if visibility == VISIBILITY_COUNTRY:
        country = (getattr(owner, "country", "") or "").strip()
        return f"Shared · {country}" if country else "Shared · all agencies"
    if visibility == VISIBILITY_AGENCY:
        return f"{owner.agency.code} · all offices"
    return str(owner)


def annotate_sharing(rooms, user):
    """
    Tag each room with what the viewer needs to know about it, without a
    template tag or a model method: is it ours, and how widely is it shared.
    """
    _, office_id, _ = viewer_scope(user)
    for room in rooms:
        room.sharing_label = sharing_label(room)
        room.is_own_office = (
            getattr(room, "owner_office_id", None) is not None
            and room.owner_office_id == office_id
        )
        room.is_shared_in = (
            getattr(room, "visibility", VISIBILITY_OFFICE) == VISIBILITY_COUNTRY
            and not room.is_own_office
        )
    return rooms


# ---------------------------------------------------------------------------
# Putting the fields on the form
# ---------------------------------------------------------------------------
#
# These are attached by the view rather than declared in RoomForm.
#
# The first version of this change asked you to add three names to
# RoomForm.Meta.fields by hand. That is a step it is easy to miss — and if you
# miss it the model has the columns, the views scope on them, and the edit page
# simply shows no way to set them. Doing it here means the section appears on
# create and edit whether or not forms.py knows anything about it.

def attach_room_scope_fields(form, user):
    """Add owner_office, visibility and shared_note to a RoomForm."""
    from django import forms as dj_forms

    try:
        from tenancy.models import CountryOffice as _CountryOffice
    except ImportError:
        return form

    model = getattr(getattr(form, "_meta", None), "model", None)
    if model is None:
        return form

    model_fields = {f.name for f in model._meta.get_fields()}
    if "owner_office" not in model_fields:
        # Migration not run yet — leave the form alone rather than offering a
        # field that cannot be saved.
        return form

    instance = getattr(form, "instance", None)

    # Narrow the office list to what this person may assign. Done whether the
    # field was declared in RoomForm or is being added here, so a form that
    # already has the field does not quietly offer every office in the system.
    offices = _CountryOffice.objects.filter(is_active=True).select_related("agency")
    if not getattr(user, "is_superuser", False):
        own = getattr(user, "country_office_id", None)
        current = getattr(instance, "owner_office_id", None)
        keep = [pk for pk in (own, current) if pk]
        offices = offices.filter(pk__in=keep) if keep else offices.none()
    offices = offices.order_by("agency__code", "name")

    if "owner_office" in form.fields:
        form.fields["owner_office"].queryset = offices
    else:
        form.fields["owner_office"] = dj_forms.ModelChoiceField(
            queryset=offices,
            required=False,
            label="Owning country office",
            empty_label="— no owner (visible to everyone) —",
            initial=getattr(instance, "owner_office_id", None),
            widget=dj_forms.Select(attrs={"class": "form-select"}),
        )

    # Always build this one here rather than trusting whatever the ModelForm
    # produced.
    #
    # If `Room.visibility` was added to models.py without `choices=`, Django
    # generates a plain CharField for it. Rendered through a Select widget that
    # is an empty `<select></select>` — a dropdown with nothing in it and no way
    # to set the value. The giveaway is `maxlength="10"` on the select, which a
    # real ChoiceField would never emit.
    #
    # Declaring the choices here means the field works whether or not the model
    # carries them, and the model and form can never drift apart.
    existing = form.fields.get("visibility")
    current = getattr(instance, "visibility", None) or VISIBILITY_OFFICE
    attrs = {"class": "form-select"}
    if existing is not None and getattr(existing, "widget", None) is not None:
        attrs = {**getattr(existing.widget, "attrs", {}), **attrs}
        attrs.pop("maxlength", None)   # meaningless on a <select>

    form.fields["visibility"] = dj_forms.ChoiceField(
        choices=VISIBILITY_CHOICES,
        required=False,
        label="Visibility",
        initial=current,
        help_text="Who may see this room in their list and book it.",
        widget=dj_forms.Select(attrs=attrs),
    )
    # An unbound form takes its value from `initial`; a bound one re-reads the
    # POST, so this only affects the first render.
    if not form.is_bound:
        form.initial.setdefault("visibility", current)

    if "shared_note" not in form.fields and "shared_note" in model_fields:
        form.fields["shared_note"] = dj_forms.CharField(
            required=False, max_length=200,
            label="Note for other agencies",
            initial=getattr(instance, "shared_note", "") or "",
            widget=dj_forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Anything another agency needs to know before booking",
            }),
        )

    return form


def apply_room_scope(form, user):
    """
    Copy the three values onto the instance before it is saved.

    Needed because fields added after the ModelForm was built are not part of
    Meta.fields, so ModelForm.save() ignores them.

    A non-superuser may only assign their own office, whatever the POST says.
    """
    data = getattr(form, "cleaned_data", None)
    if not data:
        return

    instance = form.instance
    if not hasattr(instance, "visibility"):
        return

    if "owner_office" in data:
        chosen = data.get("owner_office")
        if chosen is not None and not getattr(user, "is_superuser", False):
            own_id = getattr(user, "country_office_id", None)
            if own_id and chosen.pk != own_id:
                chosen = getattr(user, "country_office", None)
        instance.owner_office = chosen

    if data.get("visibility"):
        instance.visibility = data["visibility"]

    if "shared_note" in data and hasattr(instance, "shared_note"):
        note = (data.get("shared_note") or "").strip()
        # The note is only ever read by another agency, so drop it when the
        # room is not shared that widely — otherwise it lingers invisibly and
        # reappears if someone re-shares the room later.
        instance.shared_note = note if instance.visibility == VISIBILITY_COUNTRY else ""