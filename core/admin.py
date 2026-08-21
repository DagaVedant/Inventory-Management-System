from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

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
            return "-"
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
            return "-"
        # Links out to the real teardown page rather than reimplementing it
        # here. One screen, one implementation.
        url = reverse("project_teardown", args=[obj.pk])
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
        url = reverse("project_teardown", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">Tear down this project</a>', url
        )
