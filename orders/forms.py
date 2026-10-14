from django import forms
from .models import Order, Offer, Review, ReturnRequest


class CheckoutForm(forms.ModelForm):
    # Not a model field - handled separately in views.checkout() via
    # coupons.services, since applying it touches another app's models
    # (Coupon/CouponRedemption) rather than just this Order row.
    coupon_code = forms.CharField(
        max_length=24, required=False, label='Coupon code (optional)',
        widget=forms.TextInput(attrs={'placeholder': 'e.g. WELCOME50', 'style': 'text-transform:uppercase;'}),
    )

    class Meta:
        model = Order
        fields = ['delivery_address', 'delivery_city', 'delivery_pincode', 'payment_method']

    def __init__(self, *args, is_digital=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_digital = is_digital
        if is_digital:
            # No shipping for digital items - the file is emailed +
            # available on the order page instead, and Cash on Delivery
            # doesn't make sense with nothing to hand over at the door.
            for name in ('delivery_address', 'delivery_city', 'delivery_pincode'):
                self.fields[name].required = False
            self.fields['payment_method'].choices = [
                c for c in self.fields['payment_method'].choices if c[0] != 'cod'
            ]
        else:
            for name in ('delivery_address', 'delivery_city', 'delivery_pincode'):
                self.fields[name].required = True

    def clean(self):
        cleaned = super().clean()
        if not self.is_digital:
            for name in ('delivery_address', 'delivery_city', 'delivery_pincode'):
                if not cleaned.get(name):
                    self.add_error(name, 'This field is required.')
        return cleaned


class OfferForm(forms.ModelForm):
    class Meta:
        model = Offer
        fields = ['amount']


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ['seller_rating', 'book_quality_rating', 'delivery_rating', 'comment']
        widgets = {
            'seller_rating': forms.NumberInput(attrs={'min': 1, 'max': 5}),
            'book_quality_rating': forms.NumberInput(attrs={'min': 1, 'max': 5}),
            'delivery_rating': forms.NumberInput(attrs={'min': 1, 'max': 5}),
        }


class ReturnRequestForm(forms.ModelForm):
    class Meta:
        model = ReturnRequest
        fields = ['reason', 'details']
