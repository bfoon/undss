"""
tenancy/scoping.py
==================

Feature switches decide *whether* a module appears. This module decides *whose
records* a person sees inside it.

Why you need this
-----------------
`Visitor`, `Vehicle`, `Key`, `ParkingCard`, `AssetExit` and `Package` currently
carry no agency or country-office field, and their list views run
`Model.objects.all()`. So the moment a second country office exists, both
offices with `visitor_access` on will read one shared visitor list. Turning the
module off per office does not fix that — an office either sees everything or
nothing.

`PackageFlowTemplate` is the exception: it already has a real `agency` FK and
its views filter on it.

How to use it
-------------
Add the mixin to a model, run a migration, backfill, and the manager does the
rest:

    from tenancy.scoping import OfficeOwnedModel, OfficeScopedManager

    class Visitor(OfficeOwnedModel):
        ...
        objects = OfficeScopedManager()

Then in the view:

    qs = Visitor.objects.for_user(self.request.user)

`for_user` returns records belonging to the caller's office. Superusers get
everything. Records with no office set are treated as unclaimed and are visible
to every office until you backfill them — deliberately, so adding the field
cannot make live data vanish mid-shift.

Stamping new records
--------------------
    from tenancy.scoping import stamp_office

    visitor = form.save(commit=False)
    stamp_office(visitor, request.user)
    visitor.save()

Or add `OfficeStampMixin` to a CreateView and it happens in `form_valid`.
"""

from django.conf import settings
from django.db import models

#: When True, rows whose country_office is NULL stay visible to everyone. Keep
#: it True while backfilling; flip it to False once every row is stamped, so
#: an unstamped record can never leak into the wrong office.
SHOW_UNSCOPED = getattr(settings, "TENANCY_SHOW_UNSCOPED_RECORDS", True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def office_of(user):
    """The user's CountryOffice, or None."""
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "country_office", None)


def office_id_of(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "country_office_id", None)


def stamp_office(instance, user, field="country_office"):
    """
    Set the office on a new record from the user creating it, unless it is
    already set. Returns the instance so it can be chained.
    """
    if getattr(instance, f"{field}_id", None):
        return instance
    office_id = office_id_of(user)
    if office_id:
        setattr(instance, f"{field}_id", office_id)
    return instance


def same_office(user, instance, field="country_office") -> bool:
    """True if the record belongs to the user's office (or is unclaimed)."""
    if getattr(user, "is_superuser", False):
        return True
    record_office = getattr(instance, f"{field}_id", None)
    if record_office is None:
        return SHOW_UNSCOPED
    return record_office == office_id_of(user)


# ---------------------------------------------------------------------------
# Queryset / manager
# ---------------------------------------------------------------------------

class OfficeScopedQuerySet(models.QuerySet):

    def for_office(self, office_id, field="country_office"):
        if not office_id:
            return self.none()
        q = models.Q(**{f"{field}_id": office_id})
        if SHOW_UNSCOPED:
            q |= models.Q(**{f"{field}__isnull": True})
        return self.filter(q)

    def for_user(self, user, field="country_office"):
        """
        Records the user's office owns. Superusers see everything; users with
        no office see only unclaimed records.
        """
        if getattr(user, "is_superuser", False):
            return self
        office_id = office_id_of(user)
        if not office_id:
            if SHOW_UNSCOPED:
                return self.filter(**{f"{field}__isnull": True})
            return self.none()
        return self.for_office(office_id, field)

    def unclaimed(self, field="country_office"):
        """Rows still waiting to be backfilled. Handy for checking progress."""
        return self.filter(**{f"{field}__isnull": True})


class OfficeScopedManager(models.Manager.from_queryset(OfficeScopedQuerySet)):
    """Drop-in replacement for the default manager. `.all()` is unchanged."""
    pass


# ---------------------------------------------------------------------------
# Abstract model
# ---------------------------------------------------------------------------

class OfficeOwnedModel(models.Model):
    """
    Adds `country_office` to a model. Nullable on purpose: existing rows keep
    working, and the backfill command fills them in afterwards.
    """

    country_office = models.ForeignKey(
        "tenancy.CountryOffice",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="%(app_label)s_%(class)s_records",
        db_index=True,
        help_text="Country office that owns this record.",
    )

    objects = OfficeScopedManager()

    class Meta:
        abstract = True

    @property
    def owning_office(self):
        return self.country_office


# ---------------------------------------------------------------------------
# View mixins
# ---------------------------------------------------------------------------

class OfficeStampMixin:
    """
    For CreateViews. Stamps the caller's office onto the new object.

        class VisitorCreateView(OfficeStampMixin, CreateView):
            ...
    """

    office_field = "country_office"

    def form_valid(self, form):
        stamp_office(form.instance, self.request.user, self.office_field)
        return super().form_valid(form)


class OfficeFilterMixin:
    """
    For ListViews and DetailViews on a model using OfficeScopedManager.

        class VisitorListView(OfficeFilterMixin, ListView):
            ...

    Works whether or not the view already overrides get_queryset — it filters
    whatever the parent returned, so existing search and status filters survive.
    """

    office_field = "country_office"

    def get_queryset(self):
        qs = super().get_queryset()
        for_user = getattr(qs, "for_user", None)
        if for_user is None:
            # Model is not using OfficeScopedManager; filter by hand.
            if getattr(self.request.user, "is_superuser", False):
                return qs
            office_id = office_id_of(self.request.user)
            if not office_id:
                return qs.none() if not SHOW_UNSCOPED else qs.filter(
                    **{f"{self.office_field}__isnull": True}
                )
            q = models.Q(**{f"{self.office_field}_id": office_id})
            if SHOW_UNSCOPED:
                q |= models.Q(**{f"{self.office_field}__isnull": True})
            return qs.filter(q)
        return for_user(self.request.user, self.office_field)
