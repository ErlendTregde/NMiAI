from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from astarisland.application.analysis import AnalysisWorkflow
from astarisland.application.prediction import PredictionWorkflow
from astarisland.domain.models import AnalysisResult, MyPredictionSummary
from astarisland.domain.scoring import seed_score
from astarisland.infrastructure.artifact_store import ArtifactStore
from astarisland.infrastructure.json_io import load_json_file

from .conftest import ground_truth_from_observations, make_mock_observations, uniform_prediction


class FakeAnalysisApiClient:
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


def _benchmark_archived_round(tmp_path: Path, source_round_id: str) -> dict[str, float | list[float] | str]:
    source_root = Path("data")
    source_round_dir = source_root / "rounds" / source_round_id
    if not source_round_dir.exists():
        pytest.skip(f"Archived round {source_round_id} is not available in this checkout")
    if not (source_round_dir / "analysis" / "seed-0.json").exists():
        pytest.skip(f"Archived analysis for round {source_round_id} is not available locally yet")

    source_store = ArtifactStore(source_root)
    store = ArtifactStore(tmp_path / f"benchmark-{source_round_id}")
    source_round = source_store.load_round_detail(source_round_id)
    train_round = source_round.model_copy(update={"id": f"{source_round_id}-train"})
    eval_round = source_round.model_copy(update={"id": f"{source_round_id}-eval"})
    store.save_round_detail(train_round)
    store.save_round_detail(eval_round)

    for record in source_store.load_observations(source_round_id):
        train_record = record.model_copy(
            update={
                "round_id": train_round.id,
                "request": record.request.model_copy(update={"round_id": train_round.id}),
            }
        )
        eval_record = record.model_copy(
            update={
                "round_id": eval_round.id,
                "request": record.request.model_copy(update={"round_id": eval_round.id}),
            }
        )
        store.save_observation(train_record)
        store.save_observation(eval_record)

    class FakeArchivedAnalysisApiClient:
        def my_predictions(self, round_id: str):
            payload = load_json_file(source_round_dir / "analysis" / "my-predictions.json")
            return [MyPredictionSummary.model_validate(item) for item in payload]

        def analysis(self, round_id: str, seed_index: int) -> AnalysisResult:
            payload = load_json_file(source_round_dir / "analysis" / f"seed-{seed_index}.json")
            return AnalysisResult.model_validate(payload)

    analysis_summary = AnalysisWorkflow(FakeArchivedAnalysisApiClient(), store).analyze_completed_round(train_round)
    predictions = PredictionWorkflow(store).build_predictions(eval_round)

    seed_scores: list[float] = []
    dynamic_mass_errors: list[float] = []
    high_entropy_port_gaps: list[float] = []
    high_entropy_ruin_gaps: list[float] = []
    high_entropy_settlement_gaps: list[float] = []
    covered_dynamic_kl: list[float] = []
    uncovered_dynamic_kl: list[float] = []

    for tensor in predictions:
        payload = load_json_file(source_round_dir / "analysis" / f"seed-{tensor.seed_index}.json")
        result = AnalysisResult.model_validate(payload)
        ground_truth = np.array(result.ground_truth, dtype=float)
        prediction = tensor.values
        seed_scores.append(seed_score(ground_truth, prediction))
        truth_dynamic = ground_truth[:, :, 1:4].sum(axis=-1)
        pred_dynamic = prediction[:, :, 1:4].sum(axis=-1)
        dynamic_mass_errors.append(abs(float(pred_dynamic.mean()) - float(truth_dynamic.mean())))
        entropy = -np.sum(
            np.where(ground_truth > 0, ground_truth * np.log(np.clip(ground_truth, 1e-12, None)), 0.0),
            axis=-1,
        )
        high_entropy = entropy >= 0.5
        if int(high_entropy.sum()) > 0:
            high_entropy_settlement_gaps.append(float(prediction[:, :, 1][high_entropy].mean() - ground_truth[:, :, 1][high_entropy].mean()))
            high_entropy_port_gaps.append(float(prediction[:, :, 2][high_entropy].mean() - ground_truth[:, :, 2][high_entropy].mean()))
            high_entropy_ruin_gaps.append(float(prediction[:, :, 3][high_entropy].mean() - ground_truth[:, :, 3][high_entropy].mean()))
        coverage_mask = np.zeros((source_round.map_height, source_round.map_width), dtype=bool)
        for record in store.load_observations(eval_round.id):
            if record.seed_index != tensor.seed_index:
                continue
            for x, y in record.request.to_viewport().cell_coordinates():
                coverage_mask[y, x] = True
        dynamic_mask = (truth_dynamic >= 0.20) | (entropy >= 0.55)
        weights = np.where(dynamic_mask & coverage_mask, entropy, 0.0)
        if float(weights.sum()) > 0.0:
            per_cell_kl = -np.sum(
                np.where(
                    ground_truth > 0,
                    ground_truth * np.log(np.clip(prediction, 1e-12, None) / np.clip(ground_truth, 1e-12, None)),
                    0.0,
                ),
                axis=-1,
            )
            covered_dynamic_kl.append(float((weights * per_cell_kl).sum() / weights.sum()))
        weights = np.where(dynamic_mask & ~coverage_mask, entropy, 0.0)
        if float(weights.sum()) > 0.0:
            per_cell_kl = -np.sum(
                np.where(
                    ground_truth > 0,
                    ground_truth * np.log(np.clip(prediction, 1e-12, None) / np.clip(ground_truth, 1e-12, None)),
                    0.0,
                ),
                axis=-1,
            )
            uncovered_dynamic_kl.append(float((weights * per_cell_kl).sum() / weights.sum()))

    prediction_summary = (store.round_dir(eval_round.id) / "summaries" / "prediction-summary.md").read_text(encoding="utf-8")
    return {
        "round_id": source_round_id,
        "average_score": float(sum(seed_scores) / len(seed_scores)),
        "seed_scores": seed_scores,
        "max_dynamic_mass_error": float(max(dynamic_mass_errors)),
        "mean_high_entropy_port_gap": float(np.mean(high_entropy_port_gaps)) if high_entropy_port_gaps else 0.0,
        "mean_high_entropy_ruin_gap": float(np.mean(high_entropy_ruin_gaps)) if high_entropy_ruin_gaps else 0.0,
        "mean_high_entropy_settlement_gap": float(np.mean(high_entropy_settlement_gaps)) if high_entropy_settlement_gaps else 0.0,
        "covered_dynamic_weighted_kl": float(np.mean(covered_dynamic_kl)) if covered_dynamic_kl else 0.0,
        "uncovered_dynamic_weighted_kl": float(np.mean(uncovered_dynamic_kl)) if uncovered_dynamic_kl else 0.0,
        "prediction_summary": prediction_summary,
        "heuristic_backtest_average": float(analysis_summary.get("heuristic_backtest_average", 0.0)),
    }


def test_analysis_workflow_reproduces_official_score_formula(fixture_round_detail, artifact_store) -> None:
    observations = make_mock_observations(fixture_round_detail, runs_per_seed=3)
    ground_truth = {
        seed_index: ground_truth_from_observations(
            observations,
            seed_index=seed_index,
            height=fixture_round_detail.map_height,
            width=fixture_round_detail.map_width,
        )
        for seed_index in range(fixture_round_detail.seeds_count)
    }

    workflow = AnalysisWorkflow(FakeAnalysisApiClient(fixture_round_detail, ground_truth), artifact_store)  # type: ignore[arg-type]
    summary = workflow.analyze_completed_round(fixture_round_detail)

    assert summary["average_score"] == 100.0


def test_baseline_predictor_beats_uniform_on_fixture_round(fixture_round_detail, artifact_store) -> None:
    artifact_store.save_round_detail(fixture_round_detail)
    observations = make_mock_observations(fixture_round_detail, runs_per_seed=3)
    for record in observations:
        artifact_store.save_observation(record)

    workflow = PredictionWorkflow(artifact_store)
    predictions = workflow.build_predictions(fixture_round_detail)
    baseline = predictions[0]
    ground_truth = ground_truth_from_observations(
        observations,
        seed_index=0,
        height=fixture_round_detail.map_height,
        width=fixture_round_detail.map_width,
    )
    uniform = uniform_prediction(fixture_round_detail.map_height, fixture_round_detail.map_width, seed_index=0)

    baseline_score = seed_score(ground_truth, baseline.values)
    uniform_score = seed_score(ground_truth, uniform.values)

    assert baseline_score > uniform_score


def test_dynamic_frontier_replay_reaches_round7_target(tmp_path) -> None:
    metrics = _benchmark_archived_round(tmp_path, "36e581f1-73f8-453f-ab98-cbe3052b701b")
    average_score = float(metrics["average_score"])
    seed_scores = metrics["seed_scores"]
    baseline_seed_scores = [20.6477, 22.5316, 25.6852, 21.3555, 25.1941]
    assert all(float(score) > baseline for score, baseline in zip(seed_scores, baseline_seed_scores, strict=True))
    assert average_score >= 31.0


def test_heuristic_v4_replay_hits_archived_round_targets_if_analysis_exists(tmp_path) -> None:
    round13 = _benchmark_archived_round(tmp_path, "7b4bda99-6165-4221-97cc-27880f5e6d95")
    round14 = _benchmark_archived_round(tmp_path, "d0a2c894-2162-4d49-86cf-435b9013f3b8")

    assert "fallback to heuristic" in str(round13["prediction_summary"])
    assert "v4-regime-calibrated-heuristic" in str(round14["prediction_summary"])

    assert float(round13["average_score"]) >= 36.0
    assert all(float(score) >= 36.0 for score in round13["seed_scores"])
    assert float(round13["mean_high_entropy_port_gap"]) <= 0.05
    assert float(round13["mean_high_entropy_ruin_gap"]) <= 0.04
    assert -0.03 <= float(round13["mean_high_entropy_settlement_gap"]) <= 0.04
    assert float(round13["heuristic_backtest_average"]) >= 40.0

    assert float(round14["average_score"]) >= 43.0
    assert all(float(score) >= 43.0 for score in round14["seed_scores"])
    assert float(round14["mean_high_entropy_port_gap"]) <= 0.05
    assert float(round14["mean_high_entropy_ruin_gap"]) <= 0.05
    assert -0.04 <= float(round14["mean_high_entropy_settlement_gap"]) <= 0.02
    assert float(round14["covered_dynamic_weighted_kl"]) <= float(round14["uncovered_dynamic_weighted_kl"]) + 0.01
