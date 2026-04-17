"""
visitors/signals.py
───────────────────
Automatically re-syncs group members on a linked visitor access request
whenever a MeetingAttendee record is accepted (is_accepted flips to True).

This makes the sync fully automatic — no manual "Sync Now" click needed
unless you want to force a refresh outside of the normal acceptance flow.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
import logging

logger = logging.getLogger(__name__)


def _on_attendee_accepted(sender, instance, created, **kwargs):
    """
    Called after any MeetingAttendee save.
    If the attendee is now accepted, find all Visitor access requests
    linked to the same booking and sync their group members.
    """
    if not getattr(instance, 'is_accepted', False):
        return  # not accepted — nothing to do

    try:
        from .models import Visitor
        linked_visitors = Visitor.objects.filter(
            linked_booking=instance.booking,
            visitor_type='group',
        )
        for visitor in linked_visitors:
            created_count, updated_count = visitor.sync_members_from_booking()
            if created_count or updated_count:
                logger.info(
                    "Auto-synced visitor #%s from meeting #%s: %d new, %d updated",
                    visitor.pk, instance.booking_id, created_count, updated_count,
                )
    except Exception as exc:
        # Never let a signal error break the booking workflow
        logger.exception("Error during auto-sync of meeting members: %s", exc)


def connect_signals():
    """
    Called from VisitorsConfig.ready().
    Deferred import so this file is safe to import before migrations run.
    """
    try:
        from accounts.models import MeetingAttendee
        post_save.connect(
            _on_attendee_accepted,
            sender=MeetingAttendee,
            dispatch_uid='visitors_sync_on_attendee_accepted',
        )
        logger.debug("visitors: MeetingAttendee post_save signal connected.")
    except ImportError:
        logger.debug("visitors: accounts.MeetingAttendee not available — signal skipped.")
