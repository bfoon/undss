# accounts/urls_esign_identity.py
"""
NES signing identity routes.

Note what is absent: no path contains a user id or an identity pk. Every
endpoint resolves the identity from `request.user`, so there is no URL an
administrator could visit to manage somebody else's signature.
"""
from django.urls import path

from . import views_esign_identity as V

urlpatterns = [
    path("esign/signature/", V.identity_home, name="esign_identity_home"),
    path("esign/signature/enroll/", V.identity_enroll, name="esign_identity_enroll"),
    path("esign/signature/mark/", V.identity_mark, name="esign_identity_mark"),
    path("esign/signature/material/", V.identity_material, name="esign_identity_material"),
    path("esign/signature/challenge/", V.identity_challenge, name="esign_identity_challenge"),
    path("esign/signature/lookup/", V.identity_lookup, name="esign_identity_lookup"),
    path("esign/signature/retire/", V.identity_retire, name="esign_identity_retire"),
]
