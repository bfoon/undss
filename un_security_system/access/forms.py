# access/forms.py
from datetime import datetime, time, timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .models import AccessGroup, AccessRequest, AccessRole

ICONS = ["bi-person-badge", "bi-shield-check", "bi-shield-lock", "bi-briefcase", "bi-building-gear",
         "bi-clipboard-check", "bi-cash-coin", "bi-truck", "bi-tools", "bi-pc-display", "bi-journal-text",
         "bi-box-seam", "bi-people", "bi-star", "bi-award", "bi-key", "bi-broadcast", "bi-heart-pulse",
         "bi-mortarboard", "bi-gear"]
COLORS = ["#0F766E", "#1D4ED8", "#7C3AED", "#DB2777", "#DC2626", "#EA580C", "#CA8A04", "#16A34A",
          "#0EA5E9", "#475569"]


def _c(extra=""):
    return f"form-control {extra}".strip()


def visible_roles(user):
    qs = AccessRole.objects.filter(is_active=True)
    if user.is_superuser:
        return qs
    q = Q(agency__isnull=True)
    if getattr(user, "agency_id", None):
        q |= Q(agency_id=user.agency_id, country_office__isnull=True)
        if getattr(user, "country_office_id", None):
            q |= Q(agency_id=user.agency_id, country_office_id=user.country_office_id)
    return qs.filter(q)


def visible_groups(user):
    qs = AccessGroup.objects.filter(is_active=True)
    if user.is_superuser:
        return qs
    q = Q(agency__isnull=True)
    if getattr(user, "agency_id", None):
        q |= Q(agency_id=user.agency_id)
    return qs.filter(q)


def people_for(user):
    User = get_user_model()
    qs = User._base_manager.filter(is_active=True)
    if not user.is_superuser and getattr(user, "agency_id", None):
        qs = qs.filter(agency_id=user.agency_id)
    return qs.order_by("first_name", "last_name", "username")


def _end_of(day):
    if not day:
        return None
    return timezone.make_aware(datetime.combine(day, time(23, 59)))


class PersonChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, u):
        name = u.get_full_name() or u.username
        return f"{name} — {u.email}" if u.email else name


class RoleForm(forms.ModelForm):
    only_my_office = forms.BooleanField(required=False, label="Only for my country office")
    shared = forms.BooleanField(required=False, label="Shared role (all agencies)")

    class Meta:
        model = AccessRole
        fields = ["name", "description", "icon", "color", "inherits", "requestable", "max_days", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": _c(), "placeholder": "e.g. Operations Manager"}),
            "description": forms.Textarea(attrs={"class": _c(), "rows": 2,
                                                 "placeholder": "What this person is trusted to do"}),
            "icon": forms.HiddenInput(),
            "color": forms.HiddenInput(),
            "inherits": forms.SelectMultiple(attrs={"class": "form-select", "size": 5}),
            "requestable": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "max_days": forms.NumberInput(attrs={"class": _c(), "min": 1, "max": 3650, "placeholder": "No limit"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {"inherits": "Includes these roles", "requestable": "Staff can request it",
                  "max_days": "Time-box (days)"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        qs = visible_roles(user)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["inherits"].queryset = qs
        self.fields["inherits"].required = False
        if not user.is_superuser:
            del self.fields["shared"]
        elif self.instance.pk:
            self.fields["shared"].initial = self.instance.agency_id is None
        if self.instance.pk:
            self.fields["only_my_office"].initial = bool(self.instance.country_office_id)
        if self.instance.is_system:
            self.fields.pop("shared", None)
            self.fields.pop("only_my_office", None)


class GroupForm(forms.ModelForm):
    office_only = forms.BooleanField(required=False, initial=True, label="Only for my country office")
    rule_unit = forms.ModelChoiceField(queryset=None, required=False, label="Everyone in unit",
                                       widget=forms.Select(attrs={"class": "form-select"}))
    rule_base_role = forms.ChoiceField(required=False, label="Everyone whose base role is",
                                       widget=forms.Select(attrs={"class": "form-select"}))
    rule_whole_office = forms.BooleanField(required=False, label="Everyone in my country office")

    class Meta:
        model = AccessGroup
        fields = ["name", "description", "icon", "color", "roles", "members"]
        widgets = {
            "name": forms.TextInput(attrs={"class": _c(), "placeholder": "e.g. Procurement Committee"}),
            "description": forms.Textarea(attrs={"class": _c(), "rows": 2}),
            "icon": forms.HiddenInput(),
            "color": forms.HiddenInput(),
            "roles": forms.SelectMultiple(attrs={"class": "form-select", "size": 6}),
            "members": forms.SelectMultiple(attrs={"class": "form-select", "size": 10, "id": "memberSelect"}),
        }
        labels = {"roles": "Roles every member holds", "members": "Members (hand-picked)"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import Unit

        User = get_user_model()
        self.user = user
        self.fields["roles"].queryset = visible_roles(user)
        self.fields["roles"].required = False
        self.fields["members"].queryset = people_for(user)
        self.fields["members"].required = False
        self.fields["members"].label_from_instance = lambda u: (u.get_full_name() or u.username)
        units = Unit.objects.all()
        if not user.is_superuser and getattr(user, "agency_id", None) and hasattr(Unit, "agency"):
            units = units.filter(agency_id=user.agency_id)
        self.fields["rule_unit"].queryset = units
        self.fields["rule_base_role"].choices = [("", "— any —")] + list(getattr(User, "ROLE_CHOICES", []))
        rule = self.instance.rule or {} if self.instance.pk else {}
        if self.instance.pk:
            self.fields["office_only"].initial = bool(self.instance.country_office_id)
            self.fields["rule_unit"].initial = rule.get("unit")
            self.fields["rule_base_role"].initial = rule.get("base_role", "")
            self.fields["rule_whole_office"].initial = bool(rule.get("office"))

    def build_rule(self):
        cd, user = self.cleaned_data, self.user
        rule = {}
        if cd.get("rule_unit"):
            rule["unit"] = cd["rule_unit"].pk
        if cd.get("rule_base_role"):
            rule["base_role"] = cd["rule_base_role"]
        if cd.get("rule_whole_office") and getattr(user, "country_office_id", None):
            rule["office"] = user.country_office_id
        if rule and getattr(user, "agency_id", None) and not user.is_superuser:
            rule["agency"] = user.agency_id  # a smart group never reaches outside the agency
        return rule


class AssignForm(forms.Form):
    target = forms.ChoiceField(choices=[("user", "A person"), ("group", "A group")], initial="user",
                               widget=forms.RadioSelect)
    user = PersonChoiceField(queryset=None, required=False, widget=forms.Select(attrs={"class": "form-select"}))
    group = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs={"class": "form-select"}))
    scope = forms.ChoiceField(widget=forms.Select(attrs={"class": "form-select"}))
    ends_on = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date", "class": _c()}),
                              label="Ends on")
    reason = forms.CharField(required=False, max_length=255,
                             widget=forms.TextInput(attrs={"class": _c(), "placeholder": "Why (kept in the audit log)"}))

    def __init__(self, *args, actor=None, role=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor, self.role = actor, role
        self.fields["user"].queryset = people_for(actor)
        self.fields["group"].queryset = visible_groups(actor)
        choices = []
        if getattr(actor, "country_office_id", None):
            choices.append(("office", f"My office — {actor.country_office}"))
        if getattr(actor, "agency_id", None):
            choices.append(("agency", f"My agency — {actor.agency}"))
        if actor.is_superuser:
            choices.append(("global", "Everywhere"))
        self.fields["scope"].choices = choices or [("global", "Everywhere")]
        if role and role.max_days:
            self.fields["ends_on"].help_text = f"This role is time-boxed to {role.max_days} days."

    def clean(self):
        cd = super().clean()
        if cd.get("target") == "user" and not cd.get("user"):
            self.add_error("user", "Choose a person.")
        if cd.get("target") == "group" and not cd.get("group"):
            self.add_error("group", "Choose a group.")
        if cd.get("ends_on") and cd["ends_on"] < timezone.localdate():
            self.add_error("ends_on", "Choose a date from today on.")
        cd["ends_at"] = _end_of(cd.get("ends_on"))
        return cd


class DelegateForm(forms.Form):
    to_user = PersonChoiceField(queryset=None, label="Who covers for you",
                                widget=forms.Select(attrs={"class": "form-select"}))
    roles = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, label="Roles to hand over")
    until = forms.DateField(widget=forms.DateInput(attrs={"type": "date", "class": _c()}), label="Until (inclusive)")
    reason = forms.CharField(required=False, max_length=255,
                             widget=forms.TextInput(attrs={"class": _c(), "placeholder": "e.g. Annual leave"}))

    def __init__(self, *args, actor=None, delegable=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["to_user"].queryset = people_for(actor).exclude(pk=actor.pk)
        self.fields["roles"].choices = [(str(r.pk), r.name) for r, _s in delegable]
        today = timezone.localdate()
        self.fields["until"].widget.attrs.update({"min": today.isoformat(),
                                                  "max": (today + timedelta(days=30)).isoformat()})

    def clean_until(self):
        d = self.cleaned_data["until"]
        today = timezone.localdate()
        if d < today or d > today + timedelta(days=30):
            raise forms.ValidationError("Choose a date within the next 30 days.")
        return d


class RequestForm(forms.ModelForm):
    class Meta:
        model = AccessRequest
        fields = ["role", "days", "reason"]
        widgets = {
            "role": forms.Select(attrs={"class": "form-select"}),
            "days": forms.NumberInput(attrs={"class": _c(), "min": 1, "max": 365, "placeholder": "No end date"}),
            "reason": forms.Textarea(attrs={"class": _c(), "rows": 2, "placeholder": "What you need it for"}),
        }
        labels = {"days": "For how many days"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = visible_roles(user).filter(requestable=True)
