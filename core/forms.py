from django import forms
from django.conf import settings
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.db.models import Case, IntegerField, Value, When

from .models import Part, ProjectPart, match_key


class SignupForm(UserCreationForm):
    """Account creation, optionally behind an invite code.

    With SIGNUP_CODE unset the code field isn't rendered at all and anyone with
    the URL can sign up. Set it in the environment and signup closes without a
    deploy.
    """

    signup_code = forms.CharField(
        required=False,
        label="Invite code",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )

    class Meta(UserCreationForm.Meta):
        fields = ("username",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not settings.SIGNUP_CODE:
            del self.fields["signup_code"]

    def clean_signup_code(self):
        expected = settings.SIGNUP_CODE
        given = (self.cleaned_data.get("signup_code") or "").strip()
        if expected and given != expected:
            raise forms.ValidationError("That invite code isn't right.")
        return given


def parse_parts_text(text):
    """Turn pasted lines into part fields.

    One part per line, comma separated::

        name, qty, value, package, tag, tag, ...

    Only name and quantity are required. Everything from the fifth field
    onward is rejoined as tags, which is what lets tags contain commas without
    anyone having to escape anything. Blank lines and lines starting with #
    are skipped.

    Returns (rows, errors). Errors are (line_number, message) so the form can
    point at the offending line rather than saying "something was wrong".
    """
    rows, errors = [], []
    seen = {}

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 2:
            errors.append((number, "Needs at least a name and a quantity."))
            continue

        name = fields[0]
        if not name:
            errors.append((number, "Missing a name."))
            continue
        if len(name) > 200:
            errors.append((number, "Name is longer than 200 characters."))
            continue

        try:
            qty = int(fields[1])
        except ValueError:
            errors.append((number, f"{fields[1]!r} is not a whole number."))
            continue
        if qty < 0:
            errors.append((number, "Quantity cannot be negative."))
            continue

        value = fields[2] if len(fields) > 2 else ""
        package = fields[3] if len(fields) > 3 else ""
        tags = ", ".join(f for f in fields[4:] if f)

        key = match_key(name, value)
        if key in seen:
            errors.append((number, f"Same part as line {seen[key]} - combine them."))
            continue
        seen[key] = number

        rows.append(
            {
                "name": name,
                "qty_owned": qty,
                "value": value[:50],
                "package": package[:50],
                "tags": tags[:200],
            }
        )

    return rows, errors


class BulkPartImportForm(forms.Form):
    """Paste a whole bin in at once instead of one form at a time."""

    text = forms.CharField(
        label="One part per line",
        widget=forms.Textarea(
            attrs={
                "rows": 14,
                "autofocus": True,
                "placeholder": (
                    "10k resistor, 180, 10k, through-hole, passive, resistor\n"
                    "DHT22, 4\n"
                    "ESP32 devkit, 2, , module, mcu, wifi"
                ),
            }
        ),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.rows = []

    def clean_text(self):
        text = self.cleaned_data["text"]
        rows, errors = parse_parts_text(text)

        if not errors and not rows:
            raise forms.ValidationError("Nothing to import.")

        # Nothing is created unless every line is good. A half-applied paste is
        # worse than a rejected one - you can't tell what landed.
        if self.user is not None:
            existing = {
                match_key(name, value): f"{name} {value}".strip()
                for name, value in Part.objects.filter(user=self.user).values_list(
                    "name", "value"
                )
            }
            for row in rows:
                clash = existing.get(match_key(row["name"], row["value"]))
                if clash:
                    errors.append(
                        (
                            0,
                            f"{row['name']} looks like the {clash} you already "
                            f"have. Remove the line, or import it under a name "
                            f"that isn't the same component.",
                        )
                    )

        if errors:
            raise forms.ValidationError(
                [
                    f"Line {number}: {message}" if number else message
                    for number, message in errors
                ]
            )

        self.rows = rows
        return text

    def save(self):
        parts = [Part(user=self.user, **row) for row in self.rows]
        return Part.objects.bulk_create(parts)


class AddStockForm(forms.Form):
    """Recording a delivery, not correcting a miscount.

    Corrections belong on the edit form where you set the absolute number;
    this only ever goes up, so there is no ambiguity about which you meant.
    """

    qty = forms.IntegerField(
        min_value=1,
        label="How many arrived",
        widget=forms.NumberInput(attrs={"min": 1, "style": "width:6em"}),
    )


class MergePartForm(forms.Form):
    """Pick the part to keep. The other one is folded into it and deleted."""

    target = forms.ModelChoiceField(
        queryset=Part.objects.none(),
        label="Keep this part",
        empty_label="Choose the one to keep",
    )

    def __init__(self, *args, source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source
        if source is not None:
            others = Part.objects.filter(user=source.user).exclude(pk=source.pk)
            # Likely matches first: whoever opened this page almost certainly
            # wants one of them, and the full list can be hundreds long.
            likely = [
                part.pk for part in others if part.match_key() == source.match_key()
            ]
            self.fields["target"].queryset = others.order_by(
                Case(
                    When(pk__in=likely, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                ),
                "name",
            )


class WantToBuyForm(forms.Form):
    """How many of this to buy, regardless of any project."""

    qty = forms.IntegerField(
        min_value=0,
        label="Want to buy",
        widget=forms.NumberInput(attrs={"min": 0, "style": "width:6em"}),
    )


class PartForm(forms.ModelForm):
    class Meta:
        model = Part
        fields = [
            "name",
            "qty_owned",
            "value",
            "package",
            "pin_count",
            "voltage",
            "tags",
            "notes",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"autofocus": True, "placeholder": "10k resistor"}
            ),
            "value": forms.TextInput(attrs={"placeholder": "10k, 100nF, 3V3"}),
            "package": forms.TextInput(attrs={"placeholder": "0805, DIP-8, module"}),
            "tags": forms.TextInput(attrs={"placeholder": "sensor, i2c, 3v3"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "value": "Free text. 10k, 4.7uF and 3V3 do not share a number type.",
            "qty_owned": "Everything you have, including parts inside active projects.",
        }


class AllocationForm(forms.Form):
    """Add a part to a project, or top up the line it already has.

    Deliberately a plain Form rather than a ModelForm: a ModelForm re-applies
    the submitted data to its instance in `_post_clean()`, which runs *after*
    `clean()` and would overwrite a topped-up quantity with the raw one.
    """

    part = forms.ModelChoiceField(queryset=Part.objects.none())
    qty_wanted = forms.IntegerField(
        min_value=1,
        initial=1,
        label="How many do you need",
        widget=forms.NumberInput(attrs={"min": 1}),
    )
    note = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "optional note"}),
    )

    def __init__(self, *args, project=None, lock=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.lock = lock
        if project is not None:
            self.fields["part"].queryset = Part.objects.filter(
                user=project.user
            ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        part = cleaned.get("part")
        qty = cleaned.get("qty_wanted")

        if part is None or qty is None or self.project is None:
            return cleaned

        if self.lock:
            # Take a row lock on the part before reading availability, so a
            # concurrent allocation of the same part waits for this one to
            # commit instead of racing it. Requires an open transaction; a
            # no-op on SQLite, which is fine because it serializes writes
            # anyway.
            part = Part.objects.select_for_update().get(pk=part.pk)
            cleaned["part"] = part

        line = ProjectPart.objects.filter(project=self.project, part=part).first()
        if line is None:
            line = ProjectPart(
                project=self.project,
                part=part,
                qty_wanted=0,
                qty_allocated=0,
                note=cleaned.get("note", ""),
            )
        elif cleaned.get("note"):
            # Already on this project - top it up rather than rejecting the
            # submission on the unique constraint.
            line.note = cleaned["note"]

        # Everything this part has that isn't already spoken for by some other
        # active project. This line's own holding doesn't count against it.
        held_elsewhere = part.compute_held() - (line.remaining if line.pk else 0)
        capacity = part.qty_owned - held_elsewhere

        line.qty_wanted += qty
        # Take what's there and record the rest as short, rather than refusing
        # the whole request. Running out is information, not an error.
        line.qty_allocated = min(line.qty_wanted, capacity + line.accounted)

        try:
            line.full_clean(validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages) from exc

        cleaned["line"] = line
        return cleaned

    def save(self):
        line = self.cleaned_data["line"]
        line.save()
        return line


class TeardownLineForm(forms.Form):
    """One row of the teardown screen: what became of this part."""

    line_id = forms.IntegerField(widget=forms.HiddenInput)
    qty_returned = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Returned",
        widget=forms.NumberInput(attrs={"min": 0, "style": "width:5em"}),
    )
    qty_soldered = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Soldered in",
        widget=forms.NumberInput(attrs={"min": 0, "style": "width:5em"}),
    )
    qty_broken = forms.IntegerField(
        min_value=0,
        initial=0,
        label="Broken",
        widget=forms.NumberInput(attrs={"min": 0, "style": "width:5em"}),
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project

    def clean(self):
        cleaned = super().clean()

        # Scoped to the project being torn down. line_id comes straight from
        # the POST, and without this filter an unrelated id would still be
        # looked up and its quantity read back in the error message below,
        # which is an answer this form has no business giving.
        lines = ProjectPart.objects.select_related("part", "project")
        if self.project is not None:
            lines = lines.filter(project=self.project)

        try:
            line = lines.get(pk=cleaned.get("line_id"))
        except ProjectPart.DoesNotExist:
            raise forms.ValidationError(
                "That allocation line no longer exists - reload the page."
            ) from None

        cleaned["line"] = line

        # Field-level errors are already reported; don't pile a confusing sum
        # error on top of them.
        if self.errors:
            return cleaned

        total = (
            cleaned["qty_returned"] + cleaned["qty_soldered"] + cleaned["qty_broken"]
        )
        if total != line.remaining:
            raise forms.ValidationError(
                f"Account for exactly {line.remaining} - you entered {total}."
            )

        return cleaned


TeardownFormSet = forms.formset_factory(TeardownLineForm, extra=0)
