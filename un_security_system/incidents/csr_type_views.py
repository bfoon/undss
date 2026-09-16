from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from .models import CommonServiceRequest, CommonServiceRequestType


def _require_superuser(request):
    if request.user.is_superuser:
        return None
    messages.error(request, "Only the platform superuser can manage CSR request types.")
    return redirect("incidents:cs_support")


@login_required
@require_http_methods(["GET", "POST"])
def csr_request_type_list(request):
    denied = _require_superuser(request)
    if denied:
        return denied

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        description = (request.POST.get("description") or "").strip()
        requires_window = request.POST.get("requires_disruption_window") == "on"

        try:
            sort_order = max(0, int(request.POST.get("sort_order") or 100))
        except (TypeError, ValueError):
            sort_order = 100

        if not name:
            messages.error(request, "Request type name is required.")
            return redirect("incidents:csr_type_list")

        try:
            item = CommonServiceRequestType.objects.create(
                name=name,
                description=description,
                sort_order=sort_order,
                requires_disruption_window=requires_window,
                is_active=True,
                created_by=request.user,
            )
        except IntegrityError:
            messages.error(request, f'A request type named "{name}" already exists.')
        else:
            messages.success(
                request,
                f'CSR request type "{item.name}" was added and is now available.',
            )

        return redirect("incidents:csr_type_list")

    items = list(CommonServiceRequestType.objects.order_by("sort_order", "name"))
    counts = {
        row["category"]: row["total"]
        for row in (
            CommonServiceRequest.objects
            .values("category")
            .annotate(total=Count("id"))
        )
    }
    for item in items:
        item.request_count = counts.get(item.code, 0)

    return render(
        request,
        "common_services/csr_request_types.html",
        {"request_types": items},
    )


@login_required
@require_http_methods(["GET", "POST"])
def csr_request_type_edit(request, pk):
    denied = _require_superuser(request)
    if denied:
        return denied

    item = get_object_or_404(CommonServiceRequestType, pk=pk)

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        description = (request.POST.get("description") or "").strip()

        try:
            sort_order = max(0, int(request.POST.get("sort_order") or 100))
        except (TypeError, ValueError):
            sort_order = 100

        if not name:
            messages.error(request, "Request type name is required.")
            return redirect("incidents:csr_type_edit", pk=item.pk)

        item.name = name
        item.description = description
        item.sort_order = sort_order
        item.requires_disruption_window = (
            request.POST.get("requires_disruption_window") == "on"
        )
        item.is_active = request.POST.get("is_active") == "on"

        try:
            item.save()
        except IntegrityError:
            messages.error(request, f'A request type named "{name}" already exists.')
        else:
            messages.success(request, f'CSR request type "{item.name}" was updated.')
            return redirect("incidents:csr_type_list")

    return render(
        request,
        "common_services/csr_request_type_form.html",
        {"item": item},
    )


@login_required
@require_POST
def csr_request_type_toggle(request, pk):
    denied = _require_superuser(request)
    if denied:
        return denied

    item = get_object_or_404(CommonServiceRequestType, pk=pk)
    item.is_active = not item.is_active
    item.save(update_fields=["is_active", "updated_at"])

    state = "enabled" if item.is_active else "disabled"
    messages.success(request, f'CSR request type "{item.name}" has been {state}.')
    return redirect("incidents:csr_type_list")
