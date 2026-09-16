from django import template
from incidents.models import CommonServiceRequestType

register = template.Library()


@register.simple_tag
def csr_active_types():
    return CommonServiceRequestType.objects.filter(is_active=True).order_by(
        "sort_order", "name"
    )


@register.simple_tag
def csr_all_types():
    return CommonServiceRequestType.objects.all().order_by("sort_order", "name")
