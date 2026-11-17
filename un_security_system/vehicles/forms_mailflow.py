# vehicles/forms_mailflow.py
from django import forms
from django.db.models import Q

from .models_mailflow import MailRoute


def _c(extra=""):
    return f"form-control {extra}".strip()


class MailRouteForm(forms.ModelForm):
    class Meta:
        model = MailRoute
        fields = [
            "name", "direction", "workflow", "priority",
            "match_item_types", "match_sender_types", "match_destinations", "match_keywords",
            "is_fallback", "is_active",
        ]
        labels = {
            "workflow": "Flow Studio flow",
            "match_item_types": "Item type contains",
            "match_sender_types": "Sender type is",
            "match_destinations": "For organisation",
            "match_keywords": "Mentions",
            "is_fallback": "Fallback — use when no other rule matches",
            "is_active": "Active",
        }
        widgets = {
            "name": forms.TextInput(attrs={"class": _c(), "placeholder": "e.g. Government letters"}),
            "direction": forms.Select(attrs={"class": "form-select"}),
            "workflow": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.NumberInput(attrs={"class": _c(), "min": 1, "max": 9999}),
            "match_item_types": forms.TextInput(attrs={"class": _c(), "placeholder": "parcel, box"}),
            "match_sender_types": forms.TextInput(attrs={"class": _c(), "placeholder": "gov, ngo"}),
            "match_destinations": forms.TextInput(attrs={"class": _c(), "placeholder": "UNICEF, WFP"}),
            "match_keywords": forms.TextInput(attrs={"class": _c(), "placeholder": "invoice, contract, confidential"}),
            "is_fallback": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    SENDER_CODES = ("gov", "private", "ngo", "nonprofit", "individual", "other")

    def __init__(self, *args, agency=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models_esign_studio import DocumentWorkflow

        self.agency, self.user = agency, user
        office_id = getattr(user, "country_office_id", None)
        shared = Q(share_scope="agency") | Q(created_by=user)
        if office_id:
            shared |= Q(share_scope="office", office_id=office_id)
        qs = DocumentWorkflow.objects.filter(agency=agency, is_active=True).filter(shared)
        self.fields["workflow"].queryset = qs.order_by("name")
        self.fields["workflow"].label_from_instance = (
            lambda wf: f"{wf.name} — {wf.step_count} step{'s' if wf.step_count != 1 else ''}"
                       f"{' · private' if wf.share_scope == 'private' else ''}"
        )

    def clean_match_sender_types(self):
        raw = self.cleaned_data.get("match_sender_types") or ""
        codes = [p.strip().lower() for p in raw.split(",") if p.strip()]
        bad = [c for c in codes if c not in self.SENDER_CODES]
        if bad:
            raise forms.ValidationError(
                f"Unknown sender type: {', '.join(bad)}. Use: {', '.join(self.SENDER_CODES)}."
            )
        return ", ".join(codes)

    def clean(self):
        cd = super().clean()
        wf = cd.get("workflow")
        direction = cd.get("direction") or "incoming"
        if not wf:
            return cd

        # Reception runs this flow, so it can't be private to its designer.
        if wf.share_scope == "private":
            self.add_error(
                "workflow",
                "This flow is private. In Flow Studio, share it with your country office or "
                "agency so the mailroom can run it.",
            )

        # Docket check: would this flow start on a package?
        from accounts.workflow_engine_esign import clean_graph, validate_graph

        from .mail_docket import docket_for, schema_for
        from .models_mailflow import MailDocket

        if wf.form_id and MailDocket.objects.filter(form_id=wf.form_id).exists():
            schema = wf.form.schema
        else:
            docket = docket_for(self.agency, direction)
            schema = docket.form.schema if docket else schema_for(direction)
        report = validate_graph(clean_graph(wf.graph), schema)
        if report["errors"]:
            self.add_error(
                "workflow",
                "This flow can't run on the mail docket yet: " + report["errors"][0]["text"]
                + " Fix it in Flow Studio, then save the rule again.",
            )
        self.docket_warnings = [w["text"] for w in report["warnings"]][:5]

        has_criteria = any(cd.get(k) for k in ("match_item_types", "match_sender_types",
                                                  "match_destinations", "match_keywords"))
        if cd.get("is_fallback") and has_criteria:
            self.add_error("is_fallback", "A fallback catches everything else — clear the match fields, "
                                          "or untick Fallback.")
        return cd
