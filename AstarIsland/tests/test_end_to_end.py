from __future__ import annotations

import numpy as np

from astarisland.application.observations import ObservationWorkflow
from astarisland.application.prediction import PredictionWorkflow

from .conftest import make_mock_observations


def test_end_to_end_dry_run_builds_submit_ready_predictions(fixture_round_detail, artifact_store) -> None:
    artifact_store.save_round_detail(fixture_round_detail)
    planner = ObservationWorkflow(api_client=None, artifact_store=artifact_store)  # type: ignore[arg-type]
    plan = planner.create_plan(fixture_round_detail)

    for record in make_mock_observations(fixture_round_detail, runs_per_seed=3):
        artifact_store.save_observation(record)

    workflow = PredictionWorkflow(artifact_store)
    predictions = workflow.build_predictions(fixture_round_detail)

    assert len(plan.items_for_phase("A")) == 20
    assert plan.phase_budgets == {"A": 20, "B": 20, "C": 10}
    assert len(predictions) == fixture_round_detail.seeds_count
    for tensor in predictions:
        assert tensor.values.shape == (fixture_round_detail.map_height, fixture_round_detail.map_width, 6)
        assert np.allclose(tensor.values.sum(axis=-1), 1.0, atol=1e-6)
