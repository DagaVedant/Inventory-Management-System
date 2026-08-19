from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .forms import TeardownFormSet
from .models import Part, Project, ProjectPart, ProjectStatus


class OwnedAdminMixin:
    """Scope rows to the logged-in user and stamp ownership on create."""

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    def save_model(self, request, obj, form, change):
        if not obj.user_id:
            obj.user = request.user
        super().save_model(request, obj, form, change)

    def get_exclude(self, request, obj=None):
        return ["user"]


@admin.register(Part)
class PartAdmin(OwnedAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "value",
        "package",
        "qty_owned",
        "held_display",
        "available_display",
        "tags",
    )
    list_filter = ("package",)
    search_fields = ("name", "value", "package", "tags", "notes")
    ordering = ("name",)
    list_per_page = 100
    fieldsets = (
        (None, {"fields": ("name", "qty_owned")}),
        ("Characteristics", {"fields": ("value", "package", "pin_count", "voltage")}),
        ("Extra", {"fields": ("tags", "notes")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).with_availability()

    @admin.display(description="Held", ordering="held")
    def held_display(self, obj):
        return obj.held

    @admin.display(description="Available", ordering="available")
    def available_display(self, obj):
        return obj.available


class ProjectPartInline(admin.TabularInline):
    model = ProjectPart
    extra = 1
    autocomplete_fields = ["part"]
    fields = (
        "part",
        "qty_allocated",
        "qty_returned",
        "qty_soldered",
        "qty_broken",
        "remaining_display",
        "note",
    )
    readonly_fields = ("remaining_display",)

    @admin.display(description="Still held")
    def remaining_display(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        return obj.remaining

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "part" and not request.user.is_superuser:
            kwargs["queryset"] = Part.objects.filter(user=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Project)
class ProjectAdmin(OwnedAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "status",
        "line_count",
        "created_at",
        "archived_at",
        "teardown_link",
    )
    list_filter = ("status",)
    search_fields = ("name", "description")
    inlines = [ProjectPartInline]
    fieldsets = (
        (None, {"fields": ("name", "description")}),
        ("State", {"fields": ("status", "archived_at", "teardown_button")}),
    )
    readonly_fields = ("archived_at", "teardown_button")

    @admin.display(description="Parts")
    def line_count(self, obj):
        return obj.lines.count()

    @admin.display(description="")
    def teardown_link(self, obj):
        if obj.status != ProjectStatus.ACTIVE:
            return "—"
        url = reverse("admin:core_project_teardown", args=[obj.pk])
        return format_html('<a class="button" href="{}">Tear down</a>', url)

    @admin.display(description="Teardown")
    def teardown_button(self, obj):
        if obj is None or obj.pk is None:
            return "Save the project first, then allocate parts to it."
        if obj.status != ProjectStatus.ACTIVE:
            s = obj.teardown_summary()
            return format_html(
                "Torn down. {} returned, {} soldered in, {} broken.",
                s["returned"] or 0,
                s["soldered"] or 0,
                s["broken"] or 0,
            )
        url = reverse("admin:core_project_teardown", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">Tear down this project</a>', url
        )

    # ---------------------------------------------------------------- teardown

    def get_urls(self):
        custom = [
            path(
                "<int:object_id>/teardown/",
                self.admin_site.admin_view(self.teardown_view),
                name="core_project_teardown",
            ),
        ]
        return custom + super().get_urls()

    def teardown_view(self, request, object_id):
        project = get_object_or_404(self.get_queryset(request), pk=object_id)

        if not self.has_change_permission(request, project):
            raise PermissionDenied

        change_url = reverse("admin:core_project_change", args=[project.pk])

        if project.status != ProjectStatus.ACTIVE:
            messages.warning(request, "That project has already been torn down.")
            return HttpResponseRedirect(change_url)

        lines = list(project.lines.select_related("part").order_by("part__name"))
        if not lines:
            messages.warning(
                request, "Nothing allocated to this project - nothing to tear down."
            )
            return HttpResponseRedirect(change_url)

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
                    for msg in exc.messages:
                        messages.error(request, msg)
                else:
                    returned = sum(o[1] for o in outcomes)
                    lost = sum(o[2] + o[3] for o in outcomes)
                    messages.success(
                        request,
                        f"Tore down “{project}”. {returned} part(s) back on the "
                        f"shelf, {lost} gone for good.",
                    )
                    return HttpResponseRedirect(change_url)
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

        context = {
            **self.admin_site.each_context(request),
            "title": f"Tear down: {project}",
            "opts": self.model._meta,
            "original": project,
            "project": project,
            "formset": formset,
            "rows": rows,
            "change_url": change_url,
            "total_held": sum(line.remaining for line in lines),
        }
        return TemplateResponse(
            request, "admin/core/project/teardown.html", context
        )
