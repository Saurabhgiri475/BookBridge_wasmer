from datetime import timedelta

from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.validators import PROOF_ACCEPT_ATTR, PROOF_HELP_TEXT, clean_proof_image, validate_gstin, validate_pan
from .models import PICKUP_WINDOW_DAYS, BuybackRequest, BuybackRequestImage, ShopkeeperPartner


class PartnerApplicationForm(forms.ModelForm):
    """Used for the signup-time application, the standalone apply page, re-applying
    after a rejection, and editing a still-pending application.

    `editing=True` (a pending application being changed) makes the ID number and
    the two photos optional: leaving them blank keeps what was submitted."""

    id_type = forms.ChoiceField(
        label=_('ID number type'), choices=ShopkeeperPartner.ID_TYPE_CHOICES, initial='aadhaar', required=False,
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'}),
    )
    owner_aadhaar_number = forms.CharField(
        label=_('Owner Aadhaar Number'), required=False,
        max_length=14,
        widget=forms.TextInput(attrs={'placeholder': _('12-digit Aadhaar number'), 'inputmode': 'numeric', 'autocomplete': 'off'}),
        help_text=_('Used only to verify you\u2019re a real person - never stored in full.'),
    )
    owner_pan_number = forms.CharField(
        label=_('Owner PAN'), required=False, max_length=10,
        widget=forms.TextInput(attrs={'placeholder': _('10-character PAN'), 'autocomplete': 'off', 'style': 'text-transform:uppercase'}),
        help_text=_('Never stored in full - only the last 4 characters are kept for display.'),
    )

    class Meta:
        model = ShopkeeperPartner
        fields = [
            'shop_name', 'shop_address', 'shop_city', 'shop_pincode', 'contact_phone', 'gstin',
            'owner_id_proof', 'shop_proof', 'shop_latitude', 'shop_longitude',
        ]
        widgets = {
            'shop_address': forms.Textarea(attrs={'rows': 2}),
            'gstin': forms.TextInput(attrs={
                'placeholder': _('15-character GSTIN'), 'maxlength': '15', 'autocomplete': 'off',
                'style': 'text-transform:uppercase', 'data-gstin': '1',
            }),
            'owner_id_proof': forms.FileInput(attrs={
                'accept': PROOF_ACCEPT_ATTR, 'data-preview': 'previewIdProof', 'data-label': _('ID proof'),
            }),
            'shop_proof': forms.FileInput(attrs={
                'accept': PROOF_ACCEPT_ATTR, 'data-preview': 'previewShopProof', 'data-label': _('shop proof'),
            }),
            'shop_latitude': forms.HiddenInput(attrs={'id': 'id_shop_latitude'}),
            'shop_longitude': forms.HiddenInput(attrs={'id': 'id_shop_longitude'}),
        }

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.editing = editing
        self.fields['owner_id_proof'].help_text = PROOF_HELP_TEXT
        self.fields['shop_proof'].help_text = PROOF_HELP_TEXT
        if self.instance and self.instance.pk:
            self.fields['id_type'].initial = self.instance.id_type

    # -- field cleaners --------------------------------------------------------

    def clean_gstin(self):
        value = self.cleaned_data.get('gstin', '')
        if not value.strip():
            return ''
        gstin = validate_gstin(value)
        clash = ShopkeeperPartner.objects.filter(gstin=gstin).exclude(status='rejected')
        if self.instance and self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(_('Another partner application already uses this GSTIN. Contact support if this is your shop.'))
        return gstin

    def clean_owner_id_proof(self):
        return clean_proof_image(self.cleaned_data.get('owner_id_proof'), 'ID proof')

    def clean_shop_proof(self):
        return clean_proof_image(self.cleaned_data.get('shop_proof'), 'shop proof')

    def clean(self):
        cleaned = super().clean()
        id_type = cleaned.get('id_type') or 'aadhaar'
        cleaned['id_type'] = id_type
        have_saved = bool(self.instance and self.instance.pk) and (
            self.instance.owner_pan_hash if id_type == 'pan' else self.instance.owner_aadhaar_hash
        ) and self.instance.id_type == id_type

        if id_type == 'aadhaar':
            raw = (cleaned.get('owner_aadhaar_number') or '').replace(' ', '')
            if not raw:
                if not (self.editing and have_saved):
                    self.add_error('owner_aadhaar_number', _('Enter the owner\u2019s 12-digit Aadhaar number.'))
            elif not raw.isdigit() or len(raw) != 12:
                self.add_error('owner_aadhaar_number', _('Enter a valid 12-digit Aadhaar number.'))
            elif raw[0] in ('0', '1'):
                self.add_error('owner_aadhaar_number', _('That doesn\u2019t look like a valid Aadhaar number.'))
            else:
                cleaned['owner_aadhaar_number'] = raw
        else:
            raw = (cleaned.get('owner_pan_number') or '').strip()
            if not raw:
                if not (self.editing and have_saved):
                    self.add_error('owner_pan_number', _('Enter the owner\u2019s 10-character PAN.'))
            else:
                try:
                    cleaned['owner_pan_number'] = validate_pan(raw)
                except forms.ValidationError as exc:
                    self.add_error('owner_pan_number', exc)
        return cleaned

    def save(self, commit=True):
        partner = super().save(commit=False)
        id_type = self.cleaned_data['id_type']
        partner.id_type = id_type
        if id_type == 'aadhaar' and self.cleaned_data.get('owner_aadhaar_number'):
            partner.set_owner_aadhaar_number(self.cleaned_data['owner_aadhaar_number'])
            partner.owner_pan_hash, partner.owner_pan_last4 = '', ''
        elif id_type == 'pan' and self.cleaned_data.get('owner_pan_number'):
            partner.set_owner_pan_number(self.cleaned_data['owner_pan_number'])
            partner.owner_aadhaar_hash, partner.owner_aadhaar_last4 = '', ''
        if commit:
            partner.save()
        return partner


class BuybackRequestForm(forms.ModelForm):
    class Meta:
        model = BuybackRequest
        fields = [
            'category', 'title', 'author', 'publisher', 'isbn', 'edition', 'publication_year',
            'subject', 'board', 'university', 'branch', 'semester', 'language',
            'condition', 'original_price', 'expected_price', 'quantity', 'description',
            'pickup_address', 'pickup_city', 'pickup_pincode',
            'pickup_latitude', 'pickup_longitude',
        ]
        labels = {'expected_price': 'What are you hoping to get? (optional)'}
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'pickup_address': forms.TextInput(),
            'pickup_latitude': forms.HiddenInput(attrs={'id': 'id_pickup_latitude'}),
            'pickup_longitude': forms.HiddenInput(attrs={'id': 'id_pickup_longitude'}),
        }


BuybackRequestImageFormSet = inlineformset_factory(
    BuybackRequest, BuybackRequestImage, fields=('image', 'angle'), extra=4, max_num=6, can_delete=True,
)


class PartnerOfferForm(forms.Form):
    offered_price = forms.DecimalField(
        label='Your offer (\u20b9)', max_digits=8, decimal_places=2, min_value=1,
    )
    offer_note = forms.CharField(
        label='Note to seller (optional)', max_length=255, required=False,
        widget=forms.TextInput(attrs={'placeholder': 'e.g. Can collect this evening'}),
    )


class PickupScheduleForm(forms.Form):
    pickup_date = forms.DateField(
        label='Pickup date', widget=forms.DateInput(attrs={'type': 'date'}),
    )
    pickup_slot = forms.ChoiceField(label='Time slot', choices=BuybackRequest.PICKUP_SLOT_CHOICES)

    def clean_pickup_date(self):
        day = self.cleaned_data['pickup_date']
        today = timezone.localdate()
        if day < today:
            raise forms.ValidationError(_('Pickup date cannot be in the past.'))
        if day > today + timedelta(days=PICKUP_WINDOW_DAYS):
            raise forms.ValidationError(_('Pick a date within the next %(days)s days.') % {'days': PICKUP_WINDOW_DAYS})
        return day


class PartnerCompleteForm(forms.Form):
    pickup_otp = forms.CharField(
        label='Seller\u2019s pickup OTP', max_length=6, required=False,
        widget=forms.TextInput(attrs={'inputmode': 'numeric', 'autocomplete': 'one-time-code', 'placeholder': '6-digit OTP'}),
        help_text='Ask the seller for the code shown on their request page / sent to their phone.',
    )
    final_price = forms.DecimalField(
        label='Amount actually paid to the seller (\u20b9)', max_digits=8, decimal_places=2, min_value=1,
        help_text='This is what your commission is calculated from.',
    )
    list_for_resale = forms.BooleanField(
        label='List this book for resale on BookBridge', required=False, initial=True,
    )
    resale_price = forms.DecimalField(
        label='Resale price (\u20b9)', max_digits=8, decimal_places=2, min_value=1, required=False,
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('list_for_resale') and not cleaned.get('resale_price'):
            self.add_error('resale_price', _('Enter a resale price, or uncheck "List this book for resale".'))
        return cleaned


class PartnerRejectForm(forms.Form):
    reason = forms.CharField(
        label='Reason (shown to the applicant)', max_length=255, required=False,
        widget=forms.TextInput(attrs={'placeholder': 'e.g. ID proof image was unreadable'}),
    )


class PartnerSettingsForm(forms.ModelForm):
    """Shop Settings page - business preferences plus the shop details a
    partner may need to update after approval (address, phone, pin)."""
    class Meta:
        model = ShopkeeperPartner
        fields = [
            'is_accepting_requests', 'default_radius_km', 'target_margin_percent',
            'contact_phone', 'shop_address', 'shop_city', 'shop_pincode',
            'shop_latitude', 'shop_longitude',
        ]
        labels = {
            'is_accepting_requests': 'Open for new buyback leads',
            'default_radius_km': 'Default lead radius',
            'target_margin_percent': 'Target profit margin (%)',
        }
        widgets = {
            'default_radius_km': forms.Select(choices=[(10, '10 km'), (25, '25 km'), (50, '50 km'), (100, '100 km')]),
            'shop_latitude': forms.HiddenInput(attrs={'id': 'id_shop_latitude'}),
            'shop_longitude': forms.HiddenInput(attrs={'id': 'id_shop_longitude'}),
        }


class PartnerReviewForm(forms.Form):
    rating = forms.ChoiceField(
        label='How was your experience?', choices=[(str(i), f'{i} star{"s" if i > 1 else ""}') for i in range(5, 0, -1)],
        widget=forms.RadioSelect,
    )
    comment = forms.CharField(
        label='Comment (optional)', max_length=500, required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Was the partner on time and fair on price?'}),
    )
