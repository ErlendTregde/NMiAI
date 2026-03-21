from __future__ import annotations

import numpy as np

from astarisland.application.heuristic_v4 import (
    default_heuristic_calibration,
    predict_seed_with_heuristic_v4,
)
from astarisland.domain.frontier import build_frontier_feature_catalog, build_window_dynamics_summaries

from .conftest import make_mock_observations


def _same_cell_counts(width: int, height: int, observations) -> np.ndarray:
    counts = np.zeros((height, width, 6), dtype=float)
    for record in observations:
        viewport = record.request.to_viewport()
        for offset_y, row in enumerate(record.result.grid):
            for offset_x, value in enumerate(row):
                x = viewport.x + offset_x
                y = viewport.y + offset_y
                counts[y, x, 0 if value in {0, 10, 11} else int(value)] += 1.0
    return counts


def test_heuristic_v4_enforces_non_coastal_port_caps_and_reports_regimes(fixture_round_detail) -> None:
    observations = [record for record in make_mock_observations(fixture_round_detail, runs_per_seed=3) if record.seed_index == 0]
    window_summaries = build_window_dynamics_summaries(fixture_round_detail.id, observations)
    initial_state = fixture_round_detail.initial_states[0]
    catalog = build_frontier_feature_catalog(
        initial_state,
        observations=observations,
        window_summaries=window_summaries,
        historical_priors=None,
    )
    values, debug = predict_seed_with_heuristic_v4(
        frontier_catalog=catalog,
        same_cell_counts=_same_cell_counts(fixture_round_detail.map_width, fixture_round_detail.map_height, observations),
        historical_priors=None,
        calibration=default_heuristic_calibration(),
    )

    for (x, y), feature in catalog.per_cell.items():
        if not feature.base.coastal:
            assert values[y, x, 2] <= 0.020001

    assert np.allclose(values.sum(axis=-1), 1.0)
    assert np.isclose(sum(debug.regime_fractions.values()), 1.0)
    assert any(fraction > 0.0 for fraction in debug.regime_fractions.values())


def test_heuristic_v4_dynamic_correction_moves_toward_seed_target(fixture_round_detail) -> None:
    observations = [record for record in make_mock_observations(fixture_round_detail, runs_per_seed=3) if record.seed_index == 1]
    window_summaries = build_window_dynamics_summaries(fixture_round_detail.id, observations)
    initial_state = fixture_round_detail.initial_states[1]
    catalog = build_frontier_feature_catalog(
        initial_state,
        observations=observations,
        window_summaries=window_summaries,
        historical_priors=None,
    )
    _, debug = predict_seed_with_heuristic_v4(
        frontier_catalog=catalog,
        same_cell_counts=_same_cell_counts(fixture_round_detail.map_width, fixture_round_detail.map_height, observations),
        historical_priors=None,
        calibration=default_heuristic_calibration(),
    )

    before_error = abs(debug.mean_dynamic_before_correction - debug.target_dynamic_mass)
    after_error = abs(debug.mean_dynamic_after_correction - debug.target_dynamic_mass)
    assert after_error <= before_error + 1e-9
