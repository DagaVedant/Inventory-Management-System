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
from django.utils.functional import cached_property
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
)

from .forms import (
    AddStockForm,
    AllocationForm,
    BulkPartImportForm,
    MergePartForm,
    PartForm,
    SignupForm,
    TagRenameForm,
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
    normalise_tags,
    tag_filter,
)
from .throttle import forget, too_many


class SignupView(CreateView):
    form_class = SignupForm
    template_name = "registration/signup.html"

    BUCKET = "signup"
    LIMIT = 5
    WINDOW = 60 * 60

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("dashboard")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if too_many(request, self.BUCKET, self.LIMIT, self.WINDOW):
            return render(request, "registration/throttled.html", status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        self.object = form.save()
        login(self.request, self.object)
        messages.success(
            self.request,
            "Account created. Add your first part whenever you're ready.",
        )
        return redirect("dashboard")


class ThrottledLoginView(auth_views.LoginView):
    BUCKET = "login"
    LIMIT = 10
    WINDOW = 5 * 60

    def post(self, request, *args, **kwargs):
        if too_many(request, self.BUCKET, self.LIMIT, self.WINDOW):
            return render(request, "registration/throttled.html", status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        forget(self.request, self.BUCKET)
        return super().form_valid(form)


def healthz(request):
    try:
        connection.ensure_connection()
    except OperationalError:
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})


class GuideView(TemplateView):
    template_name = "core/guide.html"


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs):
        user = self.request.user

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

        shortfall = sorted(
            shortfall.values(), key=lambda row: (-row["total"], row["name"])
        )

        return super().get_context_data(
            active=active,
            shortfall=shortfall,
            running_low=parts.order_by("available", "name")[:8],
            total_parts=parts.count(),
            total_short=sum(row["total"] for row in shortfall),
            archived_count=Project.objects.filter(
                user=user, status=ProjectStatus.ARCHIVED
            ).count(),
            **kwargs,
        )


class OwnedMixin(LoginRequiredMixin):
    def get_queryset(self):
        return super().get_queryset().filter(user=self.request.user)


class PartListView(OwnedMixin, ListView):
    model = Part
    template_name = "core/part_list.html"
    context_object_name = "parts"
    paginate_by = 100

    SORTABLE = {
        "name": ("Name", "name", False),
        "value": ("Value", "value", False),
        "package": ("Package", "package", False),
        "owned": ("Owned", "qty_owned", True),
        "held": ("Held", "held", True),
        "available": ("Available", "available", True),
    }

    def sort_key(self):
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

        tag = self.request.GET.get("tag", "").strip()
        if tag:
            qs = qs.filter(tag_filter(tag))

        key, descending = self.sort_key()
        field = self.SORTABLE[key][1]
        return qs.order_by(f"-{field}" if descending else field, "name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        key, descending = self.sort_key()

        columns = []
        for candidate, (label, _, numeric) in self.SORTABLE.items():
            params = self.request.GET.copy()
            params.pop("page", None)
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
        context["tag"] = self.request.GET.get("tag", "").strip()
        context["total_parts"] = Part.objects.filter(user=self.request.user).count()
        return context


class TagChoicesMixin:
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["known_tags"] = [tag for tag, _ in tag_counts(self.request.user)]
        return kwargs


class PartCreateView(TagChoicesMixin, OwnedMixin, CreateView):
    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def get_queryset(self):
        return Part.objects.filter(user=self.request.user)

    def form_valid(self, form):
        form.instance.user = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"Added {self.object}.")
        return response

    def get_success_url(self):
        if "_addanother" in self.request.POST:
            return reverse("part_create")
        return reverse("part_list")


class PartDetailView(OwnedMixin, DetailView):
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
        key = self.object.match_key()
        tags = self.object.tag_list()
        similar = Part.objects.none()
        if tags:
            query = Q()
            for tag in tags:
                query |= tag_filter(tag)
            similar = (
                Part.objects.filter(user=self.request.user)
                .filter(query)
                .exclude(pk=self.object.pk)
                .with_availability()
                .order_by("-available", "name")[:8]
            )
        context["similar"] = similar
        context["twins"] = [
            part
            for part in Part.objects.filter(user=self.request.user).exclude(
                pk=self.object.pk
            )
            if part.match_key() == key
        ]
        context["movements"] = self.object.movements.select_related("project")[:25]
        context["movement_count"] = self.object.movements.count()
        return context


@login_required
@require_POST
def part_add_stock(request, pk):
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


def tag_counts(user):
    counts = {}
    for tags in (
        Part.objects.filter(user=user).exclude(tags="").values_list("tags", flat=True)
    ):
        for tag in tags.split(", "):
            if tag:
                counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))


class TagIndexView(LoginRequiredMixin, FormView):
    template_name = "core/tag_index.html"
    form_class = TagRenameForm
    success_url = reverse_lazy("tag_index")

    @cached_property
    def tags(self):
        return tag_counts(self.request.user)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "tags": self.tags}

    def get_context_data(self, **kwargs):
        return super().get_context_data(tags=self.tags, **kwargs)

    def form_valid(self, form):
        old = form.cleaned_data["old"]
        new = form.cleaned_data["new"]

        affected = list(
            Part.objects.filter(user=self.request.user).filter(tag_filter(old))
        )
        for part in affected:
            kept = [t for t in part.tag_list() if t.casefold() != old.casefold()]
            if new:
                kept.extend(
                    t
                    for t in new.split(", ")
                    if t.casefold() not in {k.casefold() for k in kept}
                )
            part.tags = normalise_tags(", ".join(kept))
            part.save(update_fields=["tags"])

        if new:
            messages.success(
                self.request, f"Renamed {old} to {new} on {len(affected)} part(s)."
            )
        else:
            messages.success(
                self.request, f"Removed {old} from {len(affected)} part(s)."
            )
        return super().form_valid(form)


class PartDuplicatesView(LoginRequiredMixin, TemplateView):
    template_name = "core/part_duplicates.html"

    def get_context_data(self, **kwargs):
        parts = list(
            Part.objects.filter(user=self.request.user)
            .with_availability()
            .order_by("name")
        )

        groups = {}
        for part in parts:
            groups.setdefault(part.match_key(), []).append(part)

        duplicates = [members for members in groups.values() if len(members) > 1]
        duplicates.sort(key=lambda members: members[0].name)

        return super().get_context_data(
            duplicates=duplicates, checked=len(parts), **kwargs
        )


class PartMergeView(LoginRequiredMixin, FormView):
    template_name = "core/part_merge.html"
    form_class = MergePartForm

    @cached_property
    def source(self):
        return get_object_or_404(Part, pk=self.kwargs["pk"], user=self.request.user)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "source": self.source}

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            source=self.source,
            likely=[
                part
                for part in Part.objects.filter(user=self.request.user).exclude(
                    pk=self.source.pk
                )
                if part.match_key() == self.source.match_key()
            ],
            line_count=self.source.allocations.count(),
            history_count=self.source.movements.count(),
            **kwargs,
        )

    def form_valid(self, form):
        target = form.cleaned_data["target"]
        name_was = str(self.source)
        try:
            self.source.merge_into(target)
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(self.request, message)
            return self.form_invalid(form)
        messages.success(self.request, f"Merged {name_was} into {target}.")
        return redirect("part_detail", pk=target.pk)


@login_required
@require_POST
def part_want(request, pk):
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


class PartImportView(LoginRequiredMixin, FormView):
    template_name = "core/part_import.html"
    form_class = BulkPartImportForm
    success_url = reverse_lazy("part_list")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        created, topped_up = form.save()
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
        bits = []
        if created:
            bits.append(f"added {len(created)} part(s)")
        if topped_up:
            bits.append(f"topped up {topped_up}")
        messages.success(self.request, f"Import done: {' and '.join(bits)}.")
        return super().form_valid(form)


class PartUpdateView(TagChoicesMixin, OwnedMixin, UpdateView):
    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def form_valid(self, form):
        was = Part.objects.get(pk=self.object.pk).qty_owned
        now = form.cleaned_data["qty_owned"]
        form.instance.qty_owned = was
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
            messages.error(
                self.request,
                f"{self.object} is used by a project, so it can't be deleted. "
                f"Set its quantity to 0 instead.",
            )
            return HttpResponseRedirect(reverse("part_list"))


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


class ProjectDetailView(LoginRequiredMixin, FormView):
    template_name = "core/project_detail.html"
    form_class = AllocationForm

    @cached_property
    def project(self):
        return get_object_or_404(Project, pk=self.kwargs["pk"], user=self.request.user)

    def get_success_url(self):
        return reverse("project_detail", args=[self.project.pk])

    def get_form_kwargs(self):
        kwargs = {**super().get_form_kwargs(), "project": self.project}
        if self.request.method == "POST":
            kwargs["lock"] = True
        return kwargs

    def post(self, request, *args, **kwargs):
        if not self.project.is_active:
            messages.error(request, "This project has been torn down.")
            return HttpResponseRedirect(self.get_success_url())
        with transaction.atomic():
            return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        line = form.save()
        if line.short:
            messages.warning(
                self.request,
                f"{line.part}: took {line.qty_allocated} of "
                f"{line.qty_wanted}. {line.short} still to buy.",
            )
        else:
            messages.success(
                self.request, f"Allocated {line.qty_allocated} × {line.part}."
            )
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        lines = list(self.project.lines.select_related("part").order_by("part__name"))
        return super().get_context_data(
            project=self.project,
            lines=lines,
            total_held=sum(line.remaining for line in lines),
            total_short=sum(line.short for line in lines),
            summary=(
                self.project.teardown_summary() if not self.project.is_active else None
            ),
            **kwargs,
        )


@login_required
@require_POST
def line_remove(request, pk, line_pk):
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
    project = _get_project(request, pk)

    try:
        qty = int(request.POST.get("qty", 0))
    except (TypeError, ValueError):
        qty = 0

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


class ProjectReopenView(LoginRequiredMixin, TemplateView):
    template_name = "core/project_reopen.html"

    @cached_property
    def project(self):
        return get_object_or_404(Project, pk=self.kwargs["pk"], user=self.request.user)

    @cached_property
    def detail_url(self):
        return reverse("project_detail", args=[self.project.pk])

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and self.project.is_active:
            messages.warning(request, "That project is already on the bench.")
            return HttpResponseRedirect(self.detail_url)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        lines = list(self.project.lines.select_related("part").order_by("part__name"))
        return super().get_context_data(
            project=self.project,
            lines=[line for line in lines if line.lost or line.teardown_returned],
            coming_back=sum(line.lost for line in lines),
            detail_url=self.detail_url,
            **kwargs,
        )

    def post(self, request, *args, **kwargs):
        coming_back = sum(line.lost for line in self.project.lines.all())
        try:
            self.project.reopen()
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            messages.success(
                request,
                f"{self.project} is back on the bench. "
                f"{coming_back} part(s) restored to your inventory.",
            )
        return HttpResponseRedirect(self.detail_url)


@login_required
def project_teardown(request, pk):
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
