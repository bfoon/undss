# accounts/urls_esign_witness.py
"""NES cross-office witnessing routes.

The two machine endpoints are reachable by participating offices and are
authenticated by bearer token plus a signature check — see
views_esign_witness.py. The status page needs a login.
"""
from django.urls import path

from . import views_esign_witness as V

urlpatterns = [
    path(
        "esign/witness/receive/",
        V.witness_receive,
        name="esign_witness_receive",
    ),
    path(
        "esign/witness/lookup/",
        V.witness_lookup,
        name="esign_witness_lookup",
    ),
    path(
        "esign/witness/",
        V.witness_status,
        name="esign_witness_status",
    ),
]
