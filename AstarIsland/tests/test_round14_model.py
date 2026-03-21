from __future__ import annotations

import numpy as np

from astarisland.application.analysis import AnalysisWorkflow
from astarisland.application.frontier_model import _cap_total_dynamic_mass, _rescale_seed_dynamic_mass
from astarisland.application.prediction import PredictionWorkflow
from astarisland.domain.models import AnalysisResult, MyPredictionSummary
from astarisland.domain.query_planner import plan_initial_observations
from astarisland.domain.scoring import seed_score

from .conftest import ground_truth_from_observations, make_mock_observations, uniform_prediction


class FakeRound14AnalysisApiClient:
    def __init__(self, round_detail, ground_truth):
        self.round_detail = round_detail
        self.ground_truth = ground_truth

    def my_predictions(self, round_id: str):
        return [
            MyPredictionSummary(
                seed_index=seed_index,
                argmax_grid=[[0] * self.round_detail.map_width for _ in range(self.round_detail.map_height)],
                confidence_grid=[[1.0] * self.round_detail.map_width for _ in range(self.round_detail.map_height)],
                score=100.0,
            )
            for seed_index in range(self.round_detail.seeds_count)
        ]

    def analysis(self, round_id: str, seed_index: int) -> AnalysisResult:
        prediction = self.ground_truth[seed_index].tolist()
        return AnalysisResult(
            prediction=prediction,
            ground_truth=prediction,
            score=100.0,
            width=self.round_detail.map_width,
            height=self.round_detail.map_height,
            initial_grid=self.round_detail.initial_states[seed_index].grid,
        )


def test_round14_phase_a_contains_hard_negative_corridor(fixture_round_detail) -> None:
    plan = plan_initial_observations(fixture_round_detail)

    assert len(plan.items_for_phase("A")) == 20
    for seed_index in range(fixture_round_detail.seeds_count):
        seed_items = [item for item in plan.items_for_phase("A") if item.seed_index == seed_index]
        assert len(seed_items) == 4
        assert "inland growth corridor" in seed_items[0].rationale.lower()
        assert "coastal and port corridor" in seed_items[1].rationale.lower()
        assert "ruin and rebuild corridor" in seed_items[2].rationale.lower()
        assert "hard-negative corridor" in seed_items[3].rationale.lower()


def test_round14_analysis_trains_and_persists_frontier_model(fixture_round_detail, artifact_store) -> None:
    artifact_store.save_round_detail(fixture_round_detail)
    observations = make_mock_observations(fixture_round_detail, runs_per_seed=3)
    for record in observations:
        artifact_store.save_observation(record)

    ground_truth = {
        seed_index: ground_truth_from_observations(
            observations,
            seed_index=seed_index,
            height=fixture_round_detail.map_height,
            width=fixture_round_detail.map_width,
        )
        for seed_index in range(fixture_round_detail.seeds_count)
    }

    workflow = AnalysisWorkflow(FakeRound14AnalysisApiClient(fixture_round_detail, ground_truth), artifact_store)  # type: ignore[arg-type]
    summary_first = workflow.analyze_completed_round(fixture_round_detail)
    summary_second = workflow.analyze_completed_round(fixture_round_detail)

    metadata = artifact_store.load_frontier_model_metadata()
    bundle = artifact_store.load_frontier_model_bundle()
    heuristic_calibration = artifact_store.load_heuristic_calibration()
    heuristic_backtest = artifact_store.load_heuristic_backtest_summary()

    assert summary_first["model_bundle_trained"] is True
    assert summary_second["average_score"] == summary_first["average_score"]
    assert metadata is not None
    assert bundle is not None
    assert heuristic_calibration is not None
    assert heuristic_backtest is not None
    assert metadata.model_version == "v3-active-frontier-gating"
    assert heuristic_calibration["model_version"] == "v4-regime-calibrated-heuristic"
    assert metadata.training_examples > 0
    assert metadata.replay_diagnostics


def test_round14_fitted_predictor_beats_uniform_on_fixture_round(fixture_round_detail, artifact_store) -> None:
    artifact_store.save_round_detail(fixture_round_detail)
    observations = make_mock_observations(fixture_round_detail, runs_per_seed=3)
    for record in observations:
        artifact_store.save_observation(record)

    ground_truth = {
        seed_index: ground_truth_from_observations(
            observations,
            seed_index=seed_index,
            height=fixture_round_detail.map_height,
            width=fixture_round_detail.map_width,
        )
        for seed_index in range(fixture_round_detail.seeds_count)
    }
    AnalysisWorkflow(FakeRound14AnalysisApiClient(fixture_round_detail, ground_truth), artifact_store).analyze_completed_round(  # type: ignore[arg-type]
        fixture_round_detail
    )

    predictions = PredictionWorkflow(artifact_store).build_predictions(fixture_round_detail)
    fitted = predictions[0]
    uniform = uniform_prediction(fixture_round_detail.map_height, fixture_round_detail.map_width, seed_index=0)

    fitted_score = seed_score(ground_truth[0], fitted.values)
    uniform_score = seed_score(ground_truth[0], uniform.values)

    assert artifact_store.load_frontier_model_bundle() is not None
    assert fitted_score > uniform_score


def test_round14_dynamic_mass_rescaling_and_caps_preserve_mass() -> None:
    probabilities = np.array(
        [
            [0.10, 0.35, 0.30, 0.15, 0.05, 0.05],
            [0.20, 0.25, 0.20, 0.15, 0.10, 0.10],
        ],
        dtype=float,
    )

    capped = _cap_total_dynamic_mass(probabilities[0], 0.12)
    rescaled = _rescale_seed_dynamic_mass(probabilities, scale=0.5)

    assert np.isclose(capped.sum(), 1.0)
    assert capped[1:4].sum() <= 0.12 + 1e-9
    assert np.allclose(rescaled.sum(axis=1), 1.0)
    assert float(rescaled[:, 1:4].sum(axis=1).mean()) < float(probabilities[:, 1:4].sum(axis=1).mean())
