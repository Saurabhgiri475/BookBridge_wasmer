import re

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from .certificate import name_is_printable
from django.utils.translation import gettext as _
from .models import (
    Donation, DonationItem, DonationPartner, MAX_BOOKS_PER_DONATION, MAX_ITEMS_PER_DONATION,
)

_PHONE_RE = re.compile(r'^\+?\d{10,14}$')
_PIN_RE = re.compile(r'^\d{6}$')


class DonationForm(forms.ModelForm):
    class Meta:
        model = Donation
        fields = ['partner', 'certificate_name', 'handover', 'pickup_address', 'pickup_city',
                  'pickup_pincode', 'contact_phone', 'note']
        widgets = {
            'handover': forms.RadioSelect,
            'note': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Anything the organisation should know (optional)'}),
            'pickup_address': forms.TextInput(attrs={'placeholder': 'House / street / area'}),
            'pickup_pincode': forms.TextInput(attrs={'inputmode': 'numeric', 'maxlength': 6}),
        }
        labels = {'partner': 'Donate to', 'certificate_name': 'Name on certificate'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['partner'].queryset = DonationPartner.objects.filter(is_active=True)
        self.fields['partner'].empty_label = 'Choose an organisation...'
        for name in ('partner', 'certificate_name', 'pickup_address', 'pickup_city', 'pickup_pincode',
                     'contact_phone', 'note'):
            self.fields[name].widget.attrs.setdefault('class', 'form-control')
        self.fields['partner'].widget.attrs['class'] = 'form-select'
        self.fields['handover'].widget.attrs['class'] = 'form-check-input'

    def clean_certificate_name(self):
        name = ' '.join(self.cleaned_data['certificate_name'].split())
        if len(name) < 2:
            raise forms.ValidationError(_('Please enter the name to print on the certificate.'))
        if not name_is_printable(name):
            raise forms.ValidationError(
                _('Please use English (Latin) letters - the PDF certificate cannot print this script yet.'))
        return name

    def clean_contact_phone(self):
        phone = self.cleaned_data.get('contact_phone', '').replace(' ', '').replace('-', '')
        if phone and not _PHONE_RE.match(phone):
            raise forms.ValidationError(_('Enter a valid phone number (10-14 digits).'))
        return phone

    def clean_pickup_pincode(self):
        pin = self.cleaned_data.get('pickup_pincode', '').strip()
        if pin and not _PIN_RE.match(pin):
            raise forms.ValidationError(_('Enter a 6-digit PIN code.'))
        return pin

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('handover') == 'pickup':
            partner = cleaned.get('partner')
            if partner and not partner.accepts_pickup:
                self.add_error('handover', _('%(name)s does not accept pickups - please choose drop-off.') % {'name': partner.name})
            for field, label in (('pickup_address', 'a pickup address'), ('pickup_city', 'your city'),
                                 ('pickup_pincode', 'your PIN code'), ('contact_phone', 'a contact phone number')):
                if not cleaned.get(field):
                    self.add_error(field, _('Please enter %(label)s so the pickup can be arranged.') % {'label': label})
        else:  # drop-off: don't keep an address we don't need
            for field in ('pickup_address', 'pickup_city', 'pickup_pincode'):
                cleaned[field] = ''
        return cleaned


class DonationItemForm(forms.ModelForm):
    class Meta:
        model = DonationItem
        fields = ['title', 'author', 'isbn', 'condition', 'quantity']
        widgets = {'quantity': forms.NumberInput(attrs={'min': 1, 'max': MAX_BOOKS_PER_DONATION})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs['class'] = 'form-select form-select-sm' if name == 'condition' else 'form-control form-control-sm'
        self.fields['quantity'].widget.attrs['min'] = 1
        self.fields['title'].widget.attrs['placeholder'] = 'Book title'
        self.fields['author'].widget.attrs['placeholder'] = 'Author (optional)'
        self.fields['isbn'].widget.attrs['placeholder'] = 'ISBN (optional)'

    def clean_isbn(self):
        return re.sub(r'[\s-]', '', self.cleaned_data.get('isbn', ''))


class BaseDonationItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [f.cleaned_data for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        if not rows:
            raise forms.ValidationError(_('Add at least one book.'))
        total = sum(r['quantity'] for r in rows)
        if total > MAX_BOOKS_PER_DONATION:
            raise forms.ValidationError(
                _('A single donation can include up to %(max_books)s books. Please split it into two donations.') % {'max_books': MAX_BOOKS_PER_DONATION})


DonationItemFormSet = inlineformset_factory(
    Donation, DonationItem, form=DonationItemForm, formset=BaseDonationItemFormSet,
    extra=5, max_num=MAX_ITEMS_PER_DONATION, validate_max=True, can_delete=False,
)


class ReceiveForm(forms.Form):
    received_count = forms.IntegerField(
        min_value=1, max_value=MAX_BOOKS_PER_DONATION * 2, label='Books actually received',
        widget=forms.NumberInput(attrs={'class': 'form-control'}),
    )


class ReasonForm(forms.Form):
    reason = forms.CharField(
        max_length=300, label='Reason', widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Reason (shown to the donor)'}),
    )

    def clean_reason(self):
        reason = self.cleaned_data['reason'].strip()
        if not reason:
            raise forms.ValidationError(_('Please give a reason.'))
        return reason
