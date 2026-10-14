from decimal import Decimal

from django import forms

from accounts.validators import PROOF_ACCEPT_ATTR, clean_proof_image

from .models import Dispute
from .services import allowed_categories
from django.utils.translation import gettext as _

MAX_FILES_PER_POST = 5


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    """Several photos in one input. Each is run through the same checker the ID
    proof uploads use (size, real image, re-encoded - so no EXIF/GPS or hidden
    payload survives)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput(attrs={'accept': PROOF_ACCEPT_ATTR}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        files = [f for f in files if f]
        if len(files) > MAX_FILES_PER_POST:
            raise forms.ValidationError(_('Attach at most %(max_photos)s photos at a time.') % {'max_photos': MAX_FILES_PER_POST})
        if not files and self.required:
            raise forms.ValidationError(self.error_messages['required'], code='required')
        return [clean_proof_image(f, 'photo') for f in files]


class DisputeOpenForm(forms.Form):
    category = forms.ChoiceField(label='What went wrong?')
    description = forms.CharField(
        label='Tell us what happened', min_length=20, max_length=2000,
        widget=forms.Textarea(attrs={'rows': 5, 'placeholder': 'What did you expect, and what did you get? Dates and details help.'}),
    )
    desired_resolution = forms.ChoiceField(label='What outcome do you want?', choices=Dispute.WANTED_CHOICES)
    requested_amount = forms.DecimalField(
        label='Refund amount you are asking for (₹)', required=False, min_value=Decimal('0.01'),
        max_digits=10, decimal_places=2, help_text='Only needed for a partial refund.',
    )
    evidence = MultipleImageField(
        label='Photos (optional, up to 5)', required=False,
        help_text='Photos of the book, the packaging or the shipping label help a lot. JPG, PNG, WebP or HEIC, up to 5 MB each.',
    )

    def __init__(self, *args, order, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order
        labels = dict(Dispute.CATEGORY_CHOICES)
        self.fields['category'].choices = [(k, labels[k]) for k in allowed_categories(order)]
        self.fields['requested_amount'].max_value = order.total_amount
        self.fields['requested_amount'].help_text = f'Only needed for a partial refund. You paid ₹{order.total_amount} in total.'

    def clean(self):
        cleaned = super().clean()
        wanted = cleaned.get('desired_resolution')
        amount = cleaned.get('requested_amount')
        if wanted == 'partial_refund':
            if amount is None:
                self.add_error('requested_amount', _('Enter the amount you are asking for.'))
            elif amount > self.order.total_amount:
                self.add_error('requested_amount', _('That is more than you paid (₹%(total_amount)s).') % {'total_amount': self.order.total_amount})
        elif wanted != 'partial_refund':
            cleaned['requested_amount'] = None
        return cleaned


class MessageForm(forms.Form):
    body = forms.CharField(
        label='Your reply', min_length=2, max_length=2000,
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'Write a reply...'}),
    )
    evidence = MultipleImageField(label='Add photos (optional)', required=False)


class EscalateForm(forms.Form):
    reason = forms.CharField(
        label='Why are you escalating? (optional)', required=False, max_length=500,
        widget=forms.Textarea(attrs={'rows': 2}),
    )


class StaffResolveForm(forms.Form):
    outcome = forms.ChoiceField(choices=Dispute.OUTCOME_CHOICES)
    note = forms.CharField(
        label='Explanation (the buyer and the seller both see this)', max_length=2000,
        widget=forms.Textarea(attrs={'rows': 3}),
    )
    refund_amount = forms.DecimalField(
        label='Refund amount (₹)', required=False, min_value=Decimal('0.01'), max_digits=10, decimal_places=2,
    )
    destination = forms.ChoiceField(label='Refund to', choices=Dispute.REFUND_DEST_CHOICES, initial='wallet')
    deduct_from_seller = forms.BooleanField(
        label='Seller bears a partial refund (deduct it from their payout)', required=False, initial=True,
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('outcome') == 'refund' and not cleaned.get('refund_amount'):
            self.add_error('refund_amount', _('Enter the refund amount.'))
        return cleaned
