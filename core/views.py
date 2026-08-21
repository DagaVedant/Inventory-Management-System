from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection, transaction
from django.db.models import Count, F, ProtectedError, Q, Sum
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)

from .forms import (
    AddStockForm,
    AllocationForm,
    BulkPartImportForm,
    PartForm,
    SignupForm,
    TeardownFormSet,
    WantToBuyForm,
)
from .models import (
    MovementReason,
    Part,
    Project,
    ProjectPart,
    ProjectStatus,
    StockMovement,
)

# ----------------------------------------------------------------- accounts


class SignupView(CreateView):
    form_class = SignupForm
    template_name = "registration/signup.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("dashboard")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        # Deliberately not calling super(): ModelFormMixin.form_valid() insists
        # on a success_url or a get_absolute_url() on User, neither of which
        # exists, and we redirect ourselves anyway.
        self.object = form.save()
        # Straight in rather than bouncing them to a login form they just
        # filled the credentials for.
        login(self.request, self.object)
        messages.success(
            self.request,
            "Account created. Add your first part whenever you're ready.",
        )
        return redirect("dashboard")


class GuardedPasswordResetView(auth_views.PasswordResetView):
    """Django's reset view, but honest when there's nowhere to send mail.

    Without a mail server the stock view renders "check your inbox" and posts
    the link to a server log nobody reads. The check happens per request rather
    than when URLs are loaded, so setting EMAIL_HOST takes effect on the next
    request instead of needing a redeploy.
    """

    email_template_name = "registration/password_reset_email.txt"
    subject_template_name = "registration/password_reset_subject.txt"
    success_url = reverse_lazy("password_reset_done")

    def dispatch(self, request, *args, **kwargs):
        if not settings.EMAIL_CONFIGURED:
            return render(
                request,
                "registration/password_reset_unavailable.html",
                status=503,
            )
        return super().dispatch(request, *args, **kwargs)


def healthz(request):
    """Liveness probe for the platform. Deliberately not behind login.

    Checks the database too: a container that booted but can't reach Postgres
    is not healthy, and answering 200 would let it take traffic it can't serve.
    """
    try:
        connection.ensure_connection()
    except OperationalError:
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})


class GuideView(TemplateView):
    """How the app works. No login required: it holds nothing personal, and
    someone deciding whether to sign up should be able to read it first."""

    template_name = "core/guide.html"


@login_required
def dashboard(request):
    """What's on the bench, what you need to buy, what you're nearly out of."""
    user = request.user

    active = list(
        Project.objects.filter(user=user, status=ProjectStatus.ACTIVE)
        .annotate(n_lines=Count("lines"))
        .prefetch_related("lines")
        .order_by("-created_at")
    )
    for project in active:
        lines = project.lines.all()
        project.held_count = sum(line.remaining for line in lines)
        project.short_count = sum(line.short for line in lines)

    # One row per part you need to buy, from two sources that both count:
    # what live builds asked for and couldn't get, and what you've simply put
    # on the list. Totalled per part, because you buy per part, not per project.
    shortfall = {}

    for row in (
        ProjectPart.objects.filter(
            project__user=user, project__status=ProjectStatus.ACTIVE
        )
        .values("part_id", "part__name", "part__value")
        .annotate(short=Sum(F("qty_wanted") - F("qty_allocated")))
        .filter(short__gt=0)
    ):
        shortfall[row["part_id"]] = {
            "part_id": row["part_id"],
            "name": row["part__name"],
            "value": row["part__value"],
            "from_builds": row["short"],
            "wanted": 0,
        }

    parts = Part.objects.filter(user=user).with_availability()

    for part in parts.filter(qty_to_buy__gt=0):
        row = shortfall.setdefault(
            part.pk,
            {
                "part_id": part.pk,
                "name": part.name,
                "value": part.value,
                "from_builds": 0,
                "wanted": 0,
            },
        )
        row["wanted"] = part.qty_to_buy

    for row in shortfall.values():
        row["total"] = row["from_builds"] + row["wanted"]

    shortfall = sorted(shortfall.values(), key=lambda row: (-row["total"], row["name"]))

    running_low = parts.order_by("available", "name")[:8]

    return render(
        request,
        "core/dashboard.html",
        {
            "active": active,
            "shortfall": shortfall,
            "running_low": running_low,
            "total_parts": parts.count(),
            "total_short": sum(row["total"] for row in shortfall),
            "archived_count": Project.objects.filter(
                user=user, status=ProjectStatus.ARCHIVED
            ).count(),
        },
    )


class OwnedMixin(LoginRequiredMixin):
    """Every query in this app is scoped to whoever is logged in."""

    def get_queryset(self):
        return super().get_queryset().filter(user=self.request.user)


# --------------------------------------------------------------------- parts


class PartListView(OwnedMixin, ListView):
    model = Part
    template_name = "core/part_list.html"
    context_object_name = "parts"
    paginate_by = 100

    # Whitelisted so a sort parameter can never reach order_by() unchecked.
    # key -> (column heading, field or annotation to sort on, right-aligned)
    SORTABLE = {
        "name": ("Name", "name", False),
        "value": ("Value", "value", False),
        "package": ("Package", "package", False),
        "owned": ("Owned", "qty_owned", True),
        "held": ("Held", "held", True),
        "available": ("Available", "available", True),
    }

    def sort_key(self):
        """(key, descending), falling back to name ascending."""
        raw = self.request.GET.get("sort", "name")
        descending = raw.startswith("-")
        key = raw.lstrip("-")
        if key not in self.SORTABLE:
            return "name", False
        return key, descending

    def get_queryset(self):
        qs = super().get_queryset().with_availability()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(value__icontains=query)
                | Q(package__icontains=query)
                | Q(tags__icontains=query)
                | Q(notes__icontains=query)
            )

        key, descending = self.sort_key()
        field = self.SORTABLE[key][1]
        # Name as a tiebreak so paging is stable when the sort column ties,
        # which it will constantly on held and available.
        return qs.order_by(f"-{field}" if descending else field, "name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        key, descending = self.sort_key()

        columns = []
        for candidate, (label, _, numeric) in self.SORTABLE.items():
            params = self.request.GET.copy()
            params.pop("page", None)
            # Clicking the column you're already on flips the direction.
            params["sort"] = (
                f"-{candidate}" if candidate == key and not descending else candidate
            )
            columns.append(
                {
                    "label": label,
                    "url": f"?{params.urlencode()}",
                    "active": candidate == key,
                    "descending": candidate == key and descending,
                    "numeric": numeric,
                }
            )

        page_params = self.request.GET.copy()
        page_params.pop("page", None)

        context["columns"] = columns
        context["page_params"] = page_params.urlencode()
        context["query"] = self.request.GET.get("q", "")
        context["total_parts"] = Part.objects.filter(user=self.request.user).count()
        return context


class PartCreateView(OwnedMixin, CreateView):
    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def get_queryset(self):
        return Part.objects.filter(user=self.request.user)

    def form_valid(self, form):
        form.instance.user = self.request.user
        # Part.save() opens the ledger for us.
        response = super().form_valid(form)
        messages.success(self.request, f"Added {self.object}.")
        return response

    def get_success_url(self):
        # You are entering a whole bin in one sitting. Land back on an empty
        # form with the cursor in the name field rather than on a list page.
        if "_addanother" in self.request.POST:
            return reverse("part_create")
        return reverse("part_list")


class PartDetailView(OwnedMixin, DetailView):
    """Where a part actually is: which builds hold it, and what has eaten it."""

    model = Part
    template_name = "core/part_detail.html"
    context_object_name = "part"

    def get_queryset(self):
        return super().get_queryset().with_availability()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lines = self.object.allocations.select_related("project").order_by(
            "-project__created_at"
        )
        context["holding"] = [
            line for line in lines if line.project.is_active and line.remaining
        ]
        context["history"] = [line for line in lines if not line.project.is_active]
        context["total_lost"] = sum(line.lost for line in lines)
        context["add_stock_form"] = AddStockForm()
        context["want_form"] = WantToBuyForm(initial={"qty": self.object.qty_to_buy})
        context["movements"] = self.object.movements.select_related("project")[:25]
        context["movement_count"] = self.object.movements.count()
        return context


@login_required
@require_POST
def part_add_stock(request, pk):
    """A delivery arrived. Add to what you own without doing the arithmetic."""
    part = get_object_or_404(Part, pk=pk, user=request.user)
    form = AddStockForm(request.POST)

    if form.is_valid():
        qty = form.cleaned_data["qty"]
        wanted = part.qty_to_buy
        part.receive(qty)

        note = ""
        if wanted:
            note = (
                " Nothing left to buy."
                if not part.qty_to_buy
                else f" Still {part.qty_to_buy} to buy."
            )
        messages.success(
            request,
            f"Added {qty}. You now own {part.qty_owned} × {part.name}.{note}",
        )
    else:
        messages.error(request, "Enter how many arrived.")

    return redirect("part_detail", pk=part.pk)


@login_required
@require_POST
def part_want(request, pk):
    """Put a part on the shopping list without inventing a project for it."""
    part = get_object_or_404(Part, pk=pk, user=request.user)
    form = WantToBuyForm(request.POST)

    if form.is_valid():
        qty = form.cleaned_data["qty"]
        Part.objects.filter(pk=part.pk).update(qty_to_buy=qty)
        if qty:
            messages.success(request, f"{part.name} is on the shopping list: {qty}.")
        else:
            messages.success(request, f"Took {part.name} off the shopping list.")
    else:
        messages.error(request, "Enter how many you want to buy.")

    return redirect("part_detail", pk=part.pk)


@login_required
def part_import(request):
    """Paste a whole bin in one go.

    All or nothing: if any line is malformed the whole paste is rejected with
    the line numbers, because a half-applied import leaves you unable to tell
    what landed.
    """
    if request.method == "POST":
        form = BulkPartImportForm(request.POST, user=request.user)
        if form.is_valid():
            created = form.save()
            StockMovement.objects.bulk_create(
                [
                    StockMovement(
                        part=part,
                        delta=part.qty_owned,
                        balance_after=part.qty_owned,
                        reason=MovementReason.OPENING,
                        note="Imported.",
                    )
                    for part in created
                    if part.qty_owned
                ]
            )
            messages.success(request, f"Added {len(created)} parts.")
            return redirect("part_list")
    else:
        form = BulkPartImportForm(user=request.user)

    return render(request, "core/part_import.html", {"form": form})


class PartUpdateView(OwnedMixin, UpdateView):
    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def form_valid(self, form):
        # Editing the quantity here means "I counted, this is the real number",
        # so it goes into the ledger as a recount rather than changing silently.
        was = Part.objects.get(pk=self.object.pk).qty_owned
        now = form.cleaned_data["qty_owned"]
        form.instance.qty_owned = was  # let adjust_stock move it, not the save
        response = super().form_valid(form)

        if now != was:
            try:
                self.object.set_stock(now, note="Edited on the part form.")
            except ValidationError as exc:
                form.add_error("qty_owned", exc.messages)
                return self.form_invalid(form)

        messages.success(self.request, f"Saved {form.instance}.")
        return response

    def get_success_url(self):
        return reverse("part_list")


class PartDeleteView(OwnedMixin, DeleteView):
    model = Part
    template_name = "core/part_confirm_delete.html"
    success_url = reverse_lazy("part_list")

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ProtectedError:
            # on_delete=PROTECT: a part named in any project, live or archived,
            # is part of that record and can't quietly disappear from it.
            messages.error(
                self.request,
                f"{self.object} is used by a project, so it can't be deleted. "
                f"Set its quantity to 0 instead.",
            )
            return HttpResponseRedirect(reverse("part_list"))


# ------------------------------------------------------------------ projects


class ProjectListView(OwnedMixin, ListView):
    model = Project
    template_name = "core/project_list.html"
    context_object_name = "projects"

    def get_queryset(self):
        return super().get_queryset().annotate(n_lines=Count("lines"))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        projects = context["projects"]
        context["active"] = [p for p in projects if p.is_active]
        context["archived"] = [p for p in projects if not p.is_active]
        return context


class ProjectCreateView(OwnedMixin, CreateView):
    model = Project
    fields = ["name", "description"]
    template_name = "core/project_form.html"

    def get_queryset(self):
        return Project.objects.filter(user=self.request.user)

    def form_valid(self, form):
        form.instance.user = self.request.user
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("project_detail", args=[self.object.pk])


class ProjectUpdateView(OwnedMixin, UpdateView):
    model = Project
    fields = ["name", "description"]
    template_name = "core/project_form.html"

    def get_success_url(self):
        return reverse("project_detail", args=[self.object.pk])


class ProjectDeleteView(OwnedMixin, DeleteView):
    model = Project
    template_name = "core/project_confirm_delete.html"
    success_url = reverse_lazy("project_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = self.object
        context["lines"] = project.lines.select_related("part")
        context["total_held"] = sum(line.remaining for line in context["lines"])
        context["summary"] = project.teardown_summary()
        return context

    def form_valid(self, form):
        name = str(self.object)
        response = super().form_valid(form)
        messages.success(self.request, f"Deleted {name}.")
        return response


def _get_project(request, pk):
    return get_object_or_404(Project, pk=pk, user=request.user)


@login_required
def project_detail(request, pk):
    """The project's parts list. This is the BOM - there is no separate one."""
    project = _get_project(request, pk)
    lines = project.lines.select_related("part").order_by("part__name")

    if request.method == "POST":
        if not project.is_active:
            messages.error(request, "This project has been torn down.")
            return redirect("project_detail", pk=project.pk)

        # The availability check reads stock and then writes an allocation.
        # Without a transaction and a row lock, two submits landing together
        # can both pass the check and between them allocate more than exists.
        with transaction.atomic():
            form = AllocationForm(request.POST, project=project, lock=True)
            if form.is_valid():
                line = form.save()
                if line.short:
                    messages.warning(
                        request,
                        f"{line.part}: took {line.qty_allocated} of "
                        f"{line.qty_wanted}. {line.short} still to buy.",
                    )
                else:
                    messages.success(
                        request, f"Allocated {line.qty_allocated} × {line.part}."
                    )
                return redirect("project_detail", pk=project.pk)
    else:
        form = AllocationForm(project=project)

    return render(
        request,
        "core/project_detail.html",
        {
            "project": project,
            "lines": lines,
            "form": form,
            "total_held": sum(line.remaining for line in lines),
            "total_short": sum(line.short for line in lines),
            "summary": project.teardown_summary() if not project.is_active else None,
        },
    )


@login_required
@require_POST
def line_remove(request, pk, line_pk):
    """Un-allocate: the parts were never actually taken out of the bin."""
    project = _get_project(request, pk)
    line = get_object_or_404(ProjectPart, pk=line_pk, project=project)

    if not project.is_active:
        messages.error(request, "This project has been torn down.")
    elif line.accounted:
        messages.error(
            request,
            f"{line.part} already has parts accounted for, so this line is a "
            f"record now. Tear the project down instead.",
        )
    else:
        line.delete()
        messages.success(request, f"Removed {line.part} from {project}.")

    return redirect("project_detail", pk=project.pk)


@login_required
@require_POST
def line_return(request, pk, line_pk):
    """Hand some parts back mid-build, without tearing the project down."""
    project = _get_project(request, pk)

    try:
        qty = int(request.POST.get("qty", 0))
    except (TypeError, ValueError):
        qty = 0

    # Same read-then-write shape as allocation: lock the line so two returns
    # can't both see the same `remaining` and hand back more than is held.
    with transaction.atomic():
        line = get_object_or_404(
            ProjectPart.objects.select_for_update().select_related("part"),
            pk=line_pk,
            project=project,
        )

        if not project.is_active:
            messages.error(request, "This project has been torn down.")
        elif qty < 1:
            messages.error(request, "Enter how many are coming back.")
        elif qty > line.remaining:
            messages.error(
                request,
                f"{project} is only holding {line.remaining} × {line.part}.",
            )
        else:
            line.qty_returned += qty
            line.save(update_fields=["qty_returned"])
            messages.success(request, f"Returned {qty} × {line.part} to the shelf.")

    return redirect("project_detail", pk=project.pk)


@login_required
def project_reopen(request, pk):
    """Undo a teardown. Confirmation on GET, the real thing on POST."""
    project = _get_project(request, pk)
    detail_url = reverse("project_detail", args=[project.pk])

    if project.is_active:
        messages.warning(request, "That project is already on the bench.")
        return HttpResponseRedirect(detail_url)

    lines = list(project.lines.select_related("part").order_by("part__name"))
    coming_back = sum(line.lost for line in lines)

    if request.method == "POST":
        try:
            project.reopen()
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            messages.success(
                request,
                f"{project} is back on the bench. "
                f"{coming_back} part(s) restored to your inventory.",
            )
        return HttpResponseRedirect(detail_url)

    return render(
        request,
        "core/project_reopen.html",
        {
            "project": project,
            "lines": [line for line in lines if line.lost or line.teardown_returned],
            "coming_back": coming_back,
            "detail_url": detail_url,
        },
    )


@login_required
def project_teardown(request, pk):
    """Say what became of everything this project is holding, then archive it."""
    project = _get_project(request, pk)
    detail_url = reverse("project_detail", args=[project.pk])

    if not project.is_active:
        messages.warning(request, "That project has already been torn down.")
        return HttpResponseRedirect(detail_url)

    lines = list(project.lines.select_related("part").order_by("part__name"))
    if not lines:
        messages.warning(
            request, "Nothing allocated to this project - nothing to tear down."
        )
        return HttpResponseRedirect(detail_url)

    if request.method == "POST":
        formset = TeardownFormSet(request.POST, form_kwargs={"project": project})
        if formset.is_valid():
            outcomes = [
                (
                    f.cleaned_data["line"],
                    f.cleaned_data["qty_returned"],
                    f.cleaned_data["qty_soldered"],
                    f.cleaned_data["qty_broken"],
                )
                for f in formset
            ]
            try:
                project.tear_down(outcomes)
            except ValidationError as exc:
                for message in exc.messages:
                    messages.error(request, message)
            else:
                returned = sum(o[1] for o in outcomes)
                lost = sum(o[2] + o[3] for o in outcomes)
                messages.success(
                    request,
                    f"Tore down {project}. {returned} back on the shelf, "
                    f"{lost} gone for good.",
                )
                return HttpResponseRedirect(detail_url)
    else:
        formset = TeardownFormSet(
            form_kwargs={"project": project},
            initial=[
                {"line_id": line.pk, "qty_returned": line.remaining} for line in lines
            ],
        )

    lines_by_id = {line.pk: line for line in lines}
    rows = []
    for form in formset:
        raw = form["line_id"].value()
        try:
            rows.append((form, lines_by_id.get(int(raw))))
        except (TypeError, ValueError):
            rows.append((form, None))

    return render(
        request,
        "core/project_teardown.html",
        {
            "project": project,
            "formset": formset,
            "rows": rows,
            "total_held": sum(line.remaining for line in lines),
            "detail_url": detail_url,
        },
    )
