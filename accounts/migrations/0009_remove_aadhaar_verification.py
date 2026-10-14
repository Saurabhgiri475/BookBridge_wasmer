from django.db import migrations


class Migration(migrations.Migration):
    """Seller Aadhaar verification was removed; email verification is the only step."""

    dependencies = [
        ('accounts', '0008_staff_two_factor'),
    ]

    operations = [
        migrations.RemoveField(model_name='user', name=name)
        for name in (
            'aadhaar_number_hash', 'aadhaar_last4', 'aadhaar_verified',
            'aadhaar_otp', 'aadhaar_otp_sent_at', 'aadhaar_otp_attempts',
        )
    ]
