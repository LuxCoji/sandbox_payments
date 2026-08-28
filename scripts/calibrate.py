"""CLI script to calibrate population parameters from PaySim CSV dataset.

Usage:
    python scripts/calibrate.py [--data-dir data/paysim] [--output data/paysim/calibrated_params.json]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sim.population.calibration import calibrate_from_csv, save_calibrated_params  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate FinSim population parameters from PaySim CSVs.")
    parser.add_argument(
        "--data-dir",
        type=str,
        default="data/paysim",
        help="Path to directory containing PaySim CSV files",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="data/paysim/calibrated_params.json",
        help="Output path for calibrated_params.json",
    )
    args = parser.parse_args()

    data_dir = PROJECT_ROOT / args.data_dir
    output_path = PROJECT_ROOT / args.output

    print(f"[*] Calibrating parameters from: {data_dir}")
    params = calibrate_from_csv(data_dir)

    print(f"[*] Saving calibrated parameters to: {output_path}")
    save_calibrated_params(params, output_path)

    print("\n[+] Calibration Complete!")
    print("=" * 60)
    print(f"Transaction Types with Profiles: {len(params.profiles_by_type)}")
    for tx_type, profiles in params.profiles_by_type.items():
        print(f"  - {tx_type.value:<12}: {len(profiles)} profile(s)")
        for p in profiles:
            print(f"      * Count: [{p.min_count}..{p.max_count}], Avg: INR {p.avg_amount_paise/100:.2f}, Std: INR {p.std_amount_paise/100:.2f}, Freq: {p.frequency:.2f}")

    print(f"\nInitial Balance Distribution Intervals: {len(params.initial_balance_distribution)}")
    for min_b, max_b, prob in params.initial_balance_distribution:
        print(f"  - INR {min_b/100:>9.2f} to INR {max_b/100:>10.2f} : {prob*100:>5.1f}% probability")

    print("\nMax Occurrences Caps per Client:")
    for tx_type, cap in params.max_occurrences_per_client.items():
        print(f"  - {tx_type.value:<12}: {cap} max/day")

    print(f"\nTemporal Rate Matrices (24x7): {len(params.temporal_rate_matrix)} types configured")
    print(f"Merchant Category Distribution (MCC): {len(params.merchant_category_distribution)} categories")
    print("=" * 60)


if __name__ == "__main__":
    main()
