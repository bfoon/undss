# accounts/urls_esign_pades.py
"""NES seal verification routes (PAdES, ETSI EN 319 142).

Kept in its own module so accounts/urls.py gains a single include line.
"""
from django.urls import path

from . import views_esign_pades as V

urlpatterns = [
    path(
        "esign/<int:pk>/seal/",
        V.esign_seal_verify,
        name="esign_seal_verify",
    ),
    path(
        "esign/<int:pk>/seal/json/",
        V.esign_seal_verify_json,
        name="esign_seal_verify_json",
    ),
    path(
        "esign/s/<str:token>/seal/",
        V.esign_token_seal_verify,
        name="esign_token_seal_verify",
    ),

    # Offline evidence package (ASiC-E container, ETSI EN 319 162)
    path(
        "esign/<int:pk>/evidence/",
        V.esign_evidence,
        name="esign_evidence",
    ),
    path(
        "esign/s/<str:token>/evidence/",
        V.esign_token_evidence,
        name="esign_token_evidence",
    ),
]
