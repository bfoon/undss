# accounts/urls_esign_bfoon.py
"""BFoon, the NES reading assistant."""
from django.urls import path

from . import views_esign_bfoon as V

urlpatterns = [
    path("esign/bfoon/search/", V.bfoon_search, name="esign_bfoon_search"),
    path("esign/bfoon/recent/", V.bfoon_recent, name="esign_bfoon_recent"),
    path("esign/s/<str:token>/bfoon/text/", V.bfoon_text_token, name="esign_bfoon_text_token"),
    path("esign/s/<str:token>/bfoon/ask/", V.bfoon_ask_token, name="esign_bfoon_ask_token"),
    path("esign/<int:pk>/bfoon/text/", V.bfoon_text, name="esign_bfoon_text"),
    path("esign/<int:pk>/bfoon/ask/", V.bfoon_ask, name="esign_bfoon_ask"),
]
