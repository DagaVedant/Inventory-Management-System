from django.conf import settings
from django.db import models
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce


class ProjectStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


class PartQuerySet(models.QuerySet):
    def with_availability(self):
        """Annotate `held` and `available` in a single query.

        held      = sum of un-accounted-for quantities across ACTIVE projects
        available = qty_owned - held
        """
        held_expr = Sum(
            F("allocations__qty_allocated")
            - F("allocations__qty_returned")
            - F("allocations__qty_soldered")
            - F("allocations__qty_broken"),
            filter=Q(allocations__project__status=ProjectStatus.ACTIVE),
        )
        return self.annotate(held=Coalesce(held_expr, 0)).annotate(
            available=F("qty_owned") - F("held")
        )


class Part(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="parts",
    )
    name = models.CharField(max_length=200)
    package = models.CharField(max_length=50, blank=True)
    value = models.CharField(max_length=50, blank=True)
    pin_count = models.IntegerField(null=True, blank=True)
    voltage = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    tags = models.CharField(
        max_length=200,
        blank=True,
        help_text="Comma separated. Also does duty as substitutes.",
    )
    qty_owned = models.PositiveIntegerField(
        default=0,
        help_text="Total still yours to use: loose plus held by active projects.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PartQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        bits = [self.name]
        if self.value:
            bits.append(self.value)
        if self.package:
            bits.append(self.package)
        return " · ".join(bits)

    def compute_held(self):
        """How many are locked inside active projects right now."""
        return self.allocations.filter(
            project__status=ProjectStatus.ACTIVE
        ).aggregate(
            held=Coalesce(
                Sum(
                    F("qty_allocated")
                    - F("qty_returned")
                    - F("qty_soldered")
                    - F("qty_broken")
                ),
                0,
            )
        )["held"]

    def compute_available(self):
        return self.qty_owned - self.compute_held()

    def clean(self):
        """qty_owned can never drop below what active projects are holding."""
        from django.core.exceptions import ValidationError

        if self.pk:
            held = self.compute_held()
            if self.qty_owned < held:
                raise ValidationError(
                    {
                        "qty_owned": (
                            f"Can't go below {held} - that many are currently "
                            f"held by active projects."
                        )
                    }
                )

    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]


class Project(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="projects",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=ProjectStatus.choices,
        default=ProjectStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def is_active(self):
        return self.status == ProjectStatus.ACTIVE


class ProjectPart(models.Model):
    """One line of a project's BOM: this part, this many, and what became of them."""

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    part = models.ForeignKey(
        Part,
        on_delete=models.PROTECT,
        related_name="allocations",
    )
    qty_allocated = models.PositiveIntegerField()
    qty_returned = models.PositiveIntegerField(default=0)
    qty_soldered = models.PositiveIntegerField(default=0)
    qty_broken = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["part__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "part"],
                name="unique_part_per_project",
            ),
            models.CheckConstraint(
                condition=Q(qty_allocated__gt=0),
                name="allocated_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(
                    qty_allocated__gte=F("qty_returned")
                    + F("qty_soldered")
                    + F("qty_broken")
                ),
                name="accounted_not_over_allocated",
            ),
        ]

    def __str__(self):
        return f"{self.qty_allocated} × {self.part} in {self.project}"

    @property
    def accounted(self):
        return self.qty_returned + self.qty_soldered + self.qty_broken

    @property
    def remaining(self):
        """Still held by the project - not yet returned, soldered or broken."""
        return self.qty_allocated - self.accounted

    @property
    def lost(self):
        """Gone for good: soldered into the board or burned out."""
        return self.qty_soldered + self.qty_broken
