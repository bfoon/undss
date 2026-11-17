from django.conf import settings
from django.db.models import Q

from .models import AgencyServiceConfig


def agency_service_flags(request):
    """
    Exposes:
        asset_mgmt_enabled  – existing Asset Management flag
        esign_enabled       – NES availability (falls back to the asset flag)
        esign_pending_count – envelopes currently waiting on this user's signature
    """
    user = getattr(request, "user", None)
    enabled = False
    esign_enabled = False
    pending = 0

    if user and user.is_authenticated:
        agency = getattr(user, "agency", None)
        if agency:
            svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
            enabled = bool(svc.asset_mgmt_enabled)
            # Add an `esign_enabled` BooleanField to AgencyServiceConfig to control
            # NES separately; until then it rides on the Asset Management flag.
            esign_enabled = bool(getattr(svc, "esign_enabled", enabled))

            if esign_enabled:
                try:
                    from .models_esign import Envelope, EnvelopeRecipient

                    rows = EnvelopeRecipient.objects.filter(
                        Q(user=user) | Q(email__iexact=user.email),
                        envelope__agency=agency,
                        envelope__status=Envelope.STATUS_SENT,
                        role__in=[
                            EnvelopeRecipient.ROLE_SIGNER,
                            EnvelopeRecipient.ROLE_APPROVER,
                        ],
                    ).exclude(
                        status__in=[
                            EnvelopeRecipient.STATUS_SIGNED,
                            EnvelopeRecipient.STATUS_DECLINED,
                        ]
                    ).select_related("envelope")

                    pending = sum(1 for r in rows if r.can_sign_now())
                except Exception:
                    pending = 0

    return {
        "asset_mgmt_enabled": enabled,
        "esign_enabled": esign_enabled,
        "esign_pending_count": pending,
        # Where the NES viewers load PDF.js from. Point this at a static
        # folder (e.g. "/static/vendor/pdfjs") on a server without internet
        # access or behind a strict Content-Security-Policy.
        # Wordmark shown in the UI and printed down the border of every
        # stamped page. Set ESIGN_BRAND = "UNDP SoftSign" to switch.
        "esign_brand": getattr(settings, "ESIGN_BRAND", "NES"),
        # Same wordmark, expanded. Used where there is room to explain it.
        "esign_brand_full": getattr(
            settings, "ESIGN_BRAND_FULL", "NES — Not eSign"
        ),
        # Badge on the witnessing nav entry. One indexed count, only for a
        # signed-in user and only when witnessing is on, so it costs nothing
        # on the pages that do not care.
        "nes_witness_alerts": _witness_alert_count(request),
        "esign_pdfjs_base": getattr(
            settings,
            "ESIGN_PDFJS_BASE",
            "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build",
        ),
    }


def _witness_alert_count(request) -> int:
    """
    Unresolved cross-office integrity alerts, for the nav badge.

    Deliberately defensive: this runs on every rendered page, so it returns 0
    rather than raising if witnessing is off, the table is not migrated yet,
    or nobody is signed in.
    """
    try:
        user = getattr(request, "user", None)
        if not (user and getattr(user, "is_authenticated", False)):
            return 0
        if not getattr(settings, "ESIGN_WITNESS_ENABLED", False):
            return 0

        from .models_esign_witness import WitnessAlert

        return WitnessAlert.objects.filter(resolved=False).count()
    except Exception:
        return 0
