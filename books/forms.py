from django import forms
from django.core.validators import FileExtensionValidator
from django.forms import inlineformset_factory
from .models import Book, BookImage, BookRequest, ListingReport


class BookForm(forms.ModelForm):
    class Meta:
        model = Book
        fields = [
            'listing_type', 'digital_type', 'digital_file',
            'category', 'title', 'author', 'publisher', 'isbn', 'edition', 'publication_year',
            'subject', 'board', 'university', 'branch', 'semester', 'language',
            'condition', 'original_price', 'price', 'quantity', 'description', 'is_rentable',
            'pickup_address', 'pickup_city', 'pickup_pincode', 'pickup_latitude', 'pickup_longitude',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'listing_type': forms.RadioSelect(),
            'is_rentable': forms.CheckboxInput(),
            'pickup_latitude': forms.HiddenInput(attrs={'id': 'id_pickup_latitude'}),
            'pickup_longitude': forms.HiddenInput(attrs={'id': 'id_pickup_longitude'}),
        }

    def clean(self):
        cleaned = super().clean()
        listing_type = cleaned.get('listing_type')

        if listing_type == 'digital':
            if not cleaned.get('digital_type'):
                self.add_error('digital_type', 'Choose what kind of digital material this is.')
            # Only require a fresh upload if there's no existing file already
            # attached (so editing a digital listing without re-uploading
            # doesn't wipe the file).
            has_existing_file = bool(getattr(self.instance, 'digital_file', None))
            if not cleaned.get('digital_file') and not has_existing_file:
                self.add_error('digital_file', 'Upload the PDF/scanned file buyers will receive.')
        else:
            # Physical listings don't use these - clear them so a listing
            # can't end up half-digital after switching types.
            cleaned['digital_type'] = ''
            cleaned['digital_file'] = None

        if listing_type == 'digital':
            # Rentals only make sense for a physical copy that has to be
            # shipped back and forth - a digital file has nothing to return.
            cleaned['is_rentable'] = False

        return cleaned


BookImageFormSet = inlineformset_factory(
    Book, BookImage, fields=('image', 'angle'), extra=4, max_num=6, can_delete=True
)


class BookRequestForm(forms.ModelForm):
    class Meta:
        model = BookRequest
        fields = ['title', 'author', 'isbn', 'edition', 'description', 'max_price', 'location']


class BookSearchForm(forms.Form):
    q = forms.CharField(required=False, label='')
    listing_type = forms.CharField(required=False)
    digital_type = forms.CharField(required=False)
    category = forms.CharField(required=False)
    board = forms.CharField(required=False)
    university = forms.CharField(required=False)
    semester = forms.CharField(required=False)
    condition = forms.CharField(required=False)
    min_price = forms.DecimalField(required=False)
    max_price = forms.DecimalField(required=False)
    city = forms.CharField(required=False)


class BulkUploadForm(forms.Form):
    file = forms.FileField(
        label='CSV or Excel file',
        validators=[FileExtensionValidator(allowed_extensions=['csv', 'xlsx', 'xls'])],
        help_text='Physical books only - one row per copy. Download the template below for the exact columns.',
    )


class ListingReportForm(forms.ModelForm):
    class Meta:
        model = ListingReport
        fields = ['reason', 'details']
        widgets = {
            'reason': forms.RadioSelect(),
            'details': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Anything else staff should know (optional)'}),
        }
