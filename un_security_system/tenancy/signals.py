"""
tenancy/signals.py
==================

Keeps the resolved-feature cache honest. Any change to a grant, an office, a
share or an admin role invalidates every cached feature set.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import CountryOffice, DirectoryShare, FeatureGrant, OfficeAdmin, SSOConfiguration
from .services import bump_cache

WATCHED = (FeatureGrant, CountryOffice, DirectoryShare, OfficeAdmin, SSOConfiguration)


@receiver(post_save)
def _invalidate_on_save(sender, **kwargs):
    if sender in WATCHED:
        bump_cache()


@receiver(post_delete)
def _invalidate_on_delete(sender, **kwargs):
    if sender in WATCHED:
        bump_cache()
