from django import forms

from .upi import is_valid_upi_id, normalize_upi_id
from django.utils.translation import gettext as _


class UpiForm(forms.Form):
    upi_id = forms.CharField(
        label='UPI ID', max_length=100,
        widget=forms.TextInput(attrs={'placeholder': 'yourname@bank', 'autocomplete': 'off', 'autocapitalize': 'off'}),
    )

    # Re-authentication: whoever is sitting at a hijacked session should not be able to redirect
    # a seller's money without knowing the account password.
    current_password = forms.CharField(
        label='Your password', required=False, strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password', 'placeholder': 'Your password'}),
    )

    def clean_upi_id(self):
        value = normalize_upi_id(self.cleaned_data['upi_id'])
        if not is_valid_upi_id(value):
            raise forms.ValidationError(_('Enter a valid UPI ID, like name@bank.'))
        return value
