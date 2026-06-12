"""
Management command: load_stations

Loads the OPIS fuel-price CSV into the database and geocodes each unique
city/state pair via Nominatim (1 request per second as required by their ToS).

Usage
-----
# First run — load all stations and geocode them
python manage.py load_stations

# Re-run after a CSV update (keeps existing geocoding, only re-geocodes new cities)
python manage.py load_stations --resume

# Load data only, skip geocoding (useful for quick testing)
python manage.py load_stations --skip-geocoding

# Geocode only (stations already in DB)
python manage.py load_stations --geocode-only
"""

import csv
import time
import logging

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from api.models import FuelStation

logger = logging.getLogger(__name__)

US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}


class Command(BaseCommand):
    help = 'Load fuel stations from CSV and geocode by city/state via Nominatim'

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv',
            default=str(settings.CSV_FILE_PATH),
            help='Path to the fuel-prices CSV file',
        )
        parser.add_argument(
            '--skip-geocoding',
            action='store_true',
            help='Insert station records without geocoding (fast, for testing)',
        )
        parser.add_argument(
            '--geocode-only',
            action='store_true',
            help='Only geocode stations already in the DB (skip CSV import)',
        )
        parser.add_argument(
            '--resume',
            action='store_true',
            help='Skip cities that are already geocoded',
        )

    def handle(self, *args, **options):
        csv_path = options['csv']
        skip_geocoding = options['skip_geocoding']
        geocode_only = options['geocode_only']
        resume = options['resume']

        if not geocode_only:
            self._load_csv(csv_path, resume)

        if not skip_geocoding:
            self._geocode_stations(resume)

        total = FuelStation.objects.count()
        geocoded = FuelStation.objects.filter(geocoded=True).count()
        self.stdout.write(
            self.style.SUCCESS(
                f'\nDone. {geocoded}/{total} stations geocoded and ready for routing.'
            )
        )

    # ------------------------------------------------------------------

    def _load_csv(self, csv_path: str, resume: bool) -> None:
        self.stdout.write(f'Loading CSV: {csv_path}')

        try:
            with open(csv_path, encoding='utf-8-sig', newline='') as fh:
                rows = list(csv.DictReader(fh))
        except FileNotFoundError:
            raise CommandError(f'CSV file not found: {csv_path}')

        self.stdout.write(f'  {len(rows)} rows in CSV')

        if not resume:
            FuelStation.objects.all().delete()
            self.stdout.write('  Cleared existing station records')

        # Build a set of existing OPIS IDs to skip in --resume mode
        existing_ids = set(FuelStation.objects.values_list('opis_id', flat=True)) if resume else set()

        stations = []
        skipped_non_us = 0
        for row in rows:
            state = row['State'].strip()
            if state not in US_STATES:
                skipped_non_us += 1
                continue

            opis_id = int(row['OPIS Truckstop ID'])
            if resume and opis_id in existing_ids:
                continue

            stations.append(FuelStation(
                opis_id=opis_id,
                name=row['Truckstop Name'].strip(),
                address=row['Address'].strip(),
                city=row['City'].strip(),
                state=state,
                rack_id=int(row['Rack ID']),
                retail_price=float(row['Retail Price']),
            ))

        if stations:
            FuelStation.objects.bulk_create(stations, ignore_conflicts=True)
            self.stdout.write(f'  Inserted {len(stations)} US station records')

        if skipped_non_us:
            self.stdout.write(f'  Skipped {skipped_non_us} non-US entries')

    def _geocode_stations(self, resume: bool) -> None:
        qs = FuelStation.objects.all()
        if resume:
            qs = qs.filter(geocoded=False)

        pairs = list(
            qs.values_list('city', 'state')
            .distinct()
            .order_by('state', 'city')
        )
        total_pairs = len(pairs)

        if total_pairs == 0:
            self.stdout.write('  All cities already geocoded.')
            return

        self.stdout.write(
            f'\nGeocoding {total_pairs} unique city/state pairs '
            f'(~{total_pairs // 60} min at 1 req/sec) …'
        )

        success = 0
        failed = 0
        last_req = 0.0

        session = requests.Session()
        session.headers['User-Agent'] = settings.NOMINATIM_USER_AGENT

        for idx, (city, state) in enumerate(pairs, 1):
            elapsed = time.monotonic() - last_req
            if elapsed < 1.05:
                time.sleep(1.05 - elapsed)

            try:
                r = session.get(
                    f'{settings.NOMINATIM_BASE_URL}/search',
                    params={'q': f'{city}, {state}, USA', 'format': 'json',
                            'limit': 1, 'countrycodes': 'us'},
                    timeout=10,
                )
                last_req = time.monotonic()
                data = r.json()
                if data:
                    coords = (float(data[0]['lat']), float(data[0]['lon']))
                    FuelStation.objects.filter(city=city, state=state).update(
                        latitude=coords[0], longitude=coords[1], geocoded=True,
                    )
                    success += 1
                else:
                    failed += 1
                    self.stdout.write(self.style.WARNING(
                        f'  [{idx}/{total_pairs}] FAILED (no result): {city}, {state}'
                    ))
            except Exception as exc:
                last_req = time.monotonic()
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f'  [{idx}/{total_pairs}] FAILED ({exc}): {city}, {state}'
                ))

            if idx % 50 == 0:
                remaining = total_pairs - idx
                self.stdout.write(
                    f'  [{idx}/{total_pairs}] {success} ok, {failed} failed — '
                    f'~{remaining} left (~{remaining // 60} min)'
                )

        session.close()
        self.stdout.write(f'\nGeocoding complete: {success} ok, {failed} failed')
