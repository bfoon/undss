# accounts/admin_esign_agents.py
"""
Optional. Add to the bottom of accounts/admin.py, after the studio admin:

    from .admin_esign_agents import *  # noqa: F401,F403

Read-mostly, like the studio admin: runs and steps are a record of what an AI
did with a document. Change agents in the designer, where every save is
checked; switch one off here in an emergency with the "Switch off" action.
"""

from django.contrib import admin
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from .models_esign_agents import AgentFlow, AgentFlowShare, AgentRun, AgentStep, AgentSubscription

__all__ = ["AgentFlowAdmin", "AgentRunAdmin"]


def _link(url_name, pk, label):
    try:
        return format_html('<a href="{}" target="_blank">{}</a>', reverse(url_name, args=[pk]), label)
    except NoReverseMatch:
        return label


class AgentFlowShareInline(admin.TabularInline):
    model = AgentFlowShare
    extra = 0
    fields = ("user", "email", "role", "shared_by", "created_at")
    readonly_fields = ("shared_by", "created_at")
    raw_id_fields = ("user",)


class AgentSubscriptionInline(admin.TabularInline):
    model = AgentSubscription
    extra = 0
    fields = ("user", "is_active", "created_at")
    readonly_fields = ("created_at",)
    raw_id_fields = ("user",)


@admin.register(AgentFlow)
class AgentFlowAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "agency", "trigger", "is_enabled", "share_scope", "card_total", "version",
                    "updated_at", "open_link")
    list_filter = ("trigger", "is_enabled", "share_scope", "is_active", "agency")
    search_fields = ("name", "description", "created_by__username", "created_by__email")
    readonly_fields = ("graph", "version", "created_at", "updated_at", "imported_from")
    raw_id_fields = ("created_by",)
    inlines = [AgentFlowShareInline, AgentSubscriptionInline]
    actions = ["switch_off"]

    @admin.display(description="Cards")
    def card_total(self, obj):
        return obj.card_count

    @admin.display(description="")
    def open_link(self, obj):
        return _link("accounts:esign_agent_design", obj.pk, "Open designer")

    @admin.action(description="Switch off (stop running automatically)")
    def switch_off(self, request, queryset):
        n = queryset.update(is_enabled=False)
        self.message_user(request, f"{n} agent(s) switched off.")


class AgentStepInline(admin.TabularInline):
    model = AgentStep
    extra = 0
    can_delete = False
    fields = ("node_label", "kind", "status", "port", "message", "model", "tokens_in", "tokens_out", "started_at",
              "finished_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = ("reference", "flow_name", "run_as", "trigger", "status", "is_test", "tokens_total", "created_at",
                    "open_link")
    list_filter = ("status", "trigger", "is_test", "agency")
    search_fields = ("reference", "flow_name", "document_name", "run_as__username", "run_as__email")
    readonly_fields = [f.name for f in AgentRun._meta.fields]
    inlines = [AgentStepInline]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    @admin.display(description="Tokens")
    def tokens_total(self, obj):
        return obj.tokens_in + obj.tokens_out

    @admin.display(description="")
    def open_link(self, obj):
        return _link("accounts:esign_agent_run", obj.pk, "Open run")
