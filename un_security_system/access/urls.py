from django.urls import path

from . import views

app_name = "access"

urlpatterns = [
    path("", views.hub, name="hub"),
    path("me/", views.me, name="me"),
    path("matrix/", views.matrix, name="matrix"),
    path("matrix/toggle/", views.matrix_toggle, name="matrix_toggle"),
    path("who-can/", views.who_can, name="who_can"),
    path("audit/", views.audit_log, name="audit"),
    path("roles/new/", views.role_create, name="role_create"),
    path("roles/<int:pk>/", views.role_detail, name="role_detail"),
    path("roles/<int:pk>/edit/", views.role_edit, name="role_edit"),
    path("roles/<int:pk>/archive/", views.role_archive, name="role_archive"),
    path("roles/<int:pk>/assign/", views.assign, name="assign"),
    path("assignments/<int:pk>/revoke/", views.revoke, name="revoke"),
    path("groups/new/", views.group_create, name="group_create"),
    path("groups/<int:pk>/", views.group_edit, name="group_edit"),
    path("requests/<int:pk>/decide/", views.request_decide, name="request_decide"),
]
