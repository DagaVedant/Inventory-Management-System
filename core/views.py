from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, ProtectedError, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .forms import AllocationForm, PartForm, TeardownFormSet
from .models import Part, Project, ProjectPart, ProjectStatus


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

    def get_queryset(self):
        qs = super().get_queryset().with_availability().order_by("name")
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(value__icontains=query)
                | Q(package__icontains=query)
                | Q(tags__icontains=query)
                | Q(notes__icontains=query)
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
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
        response = super().form_valid(form)
        messages.success(self.request, f"Added {self.object}.")
        return response

    def get_success_url(self):
        # You are entering a whole bin in one sitting. Land back on an empty
        # form with the cursor in the name field rather than on a list page.
        if "_addanother" in self.request.POST:
            return reverse("part_create")
        return reverse("part_list")


class PartUpdateView(OwnedMixin, UpdateView):
    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def form_valid(self, form):
        messages.success(self.request, f"Saved {form.instance}.")
        return super().form_valid(form)

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

        form = AllocationForm(request.POST, project=project)
        if form.is_valid():
            line = form.save()
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
    line = get_object_or_404(ProjectPart, pk=line_pk, project=project)

    try:
        qty = int(request.POST.get("qty", 0))
    except (TypeError, ValueError):
        qty = 0

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
        formset = TeardownFormSet(request.POST)
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
            initial=[
                {"line_id": line.pk, "qty_returned": line.remaining}
                for line in lines
            ]
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
