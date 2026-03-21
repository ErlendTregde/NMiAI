from __future__ import annotations

from collections import Counter

import numpy as np

from astarisland.domain.enums import PredictionClass, terrain_to_prediction_class
from astarisland.domain.features import build_feature_catalog
from astarisland.domain.query_planner import plan_initial_observations
from astarisland.domain.scoring import seed_score
from astarisland.domain.tensors import PredictionTensor, calibrate_prediction_tensor


def test_terrain_mapping_treats_plains_as_empty() -> None:
    assert terrain_to_prediction_class(11) == PredictionClass.EMPTY


def test_calibrate_prediction_tensor_applies_floor_and_normalizes() -> None:
    values = np.zeros((2, 2, 6), dtype=float)
    values[..., 0] = 1.0
    tensor = PredictionTensor(seed_index=0, values=values)

    calibrated = calibrate_prediction_tensor(tensor)

    assert np.all(calibrated.values >= 0.01 - 1e-9)
    assert np.allclose(calibrated.values.sum(axis=-1), 1.0, atol=1e-6)


def test_seed_score_is_perfect_for_identical_tensors() -> None:
    ground_truth = np.full((2, 2, 6), 1 / 6.0, dtype=float)
    prediction = ground_truth.copy()

    assert seed_score(ground_truth, prediction) == 100.0


def test_feature_catalog_marks_coastal_cells(fixture_round_detail) -> None:
    catalog = build_feature_catalog(fixture_round_detail.initial_states[0])

    assert catalog.feature_for(1, 1).coastal is True
    assert catalog.feature_for(7, 7).landmass_membership.startswith("lm")


def test_query_planner_allocates_four_phase_a_windows_per_seed(fixture_round_detail) -> None:
    plan = plan_initial_observations(fixture_round_detail)
    phase_a_items = plan.items_for_phase("A")
    by_seed = Counter(item.seed_index for item in phase_a_items)

    assert len(phase_a_items) == 20
    assert plan.phase_budgets == {"A": 20, "B": 20, "C": 10}
    assert all(count == 4 for count in by_seed.values())
