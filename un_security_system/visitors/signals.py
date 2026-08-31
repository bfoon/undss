"""
visitors/signals.py  —  REPLACEMENT FILE
════════════════════════════════════════

Same behaviour as before, with one addition: the sync now only runs for offices
that actually have the meeting-linked visitors module switched on.

Why this matters
────────────────
`connect_signals()` attaches to MeetingAttendee globally. Once a second country
office exists, an office that has room booking on but meeting-linked visitors
off would still have group members silently written into its visitor records
every time an attendee is accepted somewhere. The feature switch has to reach
into the signal, not just the URL.

The office is taken from the visitor's `registered_by` user, since `Visitor`
itself is not office-stamped yet. Once you add `OfficeOwnedModel` to `Visitor`,
change `_office_for` to read `visitor.country_office` directly.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
import logging

logger = logging.getLogger(__name__)

FEATURE = "visitor_meeting_link"


def _office_for(visitor):
    """
    The country office that owns this visitor record.

    Prefer an explicit field if you have added one; otherwise fall back to
    whoever registered the request.
    """
    office = getattr(visitor, "country_office", None)
    if office is not None:
        return office
    registered_by = getattr(visitor, "registered_by", None)
    return getattr(registered_by, "country_office", None)


def _sync_allowed(visitor) -> bool:
    """
    True when this visitor's office has meeting-linked visitors enabled.

    Fails open when tenancy is not installed, so this file stays safe to deploy
    before the tenancy app exists.
    """
    try:
        from tenancy.services import office_has_feature
    except ImportError:
        return True

    office = _office_for(visitor)
    if office is None:
        # No office resolvable — treat as a single-office deployment and allow.
        return True
    return office_has_feature(office, FEATURE)


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
        ).select_related('registered_by')

        for visitor in linked_visitors:
            if not _sync_allowed(visitor):
                logger.debug(
                    "Skipping sync for visitor #%s — %s is off for its office.",
                    visitor.pk, FEATURE,
                )
                continue

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