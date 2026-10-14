from django import forms
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, UserCreationForm
from .models import User


class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True, label=_('Email'))
    role = forms.ChoiceField(choices=User.ROLE_CHOICES, initial='both', label=_('Role'))
    phone = forms.CharField(max_length=15, required=False, label=_('Phone'))
    city = forms.CharField(max_length=100, required=False, label=_('City'))
    apply_as_partner = forms.BooleanField(
        required=False,
        label=_("I'm a shopkeeper - I also want to become a BookBridge Partner"),
    )
    referral_code = forms.CharField(
        max_length=12, required=False, label=_('Referral code (optional)'),
        widget=forms.TextInput(attrs={'placeholder': _("A friend's referral code, if you have one")}),
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'role', 'phone', 'city', 'password1', 'password2')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Browser-side helpers (static/js/auth-ui.js) hook onto these attributes.
        self.fields['username'].widget.attrs.update({
            'data-availability': 'username', 'autocomplete': 'username', 'autocapitalize': 'none', 'spellcheck': 'false',
        })
        self.fields['email'].widget.attrs.update({'data-availability': 'email', 'autocomplete': 'email'})
        self.fields['password1'].widget.attrs.update({'data-password-toggle': '1', 'data-strength': '1', 'autocomplete': 'new-password'})
        self.fields['password2'].widget.attrs.update({'data-password-toggle': '1', 'autocomplete': 'new-password'})

    def clean_email(self):
        email = self.cleaned_data['email'].strip()
        # One account per email. Unverified signups are ignored here so someone
        # can't lock a real person out of their own address by signing up with it.
        if User.objects.filter(email__iexact=email, signup_pending=False).exists():
            raise forms.ValidationError(_('An account with this email already exists. Try logging in instead.'))
        return email

    def clean_username(self):
        username = self.cleaned_data['username']
        if '@' in username:
            raise forms.ValidationError(_("Username can't be an email address - choose a display name instead."))
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(_('That username is taken - try another.'))
        return username

    def clean_referral_code(self):
        code = self.cleaned_data.get('referral_code', '').strip().upper()
        if code and not User.objects.filter(referral_code=code).exists():
            raise forms.ValidationError(_("We couldn't find that referral code - check it and try again, or leave it blank."))
        return code


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'phone', 'address', 'city', 'pincode', 'avatar')


class OTPForm(forms.Form):
    otp = forms.CharField(
        label='OTP',
        max_length=6,
        min_length=4,
        widget=forms.TextInput(attrs={'placeholder': 'Enter OTP', 'inputmode': 'numeric', 'autocomplete': 'one-time-code'}),
    )


def mark_invalid_fields(*forms):
    """Add Bootstrap's `is-invalid` class to every field that failed validation,
    so the template can show a red border on exactly the fields the user got wrong."""
    for form in forms:
        if not form.is_bound:
            continue
        for name in form.errors:
            if name in form.fields:
                widget = form.fields[name].widget
                widget.attrs['class'] = (widget.attrs.get('class', '') + ' is-invalid').strip()


class BookBridgeSetPasswordForm(SetPasswordForm):
    """The "choose a new password" form, with the same show/hide + strength helpers as signup."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['new_password1'].widget.attrs.update(
            {'data-password-toggle': '1', 'data-strength': '1', 'autocomplete': 'new-password'})
        self.fields['new_password2'].widget.attrs.update({'data-password-toggle': '1', 'autocomplete': 'new-password'})


class BookBridgeAuthenticationForm(AuthenticationForm):
    """Login form that recognises an account still waiting for its signup email
    code. Django reports those as a generic "incorrect username or password"
    (inactive users never authenticate), which would leave a genuine new user
    stuck - so when the credentials are right for such an account we flag it and
    let the view send them back to the code-entry page instead."""
    unverified_user = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['password'].widget.attrs.update({'data-password-toggle': '1'})

    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')
        if username and password:
            candidate = User.objects.filter(**{User.USERNAME_FIELD: username}).first()
            if (candidate and candidate.signup_pending and not candidate.is_active
                    and candidate.check_password(password)):
                self.unverified_user = candidate
                raise forms.ValidationError(
                    'Please verify your email to activate your account.', code='unverified',
                )
        return super().clean()
