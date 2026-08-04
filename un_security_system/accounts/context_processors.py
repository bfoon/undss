from django.db.models import Q

from .models import AgencyServiceConfig


def agency_service_flags(request):
    """
    Exposes:
        asset_mgmt_enabled  – existing Asset Management flag
        esign_enabled       – eSign availability (falls back to the asset flag)
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
            # eSign separately; until then it rides on the Asset Management flag.
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
    }
