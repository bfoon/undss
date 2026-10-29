# access/compat.py
"""
Make the old fixed-role checks dynamic without touching them.

UN PASS checks `user.role` in Python, in templates and in database queries:

    if user.role == "lsa": …
    if getattr(user, "role", "") in ("lsa", "soc"): …
    {% if user.role == 'registry' %} …
    User.objects.filter(role__in=["lsa", "soc"])

Two small pieces turn all of those into "does this person act as …?":

RoleField / RoleValue
    `user.role` still reads as the stored base role ("requester", "lsa" …) —
    printing it, saving it and forms are unchanged — but comparing it asks the
    access engine. A Requester who holds a custom role with the power
    "Acts as Registry" now passes `user.role == "registry"`.

AccessUserQuerySet
    Rewrites `role=` / `role__in=` / `role__exact=` lookups (also inside Q
    objects, and in exclude()) on User querysets to "stored role OR acts as it
    through a dynamic role". So "notify every LSA" also reaches the people who
    were given LSA powers.

What it can't see: substring tests against a string (`role in "lsa,soc"` in a
template) and set/dict lookups by hash. The supplied templates use the
`acts_as` filter for those instead.
"""

from django.contrib.auth.models import UserManager
from django.db import models
from django.db.models import Q
from django.db.models.query_utils import DeferredAttribute

ROLE_LOOKUPS = {"role", "role__exact", "role__in", "role__iexact"}


class RoleValue(str):
    """The stored role string, whose equality means "acts as"."""

    __slots__ = ("_user",)

    def __new__(cls, raw, user):
        obj = super().__new__(cls, raw)
        obj._user = user
        return obj

    def _held(self):
        user = self._user
        if user is None or not getattr(user, "pk", None):
            return {str.__str__(self)}
        from .services import legacy_codes

        return legacy_codes(user)

    def __eq__(self, other):
        if isinstance(other, str):
            other = str.__str__(other)
            # Equal to the stored value always (keeps dict lookups, choices and
            # get_role_display exact) — and to every old role it acts as.
            return other == str.__str__(self) or other in self._held()
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    __hash__ = str.__hash__

    # Normalising keeps the meaning: user.role.lower() == "lsa" still works.
    def lower(self):
        return RoleValue(str.lower(self), self._user)

    def upper(self):
        return RoleValue(str.upper(self), self._user)

    def strip(self, chars=None):
        return RoleValue(str.strip(self, chars), self._user)

    def casefold(self):
        return RoleValue(str.casefold(self), self._user)

    def __reduce__(self):
        return (str, (str.__str__(self),))

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


class RoleDescriptor(DeferredAttribute):
    """
    A *data* descriptor (it defines __set__), so it runs on every read even
    though Django keeps field values in the instance __dict__.
    """

    def __get__(self, instance, cls=None):
        if instance is None:
            return self
        attname = self.field.attname
        if attname not in instance.__dict__:
            super().__get__(instance, cls)  # load a deferred value
        value = instance.__dict__.get(attname)
        if not isinstance(value, str) or isinstance(value, RoleValue):
            return value
        return RoleValue(value, instance)

    def __set__(self, instance, value):
        if isinstance(value, RoleValue):
            value = str.__str__(value)
        instance.__dict__[self.field.attname] = value


class RoleField(models.CharField):
    """A CharField whose value compares through the access engine."""

    descriptor_class = RoleDescriptor

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return str.__str__(value) if isinstance(value, str) else value

    def deconstruct(self):
        # Same column, same migrations: present it as a plain CharField.
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.CharField", args, kwargs


def _codes(value):
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str.__str__(v) if isinstance(v, str) else v for v in value]
    return [str.__str__(value) if isinstance(value, str) else value]


def _role_q(lookup, value):
    from .services import legacy_holder_ids

    codes = _codes(value)
    if lookup == "role__iexact":
        codes = [c.lower() for c in codes if isinstance(c, str)]
    ids = legacy_holder_ids(codes)
    q = Q(role__in=codes)
    if ids:
        q |= Q(pk__in=sorted(ids))
    return q


def _rewrite_q(q):
    new = Q()
    new.connector = q.connector
    new.negated = q.negated
    for child in q.children:
        if isinstance(child, Q):
            new.children.append(_rewrite_q(child))
        elif isinstance(child, tuple) and len(child) == 2 and child[0] in ROLE_LOOKUPS:
            new.children.append(_role_q(*child))
        else:
            new.children.append(child)
    return new


class AccessUserQuerySet(models.QuerySet):
    def _filter_or_exclude_inplace(self, negate, args, kwargs):
        if kwargs and ROLE_LOOKUPS & set(kwargs):
            kwargs = dict(kwargs)
            extra = [_role_q(k, kwargs.pop(k)) for k in list(kwargs) if k in ROLE_LOOKUPS]
            args = tuple(args) + tuple(extra)
        if args:
            args = tuple(_rewrite_q(a) if isinstance(a, Q) else a for a in args)
        return super()._filter_or_exclude_inplace(negate, args, kwargs)


class AccessUserManager(UserManager.from_queryset(AccessUserQuerySet)):
    """UserManager whose querysets understand dynamic roles."""

    use_in_migrations = True
