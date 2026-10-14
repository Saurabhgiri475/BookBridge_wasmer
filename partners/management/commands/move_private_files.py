"""
One-off: move sensitive files that were uploaded before private storage existed
(partner ID proofs, shop proofs, paid digital files, chat photos, buyback photos) from the public MEDIA_ROOT
into PRIVATE_MEDIA_ROOT. Database values don't change - the relative paths stay
the same, only the root folder differs.

    python manage.py move_private_files --dry-run
    python manage.py move_private_files

Afterwards, delete any leftover copies of these folders from backups, old zips
and the web server's media directory: treat them as already exposed.
"""
import os
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from books.models import Book
from chatapp.models import Message
from partners.models import BuybackRequestImage, ShopkeeperPartner


class Command(BaseCommand):
    help = 'Move existing ID proofs, shop proofs, digital files, chat photos and buyback photos from public media to private storage.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show what would move without changing anything.')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        public_root, private_root = Path(settings.MEDIA_ROOT), Path(settings.PRIVATE_MEDIA_ROOT)
        names = []
        for p in ShopkeeperPartner.objects.all():
            names += [n for n in (p.owner_id_proof.name, p.shop_proof.name) if n]
        names += [n for n in Book.objects.exclude(digital_file='').values_list('digital_file', flat=True) if n]
        names += [n for n in Message.objects.exclude(image='').exclude(image__isnull=True).values_list('image', flat=True) if n]
        names += [n for n in BuybackRequestImage.objects.exclude(image='').values_list('image', flat=True) if n]

        moved = missing = already = 0
        for name in names:
            src, dst = public_root / name, private_root / name
            if dst.exists():
                already += 1
                if src.exists() and not dry:
                    src.unlink()  # the private copy is authoritative; drop the public duplicate
                continue
            if not src.exists():
                missing += 1
                self.stdout.write(self.style.WARNING(f'missing: {name}'))
                continue
            self.stdout.write(f'{"would move" if dry else "move"}: {name}')
            if not dry:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                try:
                    os.chmod(dst, 0o600)
                except OSError:
                    pass
            moved += 1
        verb = 'would move' if dry else 'moved'
        self.stdout.write(self.style.SUCCESS(f'{verb} {moved}, already private {already}, missing {missing}.'))
