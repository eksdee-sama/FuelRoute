"""
Supervisor QA agent for route quality.

Usage
-----
  python manage.py qa_route --start "New York, NY" --end "Denver, CO"
  python manage.py qa_route --start "40.7128,-74.0060" --end "39.7392,-104.9903"
  python manage.py qa_route --start "Chicago, IL" --end "Los Angeles, CA" --tank-range 600 --mpg 8

Exit codes
----------
  0 -- all checks PASS or INFO
  1 -- at least one FAIL
"""

import asyncio
import sys

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from api.services import route_qa
from api.views import _compute_route


_LEVEL_COLOR = {
    "PASS": "\033[92m",
    "WARN": "\033[93m",
    "FAIL": "\033[91m",
    "INFO": "\033[94m",
}
_RESET = "\033[0m"
_SEP_CHAR = "-"


class Command(BaseCommand):
    help = "Run QA checks on a computed route and print a quality report."

    def add_arguments(self, parser):
        parser.add_argument("--start", required=True, help="Start location or lat,lng")
        parser.add_argument("--end", required=True, help="End location or lat,lng")
        parser.add_argument(
            "--tank-range", type=float,
            default=float(settings.VEHICLE_TANK_RANGE_MILES),
            help="Tank range in miles (default: %(default)s)",
        )
        parser.add_argument(
            "--mpg", type=float,
            default=float(settings.VEHICLE_MPG),
            help="Fuel efficiency in MPG (default: %(default)s)",
        )
        parser.add_argument(
            "--plain", action="store_true",
            help="Disable ANSI colour output",
        )

    def handle(self, *args, **options):
        start = options["start"]
        end = options["end"]
        tank_range = options["tank_range"]
        mpg = options["mpg"]
        use_color = not options["plain"]

        self.stdout.write(f"\nComputing route: {start} -> {end} ...")
        result = asyncio.run(
            _compute_route(start, end, tank_range, mpg)
        )

        if "error" in result:
            raise CommandError(f"Route computation failed: {result['error']}")

        route = result["route"]
        summary = result["summary"]
        is_direct = result.get("_is_direct", False)
        n_stops = summary["total_fuel_stops"]
        dist = route["total_distance_miles"]

        direct_tag = " -- DIRECT TRIP" if is_direct else ""
        header = (
            f"\nRoute QA: {route['start_display']} -> {route['end_display']} "
            f"({dist:.0f} mi, {n_stops} stop{'s' if n_stops != 1 else ''}{direct_tag})"
        )
        sep = _SEP_CHAR * 70

        self.stdout.write(header)
        self.stdout.write(sep)

        findings = route_qa.run_all_checks(result)
        has_fail = False

        for f in findings:
            level = f["level"]
            if level == "FAIL":
                has_fail = True
            color = _LEVEL_COLOR.get(level, "") if use_color else ""
            reset = _RESET if use_color else ""
            self.stdout.write(
                f"{color}{level:<5}{reset}  {f['check']:<22}  {f['message']}"
            )

        self.stdout.write(sep)
        agg = route_qa.qa_summary(findings)
        overall_color = _LEVEL_COLOR.get(agg["overall"], "") if use_color else ""
        reset = _RESET if use_color else ""
        self.stdout.write(
            f"Overall: {overall_color}{agg['overall']}{reset} "
            f"({agg['fail_count']} failure(s), {agg['warn_count']} warning(s))\n"
        )

        if result["fuel_stops"] and not is_direct:
            self.stdout.write("Fuel stops:")
            self.stdout.write(
                f"  {'#':<4} {'City, State':<28} {'Mile':>6} {'Gal':>6} "
                f"{'$/gal':>7} {'Cost':>8} {'Off-route':>10}"
            )
            self.stdout.write("  " + _SEP_CHAR * 70)
            for s in result["fuel_stops"]:
                loc = f"{s['city']}, {s['state']}"
                self.stdout.write(
                    f"  {s['stop_number']:<4} {loc:<28} {s['distance_from_start_miles']:>6.0f} "
                    f"{s['gallons_to_add']:>6.1f} {s['price_per_gallon']:>7.3f} "
                    f"{s['cost_usd']:>8.2f} {s['distance_from_route_miles']:>9.2f} mi"
                )
            self.stdout.write(
                f"\n  Total: ${summary['total_fuel_cost_usd']:.2f} "
                f"for {summary['total_gallons_purchased']:.1f} gal "
                f"(avg ${summary['average_price_per_gallon']:.3f}/gal)\n"
            )

        if has_fail:
            sys.exit(1)
