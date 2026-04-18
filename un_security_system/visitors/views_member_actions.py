"""
visitors/views_member_actions.py
─────────────────────────────────
All per-GroupMember gate actions:
  - member_checkin
  - member_checkout
  - member_flag_attention
  - member_clear_attention
  - member_update_field
  - member_upload_photo
  - booking_info_api

Import these into your main views.py:
    from .views_member_actions import (
        member_checkin, member_checkout,
        member_flag_attention, member_clear_attention,
        member_update_field, member_upload_photo,
        booking_info_api,
    )
"""
import logging
from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.http import JsonResponse
from django.utils import timezone
from django.db import transaction

from .models import Visitor, GroupMember, VisitorCard, VisitorLog

logger = logging.getLogger(__name__)


def _gate_role(user):
    return user.is_authenticated and (
        getattr(user, 'role', None) in ('data_entry', 'lsa', 'soc') or user.is_superuser
    )


# ─────────────────────────────────────────────────────────────────────────────
# Individual member check-in
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def member_checkin(request, visitor_id, member_id):
    """
    POST: check in a single group member at the gate.
    Captures: id_number, id_type, card_number, gate, optional photo.
    For meeting-linked visitors, syncs updates back to MeetingAttendee.

    When called via fetch (X-Fetch: 1 header), returns JSON so the
    detail page can swap the button instantly without a full reload.
    """
    is_fetch = request.headers.get('X-Fetch') == '1'

    def _json_error(msg, status=400):
        return JsonResponse({'ok': False, 'error': msg}, status=status)

    def _json_ok(payload):
        return JsonResponse({'ok': True, **payload})

    if not _gate_role(request.user):
        if is_fetch:
            return _json_error("Permission denied.", 403)
        messages.error(request, "You don't have permission to perform gate actions.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    visitor = get_object_or_404(Visitor, pk=visitor_id)
    member  = get_object_or_404(GroupMember, pk=member_id, visitor=visitor)

    if member.checked_in and not member.checked_out:
        if is_fetch:
            return _json_error(f"{member.full_name} is already checked in.")
        messages.warning(request, f"{member.full_name} is already checked in.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    id_number   = (request.POST.get('id_number') or '').strip()
    id_type     = (request.POST.get('id_type') or 'other').strip()
    card_number = (request.POST.get('card_number') or '').strip()
    gate        = (request.POST.get('gate') or 'front').strip()

    if not card_number:
        if is_fetch:
            return _json_error("Visitor card number is required.")
        messages.error(request, f"{member.full_name}: Visitor card number is required for check-in.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    if not id_number and not member.id_number:
        if is_fetch:
            return _json_error("ID number is required.")
        messages.error(request, f"{member.full_name}: ID number is required for check-in.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    try:
        with transaction.atomic():
            now = timezone.now()

            card = VisitorCard.objects.select_for_update().get(number__iexact=card_number)
            if not card.is_active:
                raise ValueError(f"Card {card.number} is inactive.")
            if card.in_use:
                raise ValueError(f"Card {card.number} is already in use.")

            card.in_use = True
            card.issued_to = visitor
            card.issued_at = now
            card.issued_by = request.user
            card.returned_at = None
            card.returned_by = None
            card.save()

            fields_updated = {}
            if id_number and id_number != member.id_number:
                member.id_number = id_number
                fields_updated['id_number'] = id_number
            if id_type and id_type != member.id_type:
                member.id_type = id_type
            member.assigned_card = card
            member.checked_in = True
            member.checked_out = False
            member.check_in_time = now
            member.check_out_time = None
            member.save()

            photo_file = request.FILES.get('gate_photo') or request.FILES.get('photo')
            photo_url = None
            if photo_file:
                if not member.id_photo:
                    member.id_photo = photo_file
                    member.save(update_fields=['id_photo'])
                photo_url = member.id_photo.url if member.id_photo else None

            if member.from_meeting and fields_updated:
                member.sync_to_meeting_attendee(fields_updated)

            VisitorLog.objects.create(
                visitor=visitor,
                action='member_check_in',
                performed_by=request.user,
                gate=gate,
                group_member=member,
                notes=f"{member.full_name} checked in · ID: {id_number or member.id_number} · Card: {card.number}",
            )

        msg = f"{member.full_name} checked in. Card {card.number} issued."

        if is_fetch:
            return _json_ok({
                'message': msg,
                'member_pk': member.pk,
                'card_number': card.number,
                'check_in_time': now.strftime('%H:%M'),
                'id_number': member.id_number,
                'photo_url': photo_url,
            })

        messages.success(request, msg)

    except VisitorCard.DoesNotExist:
        err = f"Card '{card_number}' not found in the system."
        if is_fetch:
            return _json_error(err)
        messages.error(request, err)
    except ValueError as e:
        if is_fetch:
            return _json_error(str(e))
        messages.error(request, str(e))
    except Exception as e:
        logger.exception("Error during member check-in: %s", e)
        if is_fetch:
            return _json_error(f"Unexpected error: {e}", 500)
        messages.error(request, f"Unexpected error: {e}")

    return redirect('visitors:visitor_detail', pk=visitor_id)


# ─────────────────────────────────────────────────────────────────────────────
# Individual member check-out
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def member_checkout(request, visitor_id, member_id):
    """
    POST: check out a single group member and return their card.
    Returns JSON when called with X-Fetch: 1 header so the detail page
    can flip the button instantly without a reload.
    """
    is_fetch = request.headers.get('X-Fetch') == '1'

    def _json_error(msg, status=400):
        return JsonResponse({'ok': False, 'error': msg}, status=status)

    if not _gate_role(request.user):
        if is_fetch:
            return _json_error("Permission denied.", 403)
        messages.error(request, "You don't have permission to perform gate actions.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    visitor = get_object_or_404(Visitor, pk=visitor_id)
    member  = get_object_or_404(GroupMember, pk=member_id, visitor=visitor)
    gate    = (request.POST.get('gate') or 'front').strip()

    if not member.checked_in or member.checked_out:
        if is_fetch:
            return _json_error(f"{member.full_name} is not currently checked in.")
        messages.warning(request, f"{member.full_name} is not currently checked in.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    with transaction.atomic():
        now = timezone.now()
        card_number_returned = None

        if member.assigned_card:
            card = VisitorCard.objects.select_for_update().get(pk=member.assigned_card.pk)
            card.in_use = False
            card.returned_at = now
            card.returned_by = request.user
            card.issued_to = None
            card.save()
            card_number_returned = card.number
            member.assigned_card = None

        member.checked_in = False
        member.checked_out = True
        member.check_out_time = now
        member.save()

        VisitorLog.objects.create(
            visitor=visitor,
            action='member_check_out',
            performed_by=request.user,
            gate=gate,
            group_member=member,
            notes=f"{member.full_name} checked out"
                  + (f" · Card {card_number_returned} returned" if card_number_returned else " (no card)"),
        )

    msg = (
        f"{member.full_name} checked out."
        + (f" Card {card_number_returned} returned." if card_number_returned else "")
    )

    if is_fetch:
        return JsonResponse({
            'ok': True,
            'message': msg,
            'member_pk': member.pk,
            'card_number_returned': card_number_returned,
            'check_out_time': now.strftime('%H:%M'),
        })

    messages.success(request, msg)
    return redirect('visitors:visitor_detail', pk=visitor_id)


# ─────────────────────────────────────────────────────────────────────────────
# Attention flag
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def member_flag_attention(request, visitor_id, member_id):
    """
    POST: flag a group member as needing host attention at the gate.

    Behaviour:
    - Saves gate_attention = 'needs_attention' + the officer's note on the GroupMember
    - If the visitor is meeting-linked: also sets gate_flag=True + gate_flag_note on
      the linked MeetingAttendee so the flag shows on the booking detail page
    - Sends an email to the host (meeting requester OR visitor registered_by)
    - Creates a VisitorLog entry
    - Supports X-Fetch: 1 for AJAX calls (returns JSON)
    """
    is_fetch = request.headers.get('X-Fetch') == '1'

    if not _gate_role(request.user):
        if is_fetch:
            return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
        messages.error(request, "You don't have permission to flag gate attention.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    visitor = get_object_or_404(Visitor, pk=visitor_id)
    member  = get_object_or_404(GroupMember, pk=member_id, visitor=visitor)
    note    = (request.POST.get('attention_note') or '').strip()

    if not note:
        if is_fetch:
            return JsonResponse({'ok': False, 'error': 'Please provide a reason.'}, status=400)
        messages.error(request, "Please provide a reason for flagging this person.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    now = timezone.now()
    member.gate_attention = 'needs_attention'
    member.gate_attention_note = note
    member.gate_attention_raised_at = now
    member.save(update_fields=['gate_attention', 'gate_attention_note', 'gate_attention_raised_at'])

    gate_user = request.user.get_full_name() or request.user.username

    VisitorLog.objects.create(
        visitor=visitor,
        action='gate_flag',
        performed_by=request.user,
        group_member=member,
        notes=f"Attention flagged for {member.full_name}: {note}",
    )

    # ── Propagate flag to MeetingAttendee (meeting-linked visitors) ──────────
    if member.from_meeting:
        try:
            from accounts.models import MeetingAttendee
            attendee = MeetingAttendee.objects.filter(pk=member.meeting_attendee_id).first()
            if attendee:
                attendee.gate_flag = True
                attendee.gate_flag_note = note
                attendee.gate_flag_raised_at = now
                attendee.gate_flag_by = gate_user
                update_fields = []
                for f in ('gate_flag', 'gate_flag_note', 'gate_flag_raised_at', 'gate_flag_by'):
                    if hasattr(attendee, f):
                        update_fields.append(f)
                if update_fields:
                    attendee.save(update_fields=update_fields)
        except Exception as e:
            logger.warning("Could not propagate gate flag to MeetingAttendee: %s", e)

    # ── Send email notification to host ──────────────────────────────────────
    host, detail_url = _resolve_host_and_url(visitor, request)
    _send_attention_email(visitor, member, note, gate_user, host, detail_url)

    msg = f"{member.full_name} flagged for attention. Host has been notified."

    if is_fetch:
        return JsonResponse({
            'ok': True,
            'message': msg,
            'member_pk': member.pk,
            'note': note,
            'raised_at': now.strftime('%H:%M'),
        })

    messages.warning(request, msg)
    return redirect('visitors:visitor_detail', pk=visitor_id)


def _resolve_host_and_url(visitor, request):
    """Return (host_user, absolute_detail_url) for the visitor's host."""
    from django.urls import reverse

    host = None
    detail_url = ''

    if visitor.linked_booking:
        host = getattr(visitor.linked_booking, 'requested_by', None)
        try:
            detail_url = request.build_absolute_uri(
                reverse('accounts:booking_detail', kwargs={'pk': visitor.linked_booking.pk})
            )
        except Exception:
            pass

    if not host:
        host = getattr(visitor, 'registered_by', None)

    if not detail_url:
        try:
            detail_url = request.build_absolute_uri(
                reverse('visitors:visitor_detail', kwargs={'pk': visitor.pk})
            )
        except Exception:
            pass

    return host, detail_url


def _send_attention_email(visitor, member, note, gate_user, host, detail_url):
    """
    Send an attention-alert email to the host in a background thread.
    Works for both meeting-linked and standalone visitor access requests.
    """
    if not host or not getattr(host, 'email', None):
        return

    try:
        from django.core.mail import send_mail
        from django.conf import settings

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        if not from_email:
            return

        is_meeting = bool(visitor.linked_booking)
        meeting_info = ''
        if is_meeting:
            b = visitor.linked_booking
            meeting_info = (
                f"  Meeting: {b.title}\n"
                f"  Date:    {b.date.strftime('%A, %d %B %Y')}\n"
                f"  Room:    {b.room.name}\n\n"
            )

        subject = f"[Security Alert] Gate attention needed — {member.full_name}"
        body = (
            f"Dear {host.get_full_name() or host.username},\n\n"
            f"Gate security has flagged one of your {'meeting attendees' if is_meeting else 'visitors'} "
            f"and requires your immediate assistance.\n\n"
            f"  Person:  {member.full_name}\n"
            f"  Concern: {note}\n\n"
            f"{meeting_info}"
            f"Please come to the gate or contact security to help verify and clear this person.\n\n"
            f"{'View meeting details' if is_meeting else 'View visitor record'}: {detail_url}\n\n"
            f"Flagged by: {gate_user}\n"
            f"Time: {timezone.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"Best regards,\nUN Security / Gate Management System"
        )

        import threading
        threading.Thread(
            target=lambda: send_mail(subject, body, from_email, [host.email], fail_silently=True),
            daemon=True,
        ).start()

    except Exception as e:
        logger.warning("Failed to send attention email: %s", e)


@login_required
@require_POST
def member_clear_attention(request, visitor_pk, member_pk):
    """POST: host or LSA clears the attention flag on a member."""
    visitor = get_object_or_404(Visitor, pk=visitor_pk)
    member  = get_object_or_404(GroupMember, pk=member_pk, visitor=visitor)

    can_clear = (
        request.user.is_superuser
        or getattr(request.user, 'role', None) in ('lsa', 'soc')
        or visitor.registered_by_id == request.user.pk
        or (visitor.linked_booking and
            getattr(visitor.linked_booking, 'requested_by_id', None) == request.user.pk)
    )
    if not can_clear:
        messages.error(request, "You don't have permission to clear this flag.")
        return redirect('visitors:visitor_detail', pk=visitor_pk)

    member.gate_attention = 'cleared'
    member.gate_attention_cleared_at = timezone.now()
    member.save(update_fields=['gate_attention', 'gate_attention_cleared_at'])

    VisitorLog.objects.create(
        visitor=visitor,
        action='gate_cleared',
        performed_by=request.user,
        group_member=member,
        notes=f"Attention cleared for {member.full_name} by {request.user.username}",
    )

    messages.success(request, f"Attention flag cleared for {member.full_name}.")
    return redirect('visitors:visitor_detail', pk=visitor_pk)


# ─────────────────────────────────────────────────────────────────────────────
# Inline field edit
# ─────────────────────────────────────────────────────────────────────────────

EDITABLE_FIELDS = {
    'id_number': ('id_number', str, 100),
    'contact_number': ('contact_number', str, 20),
    'nationality': ('nationality', str, 100),
    'id_type': ('id_type', str, 20),
}

@login_required
@require_POST
def member_update_field(request, visitor_id, member_id):
    """
    POST: update a single text field on a GroupMember.
    Allowed fields: id_number, contact_number, nationality, id_type.
    Gate changes sync back to MeetingAttendee.
    """
    if not _gate_role(request.user):
        messages.error(request, "You don't have permission to edit member details.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    visitor = get_object_or_404(Visitor, pk=visitor_id)
    member  = get_object_or_404(GroupMember, pk=member_id, visitor=visitor)

    field_name  = (request.POST.get('field_name') or '').strip()
    field_value = (request.POST.get('field_value') or '').strip()

    if field_name not in EDITABLE_FIELDS:
        messages.error(request, f"Field '{field_name}' cannot be edited here.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    model_field, cast, max_len = EDITABLE_FIELDS[field_name]
    value = str(field_value)[:max_len]

    setattr(member, model_field, value)
    member.save(update_fields=[model_field])

    # Sync back to meeting attendee
    if member.from_meeting:
        member.sync_to_meeting_attendee({field_name: value})

    messages.success(request, f"{field_name.replace('_', ' ').title()} updated for {member.full_name}.")
    return redirect('visitors:visitor_detail', pk=visitor_id)


# ─────────────────────────────────────────────────────────────────────────────
# Photo upload / capture
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def member_upload_photo(request, visitor_id, member_id):
    """POST: save a face photo or uploaded ID photo for a group member."""
    if not _gate_role(request.user):
        messages.error(request, "You don't have permission to update photos.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    visitor = get_object_or_404(Visitor, pk=visitor_id)
    member  = get_object_or_404(GroupMember, pk=member_id, visitor=visitor)

    photo = request.FILES.get('photo') or request.FILES.get('gate_photo')
    if not photo:
        messages.error(request, "No photo provided.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    if photo.size > 8 * 1024 * 1024:
        messages.error(request, "Photo must be under 8 MB.")
        return redirect('visitors:visitor_detail', pk=visitor_id)

    member.id_photo = photo
    member.save(update_fields=['id_photo'])

    messages.success(request, f"Photo saved for {member.full_name}.")
    return redirect('visitors:visitor_detail', pk=visitor_id)


# ─────────────────────────────────────────────────────────────────────────────
# Booking info API (used by visitor_form.html to auto-populate fields)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def booking_info_api(request, booking_id):
    """
    GET /visitors/api/booking-info/<booking_id>/
    Returns meeting details for auto-populating the visitor form when linking.
    """
    try:
        from accounts.models import RoomBooking, MeetingAttendee
        from django.utils import timezone as tz

        today = tz.now().date()
        booking = RoomBooking.objects.select_related(
            'requested_by', 'room', 'requested_by__agency'
        ).get(pk=booking_id, status='approved', date__gte=today)

        host = booking.requested_by
        host_name = host.get_full_name() or host.username
        host_email = host.email or ''
        host_phone = getattr(host, 'phone_number', '') or getattr(host, 'phone', '') or ''

        # Agency / organisation
        agency = ''
        if hasattr(host, 'agency') and host.agency:
            agency = getattr(host.agency, 'name', '') or ''
        if not agency and hasattr(host, 'agency_name'):
            agency = host.agency_name or ''

        # Duration in plain text
        from datetime import datetime, date as date_cls
        start_dt = datetime.combine(date_cls.today(), booking.start_time)
        end_dt   = datetime.combine(date_cls.today(), booking.end_time)
        diff     = end_dt - start_dt
        total_mins = int(diff.total_seconds() / 60)
        if total_mins >= 60:
            hours = total_mins // 60
            mins  = total_mins % 60
            duration_str = f"{hours}h" + (f" {mins}min" if mins else "")
        else:
            duration_str = f"{total_mins} minutes"

        # Accepted registrant count
        accepted_count = MeetingAttendee.objects.filter(
            booking=booking, is_accepted=True
        ).count()

        return JsonResponse({
            'ok': True,
            'id': booking.pk,
            'title': booking.title,
            'date': booking.date.strftime('%Y-%m-%d'),
            'start_time': booking.start_time.strftime('%H:%M'),
            'end_time': booking.end_time.strftime('%H:%M'),
            'duration': duration_str,
            'room': booking.room.name,
            'host_name': host_name,
            'host_email': host_email,
            'host_phone': host_phone,
            'agency': agency,
            'requested_by_org': agency,
            'description': booking.description or '',
            'accepted_count': accepted_count,
        })

    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=404)

# ─────────────────────────────────────────────────────────────────────────────
# Gate attention flags API — polled by booking_detail.html
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def visitor_gate_flags_api(request, visitor_id):
    """
    GET /visitors/<visitor_id>/gate-flags/
    Returns all active attention flags on group members for this visitor.
    Used by visitor_detail.html to poll for new flags without a page reload.
    """
    visitor = get_object_or_404(Visitor, pk=visitor_id)
    flagged = visitor.group_members.filter(gate_attention='needs_attention').select_related('assigned_card')
    flags = []
    for m in flagged:
        flags.append({
            'member_pk': m.pk,
            'name': m.full_name,
            'note': m.gate_attention_note,
            'raised_at': m.gate_attention_raised_at.strftime('%H:%M') if m.gate_attention_raised_at else '',
        })
    return JsonResponse({'ok': True, 'flags': flags, 'count': len(flags)})


@login_required
def booking_gate_flags_api(request, booking_id):
    """
    GET /visitors/api/booking-gate-flags/<booking_id>/
    Returns all active gate attention flags for members linked to a booking.
    Polled by booking_detail.html every 30 seconds.
    """
    try:
        from accounts.models import RoomBooking
        booking = get_object_or_404(RoomBooking, pk=booking_id)

        # Find all visitor access requests linked to this booking
        visitors = Visitor.objects.filter(linked_booking=booking)
        flags = []
        for v in visitors:
            for m in v.group_members.filter(gate_attention='needs_attention'):
                flags.append({
                    'member_pk': m.pk,
                    'name': m.full_name,
                    'email': m.email,
                    'note': m.gate_attention_note,
                    'raised_at': m.gate_attention_raised_at.strftime('%H:%M') if m.gate_attention_raised_at else '',
                    'visitor_pk': v.pk,
                })
        return JsonResponse({'ok': True, 'flags': flags, 'count': len(flags)})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)