from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView

from .forms import ICTUserCreateForm
from .permissions import is_ict_focal

User = get_user_model()

class ICTUserGuardMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return is_ict_focal(self.request.user)


class ICTUserListView(ICTUserGuardMixin, ListView):
    template_name = "accounts/ict/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        # ICT focal can only see users in their agency
        me = self.request.user
        qs = User.objects.all().select_related('agency')
        if me.agency_id:
            qs = qs.filter(agency_id=me.agency_id)
        else:
            qs = qs.none()
        # optional search
        q = (self.request.GET.get('q') or '').strip()
        if q:
            qs = qs.filter(
                models.Q(username__icontains=q) |
                models.Q(first_name__icontains=q) |
                models.Q(last_name__icontains=q) |
                models.Q(email__icontains=q)
            )
        return qs.order_by('last_name', 'first_name', 'username')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        me = self.request.user
        ctx['my_agency'] = me.agency
        ctx['q'] = self.request.GET.get('q', '')
        return ctx


class ICTUserCreateView(ICTUserGuardMixin, CreateView):
    template_name = "accounts/ict/user_form.html"
    form_class = ICTUserCreateForm
    success_url = reverse_lazy("accounts:ict_user_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request_user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        resp = super().form_valid(form)
        messages.success(self.request, "User created. They currently have no password; set one from admin or send a set-password link.")
        return resp
