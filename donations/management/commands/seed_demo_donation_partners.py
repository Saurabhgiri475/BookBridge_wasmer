from django.core.management.base import BaseCommand

from donations.models import DonationPartner


class Command(BaseCommand):
    help = 'Adds two clearly-labelled DEMO donation organisations for local testing. Do not use in production.'

    def handle(self, *args, **options):
        demos = [
            dict(name='DEMO Community Library', org_type='library', city='Kanpur', address='1 Demo Road',
                 description='Demo entry for local testing only.', accepts_pickup=True),
            dict(name='DEMO Village School', org_type='school', city='Lucknow', address='2 Demo Lane',
                 description='Demo entry for local testing only.', accepts_pickup=False),
        ]
        for d in demos:
            _, created = DonationPartner.objects.get_or_create(name=d['name'], defaults=d)
            self.stdout.write(f'{"Created" if created else "Already exists"}: {d["name"]}')
