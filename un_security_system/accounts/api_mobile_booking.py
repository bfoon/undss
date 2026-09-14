"""
Phone API for room booking.

This file is intentionally separate from accounts/api_mobile.py so it is easy
to add/remove without disturbing the existing eSign/mobile API.

Add the URL entries from patches/backend_routes_and_modules.diff.
"""

from datetime import date as date_cls, time as time_cls
import uuid

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .api_mobile import api, signed_in, _body, _fail
from .models import Room, RoomApprover, RoomBooking
from .room_access import visible_rooms, can_view_room


def _room_json(room):
    amenities = [
        {
            "id": amenity.pk,
            "code": amenity.code,
            "name": amenity.name,
        }
        for amenity in room.amenities.all()
        if amenity.is_active
    ]

    return {
        "id": room.pk,
        "name": room.name,
        "code": room.code,
        "type": room.room_type,
        "type_label": room.get_room_type_display(),
        "location": room.location or "",
        "capacity": room.capacity,
        "description": room.description or "",
        "available_now": bool(room.is_available_now),
        "shared_note": getattr(room, "shared_note", "") or "",
        "approval_mode": getattr(room, "approval_mode", "manual") or "manual",
        "amenities": amenities,
    }


def _booking_json(booking):
    return {
        "id": booking.pk,
        "room_id": booking.room_id,
        "room_name": booking.room.name,
        "room_code": booking.room.code,
        "title": booking.title,
        "description": booking.description or "",
        "date": booking.date.isoformat(),
        "start_time": booking.start_time.strftime("%H:%M"),
        "end_time": booking.end_time.strftime("%H:%M"),
        "status": booking.status,
        "status_label": booking.get_status_display(),
        "ict_support": booking.ict_support,
        "attendee_emails": booking.attendee_emails or "",
        "virtual_meeting_link": booking.virtual_meeting_link or "",
        "enable_attendance": bool(booking.enable_attendance),
        "enable_invite_link": bool(booking.enable_invite_link),
        "registration_code": (
            str(booking.registration_code)
            if booking.registration_code
            else None
        ),
        "rejection_reason": booking.rejection_reason or "",
        "created": (
            booking.created_at.isoformat()
            if booking.created_at
            else None
        ),
    }


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@api
@require_GET
@signed_in
def room_list(request):
    rooms = (
        visible_rooms(
            request.user,
            Room.objects.filter(is_active=True),
        )
        .select_related("owner_office")
        .prefetch_related("amenities")
        .order_by("name")
    )

    q = (request.GET.get("q") or "").strip().lower()
    if q:
        rooms = [
            room
            for room in rooms
            if q in room.name.lower()
            or q in (room.location or "").lower()
            or q in (room.code or "").lower()
        ]

    return JsonResponse(
        {
            "ok": True,
            "count": len(rooms),
            "rooms": [_room_json(room) for room in rooms],
        }
    )


@api
@require_GET
@signed_in
def my_bookings(request):
    rows = (
        RoomBooking.objects.filter(requested_by=request.user)
        .select_related("room")
        .order_by("-date", "-start_time")[:200]
    )

    return JsonResponse(
        {
            "ok": True,
            "count": len(rows),
            "bookings": [_booking_json(row) for row in rows],
        }
    )


@api
@require_POST
@signed_in
def create_booking(request, room_id):
    room = Room.objects.filter(pk=room_id, is_active=True).first()
    if room is None or not can_view_room(request.user, room):
        return _fail(
            "That room does not exist or is not available to your office.",
            404,
        )

    data = _body(request) or {}

    title = str(data.get("title") or "").strip()
    description = str(data.get("description") or "").strip()
    if not title:
        return _fail("Enter a meeting title.")

    try:
        booking_date = date_cls.fromisoformat(
            str(data.get("date") or "")
        )
        start_time = time_cls.fromisoformat(
            str(data.get("start_time") or "")
        )
        end_time = time_cls.fromisoformat(
            str(data.get("end_time") or "")
        )
    except (TypeError, ValueError):
        return _fail("Choose a valid date, start time and end time.")

    if booking_date < timezone.localdate():
        return _fail("The booking date cannot be in the past.")

    if end_time <= start_time:
        return _fail("End time must be after start time.")

    ict_support = str(data.get("ict_support") or "none")
    if ict_support not in {"none", "setup", "during"}:
        return _fail("Choose a valid ICT support option.")

    attendee_emails = str(data.get("attendee_emails") or "").strip()
    virtual_meeting_link = str(
        data.get("virtual_meeting_link") or ""
    ).strip()

    booking = RoomBooking(
        room=room,
        requested_by=request.user,
        title=title,
        description=description,
        date=booking_date,
        start_time=start_time,
        end_time=end_time,
        ict_support=ict_support,
        attendee_emails=attendee_emails,
        virtual_meeting_link=virtual_meeting_link,
        enable_attendance=_parse_bool(data.get("enable_attendance")),
        enable_invite_link=_parse_bool(data.get("enable_invite_link")),
        auto_accept_registration=_parse_bool(
            data.get("auto_accept_registration")
        ),
    )

    # Keep the same overlap validation already defined on RoomBooking.clean().
    try:
        booking.full_clean(exclude=["registration_code"])
    except ValidationError as exc:
        message = "; ".join(
            item
            for values in exc.message_dict.values()
            for item in values
        ) if hasattr(exc, "message_dict") else "; ".join(exc.messages)
        return _fail(message or "That booking is not valid.")

    # Honour the room's approval mode.
    mode = getattr(room, "approval_mode", "manual") or "manual"
    auto_approve = mode == "auto"
    if mode == "mixed":
        # Use the same active approver records as the web booking module.
        auto_approve = not RoomApprover.objects.filter(
            room=room,
            is_active=True,
        ).exists()

    if auto_approve:
        booking.status = "approved"
        booking.approved_at = timezone.now()

    if booking.enable_invite_link:
        booking.registration_code = uuid.uuid4()

    booking.save()

    requested_ids = data.get("amenity_ids")
    if isinstance(requested_ids, list):
        allowed = list(
            room.amenities.filter(
                is_active=True,
                pk__in=requested_ids,
            )
        )
        booking.selected_amenities.set(allowed)
        # Keep both fields in sync with the older/newer booking UI.
        booking.requested_amenities.set(allowed)

    # Reuse the website's existing notification helpers where available.
    try:
        from .views_room_booking import (
            notify_approvers_new_booking,
            notify_requester_booking_submitted,
        )

        if booking.status == "pending":
            notify_approvers_new_booking(booking)
            notify_requester_booking_submitted(booking)
    except Exception:
        # A mail problem should not turn a successfully-created booking into
        # a failed API request.
        pass

    return JsonResponse(
        {
            "ok": True,
            "booking": _booking_json(booking),
            "message": (
                "Room booked and approved."
                if booking.status == "approved"
                else "Booking submitted for approval."
            ),
        },
        status=201,
    )


@api
@require_POST
@signed_in
def cancel_booking(request, pk):
    booking = (
        RoomBooking.objects.filter(
            pk=pk,
            requested_by=request.user,
        )
        .select_related("room")
        .first()
    )

    if booking is None:
        return _fail("That booking no longer exists.", 404)

    if booking.status in {"cancelled", "rejected"}:
        return _fail(
            "That booking can no longer be cancelled.",
            409,
            stale=True,
        )

    booking.status = "cancelled"
    booking.save(update_fields=["status", "updated_at"])

    return JsonResponse(
        {
            "ok": True,
            "booking": _booking_json(booking),
            "message": "Booking cancelled.",
        }
    )
