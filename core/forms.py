from django import forms
from django.conf import settings
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError

from .models import Part, ProjectPart


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
    qty_allocated = forms.IntegerField(
        min_value=1,
        initial=1,
        label="How many",
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
        qty = cleaned.get("qty_allocated")

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
                qty_allocated=qty,
                note=cleaned.get("note", ""),
            )
        else:
            # Already on this project - top it up rather than rejecting the
            # submission on the unique constraint.
            line.qty_allocated += qty
            if cleaned.get("note"):
                line.note = cleaned["note"]

        try:
            # Runs the availability guard on the resulting total.
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

    def clean(self):
        cleaned = super().clean()

        try:
            line = ProjectPart.objects.select_related("part", "project").get(
                pk=cleaned.get("line_id")
            )
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
