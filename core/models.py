from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone


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
        return self.allocations.filter(project__status=ProjectStatus.ACTIVE).aggregate(
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

    @transaction.atomic
    def tear_down(self, outcomes):
        """Archive this project, deciding the fate of everything it holds.

        `outcomes` is an iterable of (ProjectPart, returned, soldered, broken).
        Every line must be accounted for exactly: the three numbers must sum to
        that line's `remaining`. Returned parts become available again; soldered
        and broken ones leave `qty_owned` permanently.

        All of it lands in one transaction. A partial teardown would leave every
        quantity in the app silently wrong.
        """
        if self.status != ProjectStatus.ACTIVE:
            raise ValidationError("This project has already been torn down.")

        outcomes = list(outcomes)
        seen = set()
        for line, returned, soldered, broken in outcomes:
            if line.project_id != self.pk:
                raise ValidationError(
                    "Allocation line does not belong to this project."
                )
            if min(returned, soldered, broken) < 0:
                raise ValidationError("Quantities cannot be negative.")
            if returned + soldered + broken != line.remaining:
                raise ValidationError(
                    f"{line.part}: must account for exactly {line.remaining}, "
                    f"got {returned + soldered + broken}."
                )
            seen.add(line.pk)

        expected = set(self.lines.values_list("pk", flat=True))
        if seen != expected:
            raise ValidationError("Every allocation line must be accounted for.")

        for line, returned, soldered, broken in outcomes:
            line.qty_returned += returned
            line.qty_soldered += soldered
            line.qty_broken += broken
            line.save(update_fields=["qty_returned", "qty_soldered", "qty_broken"])

            lost = soldered + broken
            if lost:
                Part.objects.filter(pk=line.part_id).update(
                    qty_owned=models.F("qty_owned") - lost
                )

        self.status = ProjectStatus.ARCHIVED
        self.archived_at = timezone.now()
        self.save(update_fields=["status", "archived_at"])

    def teardown_summary(self):
        """What this build cost, for the archived view."""
        return self.lines.aggregate(
            returned=models.Sum("qty_returned"),
            soldered=models.Sum("qty_soldered"),
            broken=models.Sum("qty_broken"),
        )


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
    qty_wanted = models.PositiveIntegerField(
        default=1,
        help_text="How many this build needs.",
    )
    qty_allocated = models.PositiveIntegerField(
        help_text="How many it actually got. Less than wanted means short.",
    )
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
                condition=Q(qty_wanted__gt=0),
                name="wanted_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(qty_allocated__lte=F("qty_wanted")),
                name="allocated_not_over_wanted",
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

    def __init__(self, *args, **kwargs):
        # Building a line with only an allocation means it got everything it
        # asked for, which is the common case and reads better than making
        # every caller repeat the number twice. Keyword-only on purpose:
        # Django loads rows from the database positionally, and that path must
        # keep whatever is actually stored.
        if "qty_allocated" in kwargs and "qty_wanted" not in kwargs:
            kwargs["qty_wanted"] = kwargs["qty_allocated"]
        super().__init__(*args, **kwargs)

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
    def short(self):
        """How many more this build needs than it managed to take.

        Non-zero means you ran out. The parts are not held by anyone - they
        do not exist yet, which is exactly what a shopping list is for.
        """
        return self.qty_wanted - self.qty_allocated

    @property
    def lost(self):
        """Gone for good: soldered into the board or burned out."""
        return self.qty_soldered + self.qty_broken

    def clean(self):
        """You cannot allocate parts you do not have.

        `available` for this line means qty_owned minus what *other* active
        projects are holding - a line must not count against itself, or editing
        an existing allocation would always look like an over-allocation.
        """
        if not self.part_id:
            return

        # Django runs model clean() even when a form field failed validation,
        # so any of these can still be None at this point.
        if self.qty_allocated is None:
            return
        self.qty_returned = self.qty_returned or 0
        self.qty_soldered = self.qty_soldered or 0
        self.qty_broken = self.qty_broken or 0

        accounted = self.qty_returned + self.qty_soldered + self.qty_broken
        if accounted > self.qty_allocated:
            raise ValidationError(
                f"Returned + soldered + broken is {accounted}, which is more "
                f"than the {self.qty_allocated} allocated."
            )

        if self.project_id and not self.project.is_active:
            return

        held_elsewhere = (
            ProjectPart.objects.filter(
                part_id=self.part_id,
                project__status=ProjectStatus.ACTIVE,
            )
            .exclude(pk=self.pk)
            .aggregate(
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
        )

        available = self.part.qty_owned - held_elsewhere
        wanted = self.qty_allocated - accounted
        if wanted > available:
            raise ValidationError(
                {
                    "qty_allocated": (
                        f"Only {available} of {self.part} available - "
                        f"you own {self.part.qty_owned} and other active "
                        f"projects are holding {held_elsewhere}."
                    )
                }
            )
