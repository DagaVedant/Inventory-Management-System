from django import forms

from .models import ProjectPart


class TeardownLineForm(forms.Form):
    """One row of the teardown screen: what became of this part."""

    line_id = forms.IntegerField(widget=forms.HiddenInput)
    qty_returned = forms.IntegerField(
        min_value=0, initial=0, label="Returned",
        widget=forms.NumberInput(attrs={"size": 4, "style": "width:5em"}),
    )
    qty_soldered = forms.IntegerField(
        min_value=0, initial=0, label="Soldered in",
        widget=forms.NumberInput(attrs={"size": 4, "style": "width:5em"}),
    )
    qty_broken = forms.IntegerField(
        min_value=0, initial=0, label="Broken",
        widget=forms.NumberInput(attrs={"size": 4, "style": "width:5em"}),
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
            )

        # Field-level errors already reported; don't pile a confusing sum error on top.
        if self.errors:
            cleaned["line"] = line
            return cleaned

        total = (
            cleaned["qty_returned"] + cleaned["qty_soldered"] + cleaned["qty_broken"]
        )
        if total != line.remaining:
            raise forms.ValidationError(
                f"Account for exactly {line.remaining} - you entered {total}."
            )

        cleaned["line"] = line
        return cleaned


TeardownFormSet = forms.formset_factory(TeardownLineForm, extra=0)
