import re
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone


def normalise_tags(raw):
    """One canonical spelling of a tag string.

    Always ", " separated with duplicates collapsed, which is what lets the
    parts list filter on an exact tag in SQL instead of a substring match that
    would make "i2c" also match "i2c-pullup".
    """
    seen, out = set(), []
    for tag in (raw or "").split(","):
        tag = tag.strip()
        if not tag or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        out.append(tag)
    return ", ".join(out)


def tag_filter(tag):
    """Match one whole tag inside a normalised comma-separated string."""
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
    """A loose key for spotting the same component written two ways.

    "10k", "10 k", "10K" and "10kΩ" are one resistor as far as a human is
    concerned, and four separate rows as far as an exact match is concerned.

    Full stops are deliberately kept: stripping them would fold 4.7k into 47k
    and invent a duplicate that isn't one, which is worse than missing a real
    one.
    """

    def clean(text):
        text = unicodedata.normalize("NFKD", text or "").casefold()
        text = text.replace("μ", "u").replace("µ", "u")  # micro signs
        text = text.replace("ω", "")  # ohm sign, already casefolded
        # Word boundary on purpose: folds "10 kohms" down to "10k" without
        # mangling a part legitimately named something like "Ohmite".
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
        """Opening the history at the same moment the part exists.

        A part created with a quantity has to explain where that quantity came
        from, or its ledger starts one line short and every later reconciliation
        reports drift that was never real. Doing it here rather than in the
        views means nothing can forget: the seed command, the admin and the
        shell all get it. bulk_create() skips save() by design, so the importer
        writes its own opening lines.
        """
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

    @transaction.atomic
    def adjust_stock(self, delta, reason, project=None, note=""):
        """The only sanctioned way to change qty_owned.

        qty_owned stays a stored column because every list page reads it, but
        nothing may move it except this method, which writes a StockMovement in
        the same transaction. That is what makes "the number is wrong and I
        can't tell why" answerable.
        """
        if delta == 0:
            return None

        # Lock the row: two deliveries logged at once must not both read the
        # same balance and write the same balance_after.
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
        """A delivery arrived: stock goes up and the want list comes down.

        Separate from adjust_stock because only a purchase satisfies a want. A
        teardown reversal also increases stock and should not quietly tell you
        that you no longer need to buy anything.
        """
        movement = self.adjust_stock(qty, MovementReason.PURCHASE, note=note)
        # Filtered in the database rather than on self.qty_to_buy, which may be
        # stale: an instance loaded before the want was set would otherwise skip
        # this and leave the part on the shopping list after it arrived.
        Part.objects.filter(pk=self.pk, qty_to_buy__gt=0).update(
            qty_to_buy=Greatest(F("qty_to_buy") - qty, Value(0))
        )
        self.refresh_from_db(fields=["qty_to_buy"])
        return movement

    def set_stock(self, new_total, reason=MovementReason.CORRECTION, note=""):
        """Set an absolute quantity. Recounts work this way; deliveries don't."""
        return self.adjust_stock(new_total - self.qty_owned, reason, note=note)

    def match_key(self):
        return match_key(self.name, self.value)

    @transaction.atomic
    def merge_into(self, target):
        """Fold this part into another and delete it.

        Everything moves: allocation lines, quantities, want list and history.
        Where both parts appear in the same project their lines are combined,
        because the unique constraint means one project cannot hold two lines
        for what is now one part.

        The history is carried over rather than discarded, and every
        balance_after on the target is recomputed in date order afterwards.
        Two interleaved running balances would otherwise be nonsense, and the
        ledger has to keep reconciling with qty_owned.
        """
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

        # Delete before recomputing, so the source's own row can't be counted.
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
            # Remembered so reopen() can unpick this teardown without also
            # undoing parts you handed back weeks earlier.
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
                # Through adjust_stock so the loss lands in the ledger with the
                # project that caused it, rather than the number just dropping.
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
        """Undo a teardown and put the project back on the bench.

        Teardown is the one operation here that destroys information, and it is
        two clicks from a list page. Being unable to take it back made a
        mis-click permanent, recoverable only by editing every affected part
        from memory.

        Soldered and broken are reversed in full, because nothing but a
        teardown ever sets them. Returns are only reversed as far as this
        teardown contributed, so parts handed back mid-build stay handed back.
        """
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
        """What this build cost, for the archived view."""
        return self.lines.aggregate(
            returned=models.Sum("qty_returned"),
            soldered=models.Sum("qty_soldered"),
            broken=models.Sum("qty_broken"),
        )


class StockMovement(models.Model):
    """One line of history for a part's quantity.

    Written only by Part.adjust_stock(). `balance_after` is stored rather than
    recomputed so the history reads like a bank statement, and so drift between
    the ledger and qty_owned is visible instead of theoretical.
    """

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
