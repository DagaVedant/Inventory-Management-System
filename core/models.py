import re
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone


def normalise_tags(raw):
    seen, out = set(), []
    for tag in (raw or "").split(","):
        tag = tag.strip()
        if not tag or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        out.append(tag)
    return ", ".join(out)


def tag_filter(tag):
    tag = tag.strip()
    if not tag:
        return Q()
    return (
        Q(tags__iexact=tag)
        | Q(tags__istartswith=f"{tag}, ")
        | Q(tags__iendswith=f", {tag}")
        | Q(tags__icontains=f", {tag}, ")
    )


def match_key(name, value=""):

    def clean(text):
        text = unicodedata.normalize("NFKD", text or "").casefold()
        text = text.replace("μ", "u").replace("µ", "u")
        text = text.replace("ω", "")
        text = re.sub(r"ohms?\b", "", text)
        return re.sub(r"[\s\-_,/]+", "", text)

    return clean(name), clean(value)


class ProjectStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


class MovementReason(models.TextChoices):
    OPENING = "opening", "Opening balance"
    PURCHASE = "purchase", "Bought or found"
    CORRECTION = "correction", "Recount"
    TEARDOWN = "teardown", "Consumed by a teardown"
    REOPEN = "reopen", "Teardown reversed"
    MERGE = "merge", "Merged from a duplicate"


class PartQuerySet(models.QuerySet):
    def with_availability(self):
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
        help_text="Comma separated. Normalised on save so filtering is exact.",
    )
    qty_owned = models.PositiveIntegerField(
        default=0,
        help_text="Total still yours to use: loose plus held by active projects.",
    )
    qty_to_buy = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Wanted regardless of any project. Shortfall can only exist "
            "attached to a build; this is for 'I am running low on these'."
        ),
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

    @transaction.atomic
    def save(self, *args, **kwargs):
        creating = self._state.adding
        self.tags = normalise_tags(self.tags)
        super().save(*args, **kwargs)
        if creating and self.qty_owned:
            StockMovement.objects.create(
                part=self,
                delta=self.qty_owned,
                balance_after=self.qty_owned,
                reason=MovementReason.OPENING,
                note="Opening balance.",
            )

    def compute_held(self):
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

    @transaction.atomic
    def adjust_stock(self, delta, reason, project=None, note=""):
        if delta == 0:
            return None

        locked = Part.objects.select_for_update().get(pk=self.pk)
        new_total = locked.qty_owned + delta

        if new_total < 0:
            raise ValidationError(
                f"That would take {locked.name} to {new_total}. "
                f"You only own {locked.qty_owned}."
            )

        held = locked.compute_held()
        if new_total < held:
            raise ValidationError(
                f"Can't go below {held}: that many {locked.name} are held by "
                f"active projects."
            )

        Part.objects.filter(pk=locked.pk).update(qty_owned=new_total)
        self.qty_owned = new_total

        return StockMovement.objects.create(
            part=locked,
            delta=delta,
            balance_after=new_total,
            reason=reason,
            project=project,
            note=note,
        )

    @transaction.atomic
    def receive(self, qty, note=""):
        movement = self.adjust_stock(qty, MovementReason.PURCHASE, note=note)
        Part.objects.filter(pk=self.pk, qty_to_buy__gt=0).update(
            qty_to_buy=Greatest(F("qty_to_buy") - qty, Value(0))
        )
        self.refresh_from_db(fields=["qty_to_buy"])
        return movement

    def set_stock(self, new_total, reason=MovementReason.CORRECTION, note=""):
        return self.adjust_stock(new_total - self.qty_owned, reason, note=note)

    def match_key(self):
        return match_key(self.name, self.value)

    @transaction.atomic
    def merge_into(self, target):
        if target.pk == self.pk:
            raise ValidationError("A part can't be merged into itself.")
        if target.user_id != self.user_id:
            raise ValidationError("Both parts must belong to the same person.")

        for line in self.allocations.all():
            twin = ProjectPart.objects.filter(
                project_id=line.project_id, part=target
            ).first()
            if twin is None:
                line.part = target
                line.save(update_fields=["part"])
                continue

            twin.qty_wanted += line.qty_wanted
            twin.qty_allocated += line.qty_allocated
            twin.qty_returned += line.qty_returned
            twin.qty_soldered += line.qty_soldered
            twin.qty_broken += line.qty_broken
            if line.teardown_returned is not None:
                twin.teardown_returned = (
                    twin.teardown_returned or 0
                ) + line.teardown_returned
            twin.save()
            line.delete()

        moved_stock = self.qty_owned
        moved_history = self.movements.count()
        name_before = str(self)

        Part.objects.filter(pk=target.pk).update(
            qty_owned=F("qty_owned") + moved_stock,
            qty_to_buy=F("qty_to_buy") + self.qty_to_buy,
        )
        self.movements.update(part=target)

        Part.objects.filter(pk=self.pk).delete()
        target.refresh_from_db()

        running = 0
        for movement in target.movements.order_by("created_at", "pk"):
            running += movement.delta
            if movement.balance_after != running:
                movement.balance_after = running
                movement.save(update_fields=["balance_after"])

        StockMovement.objects.create(
            part=target,
            delta=0,
            balance_after=target.qty_owned,
            reason=MovementReason.MERGE,
            note=(
                f"Merged in {name_before}: {moved_stock} unit(s) and "
                f"{moved_history} history line(s)."
            ),
        )
        return target

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
            line.teardown_returned = returned
            line.save(
                update_fields=[
                    "qty_returned",
                    "qty_soldered",
                    "qty_broken",
                    "teardown_returned",
                ]
            )

            lost = soldered + broken
            if lost:
                line.part.adjust_stock(
                    -lost,
                    MovementReason.TEARDOWN,
                    project=self,
                    note=f"{soldered} soldered in, {broken} broken",
                )

        self.status = ProjectStatus.ARCHIVED
        self.archived_at = timezone.now()
        self.save(update_fields=["status", "archived_at"])

    @transaction.atomic
    def reopen(self):
        if self.is_active:
            raise ValidationError("This project is already on the bench.")

        for line in self.lines.select_related("part"):
            lost = line.qty_soldered + line.qty_broken

            if line.teardown_returned is not None:
                line.qty_returned -= line.teardown_returned
                line.teardown_returned = None
            line.qty_soldered = 0
            line.qty_broken = 0
            line.save(
                update_fields=[
                    "qty_returned",
                    "qty_soldered",
                    "qty_broken",
                    "teardown_returned",
                ]
            )

            if lost:
                line.part.adjust_stock(
                    lost,
                    MovementReason.REOPEN,
                    project=self,
                    note="Teardown reversed.",
                )

        self.status = ProjectStatus.ACTIVE
        self.archived_at = None
        self.save(update_fields=["status", "archived_at"])

    def teardown_summary(self):
        return self.lines.aggregate(
            returned=models.Sum("qty_returned"),
            soldered=models.Sum("qty_soldered"),
            broken=models.Sum("qty_broken"),
        )


class StockMovement(models.Model):
    part = models.ForeignKey(
        Part,
        on_delete=models.CASCADE,
        related_name="movements",
    )
    delta = models.IntegerField(help_text="Signed. Negative means it left.")
    balance_after = models.PositiveIntegerField()
    reason = models.CharField(max_length=20, choices=MovementReason.choices)
    project = models.ForeignKey(
        "Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movements",
        help_text="Set when a teardown caused this.",
    )
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["part", "-created_at"])]

    def __str__(self):
        sign = "+" if self.delta > 0 else ""
        return f"{sign}{self.delta} {self.part} ({self.get_reason_display()})"


class ProjectPart(models.Model):
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
        default=0,
        help_text="How many this build needs.",
    )
    qty_allocated = models.PositiveIntegerField(
        help_text="How many it actually got. Less than wanted means short.",
    )
    qty_returned = models.PositiveIntegerField(default=0)
    qty_soldered = models.PositiveIntegerField(default=0)
    qty_broken = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)
    teardown_returned = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "How much of qty_returned came from the teardown. Null until torn "
            "down. Soldered and broken need no equivalent: nothing but a "
            "teardown ever sets them."
        ),
    )

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

    def __str__(self):
        return f"{self.qty_allocated} × {self.part} in {self.project}"

    def save(self, *args, **kwargs):
        self.apply_defaults()
        super().save(*args, **kwargs)

    def apply_defaults(self):
        if self._state.adding and not self.qty_wanted:
            self.qty_wanted = self.qty_allocated

    @property
    def accounted(self):
        return self.qty_returned + self.qty_soldered + self.qty_broken

    @property
    def remaining(self):
        return self.qty_allocated - self.accounted

    @property
    def short(self):
        return self.qty_wanted - self.qty_allocated

    @property
    def lost(self):
        return self.qty_soldered + self.qty_broken

    def clean(self):
        self.apply_defaults()

        if not self.part_id:
            return

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
