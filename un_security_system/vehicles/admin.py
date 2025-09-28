# vehicles/admin.py
from django.contrib import admin
from .models import AgencyApprover
@admin.register(AgencyApprover)
class AgencyApproverAdmin(admin.ModelAdmin):
    list_display = ('user','agency_name')
    search_fields = ('user__username','agency_name')
    list_filter = ('agency_name',)
