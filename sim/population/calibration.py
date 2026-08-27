"""Calibration pipeline for population behaviour parameters.

Parses PaySim CSV files into strongly-typed CalibratedParams dataclasses,
with serialization and deserialization utilities.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from sim.core.interfaces import TransactionType
from sim.population.interfaces import ActionProfile, CalibratedParams

DEFAULT_MCC_DISTRIBUTION: dict[str, float] = {
    "5411": 0.30,  # Grocery Stores / Supermarkets
    "5812": 0.25,  # Restaurants / Dining
    "5311": 0.15,  # Department Stores / Retail
    "4814": 0.10,  # Telecom / Utilities
    "4900": 0.08,  # Electric / Gas / Sanitary
    "5912": 0.07,  # Drug Stores / Pharmacies
    "5732": 0.05,  # Electronics Stores
}


def calibrate_from_csv(data_dir: str | Path) -> CalibratedParams:
    """Parse PaySim CSV files into a CalibratedParams instance.

    Args:
        data_dir: Directory containing the 5 PaySim CSV files.

    Returns:
        A fully constructed, immutable CalibratedParams object.
    """
    data_path = Path(data_dir)

    # 0. Parse transactionsTypes.csv -> canonical set of valid transaction
    # type codes, used below to validate the other CSVs reference only
    # recognized types instead of silently accepting typos/stale codes.
    valid_tx_types: set[TransactionType] = set()
    types_csv = data_path / "transactionsTypes.csv"
    if types_csv.exists():
        with open(types_csv, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                code = row["code"].strip()
                try:
                    valid_tx_types.add(TransactionType(code))
                except ValueError:
                    raise ValueError(
                        f"transactionsTypes.csv declares unknown TransactionType code: {code!r}"
                    ) from None

    def _validate_type(tx_type: TransactionType, source_csv: str) -> None:
        if valid_tx_types and tx_type not in valid_tx_types:
            raise ValueError(
                f"{source_csv} references TransactionType {tx_type.value!r} "
                f"not declared in transactionsTypes.csv"
            )

    # 1. Parse clientsProfiles.csv
    profiles_by_type: dict[TransactionType, list[ActionProfile]] = {}
    profiles_csv = data_path / "clientsProfiles.csv"
    if not profiles_csv.exists():
        raise FileNotFoundError(f"Missing {profiles_csv}")

    with open(profiles_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tx_type = TransactionType(row["stepType"].strip())
            _validate_type(tx_type, "clientsProfiles.csv")
            min_count = int(row["minCount"])
            max_count = int(row["maxCount"])
            # Convert currency float to integer paise (₹1.00 = 100 paise)
            avg_amount_paise = int(round(float(row["avgAmount"]) * 100))
            std_amount_paise = int(round(float(row["stdAmount"]) * 100))
            frequency = float(row["probability"])

            profile = ActionProfile(
                action_type=tx_type,
                min_count=min_count,
                max_count=max_count,
                avg_amount_paise=avg_amount_paise,
                std_amount_paise=std_amount_paise,
                frequency=frequency,
            )
            profiles_by_type.setdefault(tx_type, []).append(profile)

    frozen_profiles_by_type = {
        k: tuple(v) for k, v in profiles_by_type.items()
    }

    # 2. Parse initialBalancesDistribution.csv
    initial_balances: list[tuple[int, int, float]] = []
    balances_csv = data_path / "initialBalancesDistribution.csv"
    if not balances_csv.exists():
        raise FileNotFoundError(f"Missing {balances_csv}")

    with open(balances_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            min_balance_paise = int(round(float(row["minBalance"]) * 100))
            max_balance_paise = int(round(float(row["maxBalance"]) * 100))
            prob = float(row["probability"])
            initial_balances.append((min_balance_paise, max_balance_paise, prob))

    # 3. Parse maxOccurrencesPerClient.csv
    max_occurrences: dict[TransactionType, int] = {}
    max_occ_csv = data_path / "maxOccurrencesPerClient.csv"
    if not max_occ_csv.exists():
        raise FileNotFoundError(f"Missing {max_occ_csv}")

    with open(max_occ_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tx_type = TransactionType(row["stepType"].strip())
            _validate_type(tx_type, "maxOccurrencesPerClient.csv")
            max_occurrences[tx_type] = int(row["maxOccurrences"])

    # 4. Parse aggregatedTransactions.csv -> 24x7 matrix per TransactionType
    # Initialize 7 days x 24 hours rate matrices
    temp_matrices: dict[TransactionType, list[list[float]]] = {}
    agg_csv = data_path / "aggregatedTransactions.csv"
    if not agg_csv.exists():
        raise FileNotFoundError(f"Missing {agg_csv}")

    with open(agg_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            day_idx = int(row["day"]) - 1  # 1-7 -> 0-6
            hour_idx = int(row["hour"])    # 0-23
            tx_type = TransactionType(row["stepType"].strip())
            _validate_type(tx_type, "aggregatedTransactions.csv")
            rate = float(row["normalizedRate"])

            if tx_type not in temp_matrices:
                temp_matrices[tx_type] = [[0.0 for _ in range(24)] for _ in range(7)]
            temp_matrices[tx_type][day_idx][hour_idx] = rate

    frozen_temporal_matrix = {
        tx_type: tuple(tuple(row) for row in matrix)
        for tx_type, matrix in temp_matrices.items()
    }

    return CalibratedParams(
        profiles_by_type=frozen_profiles_by_type,
        initial_balance_distribution=tuple(initial_balances),
        max_occurrences_per_client=max_occurrences,
        temporal_rate_matrix=frozen_temporal_matrix,
        merchant_category_distribution=DEFAULT_MCC_DISTRIBUTION,
    )


def save_calibrated_params(params: CalibratedParams, output_file: str | Path) -> None:
    """Serialize CalibratedParams to JSON."""
    data: dict[str, Any] = {
        "profiles_by_type": {
            tx_type.value: [
                {
                    "action_type": p.action_type.value,
                    "min_count": p.min_count,
                    "max_count": p.max_count,
                    "avg_amount_paise": p.avg_amount_paise,
                    "std_amount_paise": p.std_amount_paise,
                    "frequency": p.frequency,
                }
                for p in profiles
            ]
            for tx_type, profiles in params.profiles_by_type.items()
        },
        "initial_balance_distribution": [
            [min_b, max_b, prob] for min_b, max_b, prob in params.initial_balance_distribution
        ],
        "max_occurrences_per_client": {
            tx_type.value: count for tx_type, count in params.max_occurrences_per_client.items()
        },
        "temporal_rate_matrix": {
            tx_type.value: matrix for tx_type, matrix in params.temporal_rate_matrix.items()
        },
        "merchant_category_distribution": params.merchant_category_distribution,
    }

    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_calibrated_params(input_file: str | Path) -> CalibratedParams:
    """Deserialize CalibratedParams from JSON."""
    in_path = Path(input_file)
    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    profiles_by_type: dict[TransactionType, tuple[ActionProfile, ...]] = {
        TransactionType(tx_key): tuple(
            ActionProfile(
                action_type=TransactionType(p["action_type"]),
                min_count=p["min_count"],
                max_count=p["max_count"],
                avg_amount_paise=p["avg_amount_paise"],
                std_amount_paise=p["std_amount_paise"],
                frequency=p["frequency"],
            )
            for p in profiles
        )
        for tx_key, profiles in data["profiles_by_type"].items()
    }

    initial_balance_distribution = tuple(
        (int(item[0]), int(item[1]), float(item[2]))
        for item in data["initial_balance_distribution"]
    )

    max_occurrences_per_client = {
        TransactionType(tx_key): int(count)
        for tx_key, count in data["max_occurrences_per_client"].items()
    }

    temporal_rate_matrix = {
        TransactionType(tx_key): tuple(tuple(float(v) for v in row) for row in matrix)
        for tx_key, matrix in data["temporal_rate_matrix"].items()
    }

    merchant_category_distribution = {
        str(k): float(v) for k, v in data["merchant_category_distribution"].items()
    }

    return CalibratedParams(
        profiles_by_type=profiles_by_type,
        initial_balance_distribution=initial_balance_distribution,
        max_occurrences_per_client=max_occurrences_per_client,
        temporal_rate_matrix=temporal_rate_matrix,
        merchant_category_distribution=merchant_category_distribution,
    )
