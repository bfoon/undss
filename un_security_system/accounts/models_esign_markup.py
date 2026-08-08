# accounts/models_esign_markup.py
"""
UN PASS — eSign markup layer (Word-style annotations + comment threads).

Sits beside models_esign.py and deliberately does NOT import from it — every
relation is a lazy string reference, so this module can be imported from
accounts/models.py in either order without a circular import.

Add ONE line at the bottom of accounts/models.py, after the existing
`from .models_esign import *`:

    from .models_esign_markup import *  # noqa: F401,F403

Then:  python manage.py makemigrations accounts && python manage.py migrate

Geometry contract
-----------------
Every coordinate is a fraction of the page (0..1) with the origin at the
TOP-LEFT — exactly the convention SignatureField already uses, so the same
numbers work in the browser overlay and, later, in a ReportLab stamp.

    highlight / area / strike : {"x":.., "y":.., "w":.., "h":..}
    note                      : {"x":.., "y":..}
    freehand                  : {"strokes":[[{"x":..,"y":..}, ...], ...],
                                 "bbox":{"x":..,"y":..,"w":..,"h":..}}
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

__all__ = [
    "MARKUP_COLORS",
    "MARKUP_COLOR_MAP",
    "markup_colors_json",
    "clean_geometry",
    "EnvelopeAnnotation",
    "EnvelopeAnnotationReply",
]


# ─────────────────────────────────────────────────────────────────────────────
# Colour = intent. The palette drives the comment box that opens, the badge on
# the thread card and the tint of the mark itself — one table, three surfaces.
# Keep in sync with the swatches in accounts/esign/_markup.html.
# ─────────────────────────────────────────────────────────────────────────────

MARKUP_COLORS = [
    {
        "key": "yellow",
        "label": "Comment",
        "hex": "#F4C000",
        "prompt": "What would you like to say about this?",
    },
    {
        "key": "green",
        "label": "Looks good",
        "hex": "#16A34A",
        "prompt": "Confirm what you are happy with here.",
    },
    {
        "key": "red",
        "label": "Must change",
        "hex": "#DC2626",
        "prompt": "What has to change before you can sign?",
    },
    {
        "key": "blue",
        "label": "Question",
        "hex": "#009EDB",
        "prompt": "What would you like clarified?",
    },
    {
        "key": "purple",
        "label": "Suggestion",
        "hex": "#7C3AED",
        "prompt": "What would you suggest instead?",
    },
]

MARKUP_COLOR_MAP = {c["key"]: c for c in MARKUP_COLORS}
DEFAULT_COLOR = "yellow"


def markup_colors_json():
    """Palette for the template — a plain list, JSON-serialisable as-is."""
    return list(MARKUP_COLORS)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry sanitising. Never trust a browser payload: clamp every coordinate
# and cap the point budget so one bad request cannot store a megabyte of
# scribble or push a mark off the page.
# ─────────────────────────────────────────────────────────────────────────────

MAX_STROKES = 80
MAX_POINTS_PER_STROKE = 600


def _f(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return out


def _clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, _f(value)))


def _rect(raw):
    x = _clamp((raw or {}).get("x"))
    y = _clamp((raw or {}).get("y"))
    w = _clamp((raw or {}).get("w", 0.0))
    h = _clamp((raw or {}).get("h", 0.0))
    # A zero-size drag is a mis-click, not a mark — give it a usable minimum.
    w = max(w, 0.01)
    h = max(h, 0.008)
    return {
        "x": round(x, 5),
        "y": round(y, 5),
        "w": round(min(w, 1.0 - x), 5),
        "h": round(min(h, 1.0 - y), 5),
    }


def clean_geometry(kind, raw):
    """Return a safe, minimal geometry dict for `kind`, or None if unusable."""
    raw = raw if isinstance(raw, dict) else {}

    if kind == EnvelopeAnnotation.KIND_NOTE:
        return {"x": round(_clamp(raw.get("x")), 5), "y": round(_clamp(raw.get("y")), 5)}

    if kind == EnvelopeAnnotation.KIND_FREEHAND:
        strokes = []
        for stroke in (raw.get("strokes") or [])[:MAX_STROKES]:
            if not isinstance(stroke, (list, tuple)):
                continue
            points = [
                {"x": round(_clamp(p.get("x")), 4), "y": round(_clamp(p.get("y")), 4)}
                for p in stroke[:MAX_POINTS_PER_STROKE]
                if isinstance(p, dict)
            ]
            if len(points) >= 2:
                strokes.append(points)
        if not strokes:
            return None

        xs = [p["x"] for s in strokes for p in s]
        ys = [p["y"] for s in strokes for p in s]
        bbox = {
            "x": round(min(xs), 5),
            "y": round(min(ys), 5),
            "w": round(max(max(xs) - min(xs), 0.01), 5),
            "h": round(max(max(ys) - min(ys), 0.01), 5),
        }
        return {"strokes": strokes, "bbox": bbox}

    return _rect(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Annotation
# ─────────────────────────────────────────────────────────────────────────────

class EnvelopeAnnotation(models.Model):
    """
    A mark on a page plus the comment thread hanging off it.

    Distinct from EnvelopeComment: that is an envelope-level note ("I have a
    question about this document"). This is anchored to pixels on a page
    ("this clause, here, must change") and carries replies.
    """

    KIND_HIGHLIGHT = "highlight"
    KIND_FREEHAND = "freehand"
    KIND_AREA = "area"
    KIND_STRIKE = "strike"
    KIND_NOTE = "note"
    KIND_CHOICES = [
        (KIND_HIGHLIGHT, "Highlight"),
        (KIND_FREEHAND, "Freehand"),
        (KIND_AREA, "Boxed area"),
        (KIND_STRIKE, "Strikeout"),
        (KIND_NOTE, "Note pin"),
    ]

    COLOR_CHOICES = [(c["key"], c["label"]) for c in MARKUP_COLORS]

    envelope = models.ForeignKey(
        "accounts.Envelope", on_delete=models.CASCADE, related_name="annotations"
    )
    document = models.ForeignKey(
        "accounts.EnvelopeDocument", on_delete=models.CASCADE, related_name="annotations"
    )
    page = models.PositiveSmallIntegerField(default=1)

    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_HIGHLIGHT)
    color = models.CharField(max_length=12, choices=COLOR_CHOICES, default=DEFAULT_COLOR)
    geometry = models.JSONField(default=dict, blank=True)

    text = models.TextField(blank=True, default="")

    # Author: a tokenized recipient, or a signed-in staff user, never both.
    recipient = models.ForeignKey(
        "accounts.EnvelopeRecipient",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="annotations",
    )
    author_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="esign_annotations",
    )
    author_name = models.CharField(max_length=150, blank=True, default="")

    is_internal = models.BooleanField(
        default=False, help_text="Visible to the sender's team only."
    )

    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by_name = models.CharField(max_length=150, blank=True, default="")

    #: Marks attest to a specific revision of the document. After a rework the
    #: page they pointed at may no longer exist, so the viewer shows only marks
    #: made against the current revision.
    revision = models.PositiveSmallIntegerField(default=1)

    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["document__order", "page", "created_at", "id"]
        indexes = [
            models.Index(fields=["envelope", "revision"]),
            models.Index(fields=["document", "page"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} p{self.page} — {self.display_name}"

    # -- display helpers ------------------------------------------------
    @property
    def display_name(self) -> str:
        if self.author_name:
            return self.author_name
        if self.recipient:
            return self.recipient.name
        if self.author_user:
            return self.author_user.get_full_name() or self.author_user.username
        return "Unknown"

    @property
    def initials(self) -> str:
        parts = [p for p in (self.display_name or "").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def role_label(self) -> str:
        if self.is_internal:
            return "Internal"
        if self.recipient:
            return self.recipient.get_role_display()
        return "Sender"

    @property
    def color_meta(self) -> dict:
        return MARKUP_COLOR_MAP.get(self.color, MARKUP_COLOR_MAP[DEFAULT_COLOR])

    @property
    def color_hex(self) -> str:
        return self.color_meta["hex"]

    @property
    def color_label(self) -> str:
        return self.color_meta["label"]

    @property
    def anchor(self) -> dict:
        """Bounding box used to sort thread cards top-to-bottom down the page."""
        geo = self.geometry or {}
        if self.kind == self.KIND_FREEHAND:
            return geo.get("bbox") or {"x": 0.0, "y": 0.0, "w": 0.02, "h": 0.02}
        if self.kind == self.KIND_NOTE:
            return {
                "x": _f(geo.get("x")),
                "y": _f(geo.get("y")),
                "w": 0.02,
                "h": 0.02,
            }
        return {
            "x": _f(geo.get("x")),
            "y": _f(geo.get("y")),
            "w": _f(geo.get("w"), 0.02),
            "h": _f(geo.get("h"), 0.02),
        }

    def owner_key(self) -> str:
        """Stable identity for 'is this mine?' checks across both access paths."""
        if self.recipient_id:
            return f"r:{self.recipient_id}"
        if self.author_user_id:
            return f"u:{self.author_user_id}"
        return ""

    # -- serialisation --------------------------------------------------
    def as_dict(self, viewer_key: str = "", can_moderate: bool = False) -> dict:
        mine = bool(viewer_key) and self.owner_key() == viewer_key
        return {
            "id": self.pk,
            "document_id": self.document_id,
            "page": self.page,
            "kind": self.kind,
            "color": self.color,
            "color_hex": self.color_hex,
            "color_label": self.color_label,
            "geometry": self.geometry or {},
            "anchor": self.anchor,
            "text": self.text,
            "author": self.display_name,
            "initials": self.initials,
            "role": self.role_label,
            "is_internal": self.is_internal,
            "resolved": self.resolved,
            "resolved_by": self.resolved_by_name,
            "created_at": self.created_at.isoformat(),
            "created_display": timezone.localtime(self.created_at).strftime("%d %b %Y, %H:%M"),
            "mine": mine,
            "can_delete": mine or can_moderate,
            "can_resolve": True,
            "replies": [
                r.as_dict(viewer_key=viewer_key, can_moderate=can_moderate)
                for r in self.replies.all()
            ],
        }


class EnvelopeAnnotationReply(models.Model):
    """One turn in the thread hanging off a mark."""

    annotation = models.ForeignKey(
        EnvelopeAnnotation, on_delete=models.CASCADE, related_name="replies"
    )
    recipient = models.ForeignKey(
        "accounts.EnvelopeRecipient",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="annotation_replies",
    )
    author_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="esign_annotation_replies",
    )
    author_name = models.CharField(max_length=150, blank=True, default="")

    text = models.TextField()
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name_plural = "Envelope annotation replies"

    def __str__(self):
        return f"{self.display_name}: {self.text[:40]}"

    @property
    def display_name(self) -> str:
        if self.author_name:
            return self.author_name
        if self.recipient:
            return self.recipient.name
        if self.author_user:
            return self.author_user.get_full_name() or self.author_user.username
        return "Unknown"

    @property
    def initials(self) -> str:
        parts = [p for p in (self.display_name or "").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def owner_key(self) -> str:
        if self.recipient_id:
            return f"r:{self.recipient_id}"
        if self.author_user_id:
            return f"u:{self.author_user_id}"
        return ""

    def as_dict(self, viewer_key: str = "", can_moderate: bool = False) -> dict:
        mine = bool(viewer_key) and self.owner_key() == viewer_key
        return {
            "id": self.pk,
            "text": self.text,
            "author": self.display_name,
            "initials": self.initials,
            "created_at": self.created_at.isoformat(),
            "created_display": timezone.localtime(self.created_at).strftime("%d %b %Y, %H:%M"),
            "mine": mine,
            "can_delete": mine or can_moderate,
        }
