# accounts/expiry_notify.py
"""
UN PASS — advance warning before things expire.

Three kinds of record in UN PASS carry an expiry date and, until now, nothing
ever told anyone it was coming:

    Parking cards       ParkingCard.expiry_date      checked only when scanned
    Staff ID cards      User.employee_id_expiry      a list HR has to remember
                                                     to open
    Module licences     FeatureGrant.valid_until     the module silently
                                                     switches off for the office

Each was discovered the day it failed: at the gate, at the door, or when a
whole office lost a module. This sends warnings ahead of time instead.

How it decides what to send
---------------------------
Warnings go out at fixed distances from the expiry date — 30, 14, 7 and 1 days
before, on the day, and once after — and each is sent ONCE per record per
distance. "Once" is remembered in Redis, keyed by the record, its expiry date
and the distance, so:

* a missed day is caught up the next day rather than skipped: if the worker
  was down on the 7-day mark, the 7-day warning goes out on day 6;
* renewing a card changes its expiry date, which changes the key, so the new
  date gets its own fresh set of warnings;
* nothing is ever sent twice, however often the job runs.

Without Redis it falls back to sending only on the exact day of each
distance, which is correct when the job runs daily and merely less forgiving
when it does not.

People get one email per run, not one per record: security gets a digest of
parking cards, each agency's HR gets a digest of its own staff, and each
member of staff gets their own reminder about their own ID card.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .asset_email import deliver_synchronously, send_email_async

logger = logging.getLogger(__name__)

#: Days before expiry at which a warning is due. 0 is the day itself; the
#: final bucket ("expired") catches anything already past its date.
DEFAULT_THRESHOLDS = (30, 14, 7, 1, 0)

TEMPLATE = "emails/expiry/digest.html"


def thresholds() -> tuple[int, ...]:
    raw = getattr(settings, "UNPASS_EXPIRY_THRESHOLDS", DEFAULT_THRESHOLDS)
    return tuple(sorted({int(x) for x in raw}, reverse=True))


def _bucket(days_left: int) -> str | None:
    """
    The warning a record is due, or None when it is too far off.

    The tightest threshold the record has reached: 9 days out is the 14-day
    warning, 3 days out the 7-day one. Past expiry, one "expired" notice.
    """
    if days_left < 0:
        return "expired"
    due = [t for t in thresholds() if days_left <= t]
    return str(min(due)) if due else None


# ─────────────────────────────────────────────────────────────────────────────
# Remembering what was sent
# ─────────────────────────────────────────────────────────────────────────────

_REDIS = None


def _redis():
    """A Redis client, or None. Cached; never raises."""
    global _REDIS
    if _REDIS is not None:
        return _REDIS or None
    try:
        import redis

        url = getattr(settings, "UNPASS_STATE_REDIS_URL", "") or getattr(
            settings, "CELERY_BROKER_URL", "")
        client = redis.Redis.from_url(url, socket_timeout=3) if url else None
        if client is not None:
            client.ping()
        _REDIS = client or False
    except Exception:
        logger.warning("Expiry warnings: Redis unavailable — sending on exact "
                       "days only", exc_info=True)
        _REDIS = False
    return _REDIS or None


def _claim(kind: str, pk, expiry: date, bucket: str) -> bool:
    """
    True the first time this (record, expiry date, warning) is seen.

    With Redis: SET NX, kept for 400 days. Without: only on the exact day of
    the threshold, so a daily run still sends each warning once.
    """
    client = _redis()
    if client is not None:
        key = f"unpass:expiry:{kind}:{pk}:{expiry.isoformat()}:{bucket}"
        try:
            return bool(client.set(key, "1", nx=True, ex=400 * 86400))
        except Exception:
            logger.warning("Expiry warnings: Redis write failed", exc_info=True)
    days_left = (expiry - timezone.localdate()).days
    if bucket == "expired":
        return days_left == -1
    return str(days_left) == bucket


# ─────────────────────────────────────────────────────────────────────────────
# Sending
# ─────────────────────────────────────────────────────────────────────────────

def _site() -> str:
    return (getattr(settings, "SITE_URL", "") or "").rstrip("/")


def _url(name: str) -> str:
    """Absolute link to a named URL, or "" if it does not resolve here."""
    try:
        from django.urls import reverse

        return f"{_site()}{reverse(name)}"
    except Exception:
        return ""


def _send(subject, to, heading, intro, rows, columns, action_url=""):
    to = sorted({e for e in to if e})
    if not to or not rows:
        return 0
    send_email_async(
        subject=subject,
        to_emails=to,
        html_template=TEMPLATE,
        context={
            "subject": subject,
            "heading": heading,
            "intro": intro,
            "rows": rows,
            "columns": columns,
            "action_url": action_url,
            "brand": "UN PASS",
        },
    )
    return len(to)


def _label(days_left: int) -> str:
    if days_left < 0:
        return f"expired {-days_left} day{'s' if days_left != -1 else ''} ago"
    if days_left == 0:
        return "expires today"
    return f"in {days_left} day{'s' if days_left != 1 else ''}"


def _emails(qs) -> list[str]:
    return [e for e in qs.filter(is_active=True).values_list("email", flat=True) if e]


# ─────────────────────────────────────────────────────────────────────────────
# The three kinds
# ─────────────────────────────────────────────────────────────────────────────

def parking_cards(today: date, horizon: int) -> dict:
    """One digest to the security team (LSA and SOC roles)."""
    from vehicles.models import ParkingCard

    User = get_user_model()
    rows = []
    qs = ParkingCard.objects.filter(
        is_active=True,
        expiry_date__lte=today + timezone.timedelta(days=horizon),
    ).order_by("expiry_date")
    for card in qs:
        left = (card.expiry_date - today).days
        bucket = _bucket(left)
        if bucket and _claim("parking", card.pk, card.expiry_date, bucket):
            rows.append([card.card_number, card.owner_name, card.vehicle_plate,
                         card.department, card.expiry_date.isoformat(),
                         _label(left)])
    roles = getattr(settings, "UNPASS_EXPIRY_PARKING_ROLES", ["lsa", "soc"])
    sent = _send(
        f"Parking cards expiring — {len(rows)} to renew",
        _emails(User.objects.filter(role__in=roles)),
        "Parking cards expiring",
        "These parking cards are reaching their expiry date. An expired card "
        "is refused at the gate.",
        rows,
        ["Card", "Holder", "Plate", "Department", "Expires", ""],
        _url("vehicles:parking_card_list"),
    )
    return {"due": len(rows), "emailed": sent}


def staff_ids(today: date, horizon: int) -> dict:
    """Each person about their own card; each agency's HR about its staff."""
    User = get_user_model()
    people = (
        User.objects.filter(is_active=True)
        .exclude(employee_id__isnull=True).exclude(employee_id="")
        .exclude(employee_id_expiry__isnull=True)
        .filter(employee_id_expiry__lte=today + timezone.timedelta(days=horizon))
        .select_related("agency").order_by("employee_id_expiry")
    )
    per_agency = defaultdict(list)
    personal = 0
    for person in people:
        left = (person.employee_id_expiry - today).days
        bucket = _bucket(left)
        if not bucket or not _claim("staffid", person.pk,
                                    person.employee_id_expiry, bucket):
            continue
        name = person.get_full_name() or person.username
        row = [name, person.employee_id, person.employee_id_expiry.isoformat(),
               _label(left)]
        per_agency[person.agency_id].append(row)
        personal += _send(
            "Your UN ID card " + ("has expired" if left < 0 else
                                  "expires today" if left == 0 else
                                  f"expires in {left} day{'s' if left != 1 else ''}"),
            [person.email],
            "Your ID card",
            "Your staff ID card is reaching its expiry date. Request a renewal "
            "from UN PASS so the new card is ready in time.",
            [row],
            ["Name", "ID number", "Expires", ""],
            _url("accounts:my_idcard_requests"),
        )

    hr = 0
    for agency_id, rows in per_agency.items():
        hr += _send(
            f"Staff ID cards expiring — {len(rows)} in your agency",
            _emails(User.objects.filter(role__in=["agency_hr"],
                                        agency_id=agency_id)),
            "Staff ID cards expiring",
            "These staff members' ID cards are reaching their expiry date.",
            rows,
            ["Name", "ID number", "Expires", ""],
            _url("accounts:expiring_ids"),
        )
    return {"due": sum(len(r) for r in per_agency.values()),
            "emailed_staff": personal, "emailed_hr": hr}


def module_licences(today: date, horizon: int) -> dict:
    """
    One digest to platform administrators.

    A lapsed grant switches the module OFF for that office with no warning to
    its users — the one expiry here that takes a whole service down at once.
    """
    try:
        from tenancy.models import FeatureGrant
    except Exception:
        return {"due": 0, "emailed": 0, "skipped": "tenancy not installed"}

    User = get_user_model()
    rows = []
    qs = (FeatureGrant.objects.filter(enabled=True, valid_until__isnull=False)
          .filter(valid_until__lte=today + timezone.timedelta(days=horizon))
          .order_by("valid_until"))
    for grant in qs:
        left = (grant.valid_until - today).days
        bucket = _bucket(left)
        if bucket and _claim("licence", grant.pk, grant.valid_until, bucket):
            scope = (getattr(grant, "country_office", None)
                     or getattr(grant, "agency", None) or "—")
            rows.append([grant.get_feature_code_display(), str(scope),
                         grant.valid_until.isoformat(), _label(left)])
    sent = _send(
        f"Module licences expiring — {len(rows)}",
        _emails(User.objects.filter(is_superuser=True)),
        "Module licences expiring",
        "When a licence lapses the module switches off for that office, for "
        "every user at once. Renew or extend these before their date.",
        rows,
        ["Module", "Office / agency", "Valid until", ""],
        _url("admin:tenancy_featuregrant_changelist"),
    )
    return {"due": len(rows), "emailed": sent}


def run(today: date | None = None) -> dict:
    """Send every warning that is due. Never raises for one kind's failure."""
    today = today or timezone.localdate()
    horizon = max(thresholds()) if thresholds() else 30
    out = {}
    with deliver_synchronously():
        for name, fn in (("parking_cards", parking_cards),
                         ("staff_ids", staff_ids),
                         ("module_licences", module_licences)):
            try:
                out[name] = fn(today, horizon)
            except Exception as exc:
                logger.exception("Expiry warnings: %s failed", name)
                out[name] = {"error": str(exc)[:200]}
    return out
