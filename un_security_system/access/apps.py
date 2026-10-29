from django.apps import AppConfig


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "access"
    verbose_name = "Access Studio (roles, groups & access)"

    def ready(self):
        from django.db.models.signals import m2m_changed, post_delete, post_save

        from . import registry
        from .models import AccessGroup, AccessRole, RoleAssignment
        from .services import bump

        registry.autodiscover()

        for model in (AccessRole, AccessGroup, RoleAssignment):
            post_save.connect(bump, sender=model, dispatch_uid=f"access_bump_save_{model.__name__}")
            post_delete.connect(bump, sender=model, dispatch_uid=f"access_bump_del_{model.__name__}")
        for through in (AccessRole.inherits.through, AccessGroup.members.through, AccessGroup.roles.through):
            m2m_changed.connect(bump, sender=through, dispatch_uid=f"access_bump_m2m_{through.__name__}")
