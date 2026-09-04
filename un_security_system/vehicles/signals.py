"""Package/eSign lifecycle synchronization."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models_esign import Envelope

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Envelope)
def sync_package_esign(sender, instance, **kwargs):
    """Reflect eSign state back into a package document and Package Flow."""
    try:
        doc = instance.package_document_source
    except Exception:
        return

    try:
        if instance.status == Envelope.STATUS_SENT and doc.status != 'pending_signature':
            doc.status = 'pending_signature'
            doc.save(update_fields=['status'])
            return

        if (
            instance.status == Envelope.STATUS_COMPLETED
            and instance.completed_pdf
        ):
            from .package_esign import complete_package_step_from_envelope
            complete_package_step_from_envelope(instance)
    except Exception:
        logger.exception('Could not synchronize package eSign envelope %s', instance.pk)
