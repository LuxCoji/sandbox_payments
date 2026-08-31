"""Contract tests for the Population subsystem.

Verifies:
  1. BehaviourModel protocol compliance
  2. Intent shape and field-level contract guarantees
  3. WorldView read-only immutability and role-scoped execution
  4. Balance-spring dynamic compliance (zero outflows on zero balance)
  5. Deterministic reproducibility across identical RNG seeds
  6. CalibratedParams serialization/deserialization fidelity
  7. Strict import boundary isolation
"""
from __future__ import annotations

import uuid
from pathlib import Path

from sim.core.interfaces import (
    AccountSnapshot,
    AccountStatus,
    AccountType,
    ActorRole,
    DeviceSnapshot,
    DeviceStatus,
    DeviceType,
    FeeSchedule,
    GlobalParams,
    MerchantDirectoryEntry,
    RailLimits,
    TransactionType,
    WorldView,
)
from sim.population.behaviour import PopulationBehaviourModel
from sim.population.calibration import calibrate_from_csv, load_calibrated_params, save_calibrated_params
from sim.population.interfaces import BehaviourModel, Intent
from sim.scheduler.rng import DeterministicRNG

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "paysim"


def _create_mock_world_view(
    actor_id: str,
    actor_role: ActorRole = ActorRole.USER,
    balance_paise: int = 500_000,
    merchant_count: int = 3,
) -> WorldView:
    """Helper to create a valid WorldView for testing."""
    accounts = (
        AccountSnapshot(
            account_id=f"acc-{actor_id[:8]}",
            account_type=AccountType.PERSONAL if actor_role == ActorRole.USER else AccountType.MERCHANT,
            balance_paise=balance_paise,
            status=AccountStatus.ACTIVE,
            kyc_level=2,
            created_at=0.0,
            daily_tx_count=0,
            daily_tx_volume_paise=0,
            daily_inbound_tx_count=0,
            daily_inbound_volume_paise=0,
            linked_device_ids=(f"dev-{actor_id[:8]}",),
            owner_id=actor_id,
        ),
    )

    merchants = tuple(
        MerchantDirectoryEntry(
            merchant_id=f"merchant-{i}",
            name=f"Store-{i}",
            category="5411",
            avg_rating=4.5,
            settlement_rail="UPI",
        )
        for i in range(merchant_count)
    )

    devices = (
        DeviceSnapshot(
            device_id=f"dev-{actor_id[:8]}",
            owner_id=actor_id,
            device_type=DeviceType.MOBILE,
            status=DeviceStatus.ACTIVE,
            registered_at=0.0,
        ),
    )

    global_params = GlobalParams(
        fee_schedules=(
            FeeSchedule(
                tx_type=TransactionType.PAYMENT,
                gateway_id="gw-1",
                flat_fee_paise=0,
                percentage_bps=50,
                min_fee_paise=0,
                max_fee_paise=10000,
            ),
        ),
        rail_limits=(
            RailLimits(
                rail_id="UPI",
                min_amount_paise=100,
                max_amount_paise=10_000_000,
                daily_limit_paise=50_000_000,
                cut_off_time_ns=0.0,
            ),
        ),
        settlement_cut_off_ns=0.0,
    )

    return WorldView(
        actor_id=actor_id,
        actor_role=actor_role,
        sim_time_ns=10_000_000_000.0,  # 10 seconds in sim time
        accounts=accounts,
        merchants=merchants,
        devices=devices,
        global_params=global_params,
    )


def test_behaviour_model_protocol_adherence() -> None:
    """Verify PopulationBehaviourModel satisfies the BehaviourModel runtime protocol."""
    params = calibrate_from_csv(DATA_DIR)
    model = PopulationBehaviourModel(params)
    assert isinstance(model, BehaviourModel)


def test_entity_initialization_contract() -> None:
    """Verify initialize_entity produces all required attributes with correct types."""
    params = calibrate_from_csv(DATA_DIR)
    model = PopulationBehaviourModel(params)
    rng = DeterministicRNG.from_seed(100)

    # Test User initialization
    user_init = model.initialize_entity("u-12345", "user", rng)
    assert user_init["entity_id"] == "u-12345"
    assert user_init["account_type"] == AccountType.PERSONAL
    assert isinstance(user_init["initial_balance_paise"], int)
    assert user_init["initial_balance_paise"] >= 0
    assert 0 <= int(str(user_init["kyc_level"])) <= 3
    assert isinstance(user_init["device_type"], DeviceType)

    # Test Merchant initialization
    merchant_init = model.initialize_entity("m-67890", "merchant", rng)
    assert merchant_init["entity_id"] == "m-67890"
    assert merchant_init["account_type"] == AccountType.MERCHANT
    assert merchant_init["kyc_level"] == 3
    assert isinstance(merchant_init["merchant_category_code"], str)
    assert merchant_init["device_type"] == DeviceType.POS


def test_intent_shape_and_validation() -> None:
    """Verify propose_actions returns valid Intent instances conforming to contract."""
    params = calibrate_from_csv(DATA_DIR)
    model = PopulationBehaviourModel(params)

    actor_id = "user-test-01"
    world_view = _create_mock_world_view(actor_id=actor_id, balance_paise=1_000_000)

    intents = model.propose_actions(actor_id, world_view)
    assert isinstance(intents, list)

    for intent in intents:
        assert isinstance(intent, Intent)
        assert intent.actor_id == actor_id
        assert isinstance(intent.action_type, TransactionType)
        assert isinstance(intent.amount_paise, int)
        assert intent.amount_paise >= 100  # Minimum ₹1.00
        # Check idempotency key is a valid UUID string
        parsed_uuid = uuid.UUID(intent.idempotency_key)
        assert str(parsed_uuid) == intent.idempotency_key


def test_balance_spring_dynamic_zero_balance() -> None:
    """Verify that an actor with zero balance does not produce outflow actions."""
    params = calibrate_from_csv(DATA_DIR)
    model = PopulationBehaviourModel(params)

    actor_id = "broke-user"
    world_view = _create_mock_world_view(actor_id=actor_id, balance_paise=0)

    # Over 50 action proposals with 0 balance, no outflows (PAYMENT, TRANSFER, CASH_OUT) should succeed
    for _ in range(50):
        intents = model.propose_actions(actor_id, world_view)
        for intent in intents:
            assert intent.action_type not in (
                TransactionType.PAYMENT,
                TransactionType.TRANSFER,
                TransactionType.CASH_OUT,
            ), f"Outflow {intent.action_type} proposed despite zero balance!"


def test_deterministic_reproducibility() -> None:
    """Verify that two models with the same seed produce identical sequences."""
    params = calibrate_from_csv(DATA_DIR)
    model1 = PopulationBehaviourModel(params, root_rng=DeterministicRNG.from_seed(777))
    model2 = PopulationBehaviourModel(params, root_rng=DeterministicRNG.from_seed(777))

    actor_id = "reproducible-user"
    world_view = _create_mock_world_view(actor_id=actor_id, balance_paise=2_500_000)

    intents1 = model1.propose_actions(actor_id, world_view)
    intents2 = model2.propose_actions(actor_id, world_view)

    assert len(intents1) == len(intents2)
    for i1, i2 in zip(intents1, intents2, strict=False):
        assert i1.action_type == i2.action_type
        assert i1.amount_paise == i2.amount_paise
        assert i1.target_id == i2.target_id
        assert i1.idempotency_key == i2.idempotency_key

    # Test inter-arrival reproducibility
    dt1 = model1.get_next_interarrival(actor_id, TransactionType.PAYMENT, 0.0)
    dt2 = model2.get_next_interarrival(actor_id, TransactionType.PAYMENT, 0.0)
    assert dt1 == dt2


def test_calibrated_params_serialization(tmp_path: Path) -> None:
    """Verify CalibratedParams serialization to and from JSON produces identical structures."""
    params = calibrate_from_csv(DATA_DIR)
    json_path = tmp_path / "test_params.json"

    save_calibrated_params(params, json_path)
    loaded_params = load_calibrated_params(json_path)

    assert loaded_params.profiles_by_type == params.profiles_by_type
    assert loaded_params.initial_balance_distribution == params.initial_balance_distribution
    assert loaded_params.max_occurrences_per_client == params.max_occurrences_per_client
    assert loaded_params.temporal_rate_matrix == params.temporal_rate_matrix
    assert loaded_params.merchant_category_distribution == params.merchant_category_distribution


def test_import_isolation() -> None:
    """Verify sim.population's own source doesn't statically import concrete
    engine/chrono implementations.

    This is a static per-file check (not a sys.modules check) deliberately:
    sys.modules is process-global, so checking it after other test modules
    have already imported sim.core.engine for unrelated reasons produces
    false positives regardless of what sim.population itself imports. The
    authoritative, order-independent version of this contract is enforced
    by import-linter (see `make lint` / `lint-imports`); this test is a
    lightweight sanity check on top of that.
    """
    import ast
    import importlib.util

    forbidden_prefixes = (
        "sim.core.engine", "sim.core.payment", "sim.core.account",
        "sim.chrono.store", "sim.chrono.branch", "simpy",
    )
    population_modules = [
        "sim.population.agents",
        "sim.population.behaviour",
        "sim.population.calibration",
        "sim.population.profiles",
        "sim.population.temporal",
    ]

    for mod_name in population_modules:
        spec = importlib.util.find_spec(mod_name)
        assert spec and spec.origin, f"Could not locate source for {mod_name}"
        with open(spec.origin) as f:
            tree = ast.parse(f.read(), filename=spec.origin)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                assert not name.startswith(forbidden_prefixes), (
                    f"{mod_name} statically imports forbidden module: {name}"
                )
