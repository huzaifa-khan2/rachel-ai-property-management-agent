"""
Manual lease-renewal and rent-increase review.

Default behavior: refresh AppFolio and preview only.
Live email requires the explicit --send flag.
"""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TRACKER_PATH = ROOT / "rachel_lease_tracker_v2.1.py"
SCRAPER_PATH = ROOT / "rachel_scraper_v2.py"


def load_tracker_module():
    spec = importlib.util.spec_from_file_location(
        "rachel_lease_tracker_manual",
        TRACKER_PATH,
    )

    if spec is None or spec.loader is None:
        raise RuntimeError(
            "Could not load rachel_lease_tracker_v2.1.py."
        )

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refresh_appfolio():
    print("\n[Manual Lease Review] Refreshing AppFolio reports...")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_PATH),
            "--headless",
        ],
        cwd=str(ROOT),
    )

    if result.returncode != 0:
        raise RuntimeError(
            "AppFolio refresh failed. No email was sent."
        )

    print("[Manual Lease Review] AppFolio refresh completed.")


def print_preview(tracker):
    print("\n" + "=" * 70)
    print("PREVIEW ONLY — NO EMAIL SENT")
    print("=" * 70)

    print(
        f"Lease renewals requiring review: "
        f"{len(tracker.renewal_alerts)}"
    )
    print(
        f"Rent increases requiring review: "
        f"{len(tracker.mtm_alerts)}"
    )

    if tracker.renewal_alerts:
        print("\nLEASE RENEWALS")

        for item in tracker.renewal_alerts:
            rent = item.get("rent")

            rent_text = (
                f"${rent:,.2f}"
                if isinstance(rent, (int, float))
                else "Unknown"
            )

            print(
                f"- {item['property']} | "
                f"{item['tenant']} | "
                f"Ends {item['lease_end']} | "
                f"{item['days_out']} days | "
                f"Rent {rent_text} | "
                f"{item['regime']}"
            )

    if tracker.mtm_alerts:
        print("\nRENT INCREASES")

        for item in tracker.mtm_alerts:
            rent = item.get("rent")

            rent_text = (
                f"${rent:,.2f}"
                if isinstance(rent, (int, float))
                else "Unknown"
            )

            print(
                f"- {item['property']} | "
                f"{item['tenant']} | "
                f"Eligible {item['eligible']} | "
                f"{item['days_out']} days | "
                f"Rent {rent_text} | "
                f"{item['regime']}"
            )

    if not tracker.renewal_alerts and not tracker.mtm_alerts:
        print("\nNo lease-renewal or rent-increase items were found.")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--send",
        action="store_true",
        help=(
            "Send the live consolidated email to Manager "
            "and copy Owner."
        ),
    )

    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Use the existing database without refreshing AppFolio.",
    )

    args = parser.parse_args()

    if not args.skip_refresh:
        refresh_appfolio()

    module = load_tracker_module()
    tracker = module.RachelLeaseTracker()

    # This manual workflow is intentionally limited to the two
    # time-sensitive categories Owner requested.
    tracker._collect_lease_renewals()
    tracker._collect_mtm_increases()

    print_preview(tracker)

    if not args.send:
        return

    if not tracker.renewal_alerts and not tracker.mtm_alerts:
        print(
            "[Manual Lease Review] Nothing to send. "
            "No email was sent."
        )
        return

    print(
        "\n[Manual Lease Review] LIVE SEND authorized with --send."
    )

    tracker._send_consolidated_email()

    print(
        "[Manual Lease Review] Lease-renewal and "
        "rent-increase email completed."
    )


if __name__ == "__main__":
    main()
