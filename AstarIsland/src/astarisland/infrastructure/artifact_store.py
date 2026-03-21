from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib

from astarisland.domain.models import (
    AnalysisResult,
    FrontierPriorTable,
    ModelBundle,
    ModelBundleMetadata,
    MyPredictionSummary,
    ObservationPlan,
    RecordedObservation,
    RoundDetail,
    SubmissionResult,
    WindowDynamicsSummary,
)
from astarisland.domain.tensors import PredictionTensor
from astarisland.infrastructure.json_io import dump_json_file, load_json_file


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def round_dir(self, round_id: str) -> Path:
        path = self.root / "rounds" / round_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _immutable_write(self, path: Path, payload: Any) -> Path:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite immutable artifact: {path}")
        dump_json_file(path, payload)
        return path

    def save_round_detail(self, round_detail: RoundDetail) -> Path:
        path = self.round_dir(round_detail.id) / "round-detail.json"
        dump_json_file(path, round_detail.model_dump(mode="json"))
        return path

    def load_round_detail(self, round_id: str) -> RoundDetail:
        payload = load_json_file(self.round_dir(round_id) / "round-detail.json")
        return RoundDetail.model_validate(payload)

    def save_observation_plan(self, plan: ObservationPlan) -> Path:
        path = self.round_dir(plan.round_id) / "observation-plan.json"
        dump_json_file(path, plan.model_dump(mode="json"))
        return path

    def load_observation_plan(self, round_id: str) -> ObservationPlan:
        payload = load_json_file(self.round_dir(round_id) / "observation-plan.json")
        return ObservationPlan.model_validate(payload)

    def save_plan_summary_markdown(self, round_id: str, content: str) -> Path:
        path = self.round_dir(round_id) / "summaries" / "observation-plan.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def save_observation(self, observation: RecordedObservation) -> Path:
        round_dir = self.round_dir(observation.round_id)
        observations_dir = round_dir / "observations" / "raw"
        observations_dir.mkdir(parents=True, exist_ok=True)
        viewport = observation.request.to_viewport()
        filename = (
            f"{observation.executed_at.strftime('%Y%m%dT%H%M%SZ')}_"
            f"{observation.phase}_seed{observation.seed_index}_"
            f"x{viewport.x}_y{viewport.y}_w{viewport.w}_h{viewport.h}.json"
        )
        path = observations_dir / filename
        self._immutable_write(path, observation.model_dump(mode="json"))
        return path

    def load_observations(self, round_id: str) -> list[RecordedObservation]:
        observations_dir = self.round_dir(round_id) / "observations" / "raw"
        if not observations_dir.exists():
            return []
        records = [
            RecordedObservation.model_validate(load_json_file(path))
            for path in sorted(observations_dir.glob("*.json"))
        ]
        return records

    def save_window_dynamics_summaries(self, round_id: str, summaries: dict[str, WindowDynamicsSummary]) -> Path:
        path = self.round_dir(round_id) / "observations" / "window-dynamics.json"
        payload = {
            "round_id": round_id,
            "summaries": [summary.model_dump(mode="json") for summary in summaries.values()],
        }
        dump_json_file(path, payload)
        return path

    def load_window_dynamics_summaries(self, round_id: str) -> dict[str, WindowDynamicsSummary]:
        path = self.round_dir(round_id) / "observations" / "window-dynamics.json"
        if not path.exists():
            return {}
        payload = load_json_file(path)
        return {
            item["window_key"]: WindowDynamicsSummary.model_validate(item)
            for item in payload.get("summaries", [])
        }

    def save_prediction_tensor(self, round_id: str, tensor: PredictionTensor) -> Path:
        predictions_dir = self.round_dir(round_id) / "predictions"
        predictions_dir.mkdir(parents=True, exist_ok=True)
        path = predictions_dir / f"seed-{tensor.seed_index}.json"
        payload = {
            "seed_index": tensor.seed_index,
            "height": tensor.height,
            "width": tensor.width,
            "prediction": tensor.tolist(),
        }
        dump_json_file(path, payload)
        return path

    def load_prediction_tensor(self, round_id: str, seed_index: int) -> PredictionTensor:
        payload = load_json_file(self.round_dir(round_id) / "predictions" / f"seed-{seed_index}.json")
        import numpy as np

        return PredictionTensor(seed_index=seed_index, values=np.array(payload["prediction"], dtype=float))

    def load_all_prediction_tensors(self, round_id: str) -> list[PredictionTensor]:
        predictions_dir = self.round_dir(round_id) / "predictions"
        if not predictions_dir.exists():
            return []
        return [
            self.load_prediction_tensor(round_id, int(path.stem.split("-")[-1]))
            for path in sorted(predictions_dir.glob("seed-*.json"))
        ]

    def save_submission_result(self, round_id: str, result: SubmissionResult) -> Path:
        directory = self.round_dir(round_id) / "submissions"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"seed-{result.seed_index}.json"
        dump_json_file(path, result.model_dump(mode="json"))
        return path

    def save_my_predictions(self, round_id: str, predictions: list[MyPredictionSummary]) -> Path:
        path = self.round_dir(round_id) / "analysis" / "my-predictions.json"
        dump_json_file(path, [item.model_dump(mode="json") for item in predictions])
        return path

    def save_analysis_result(self, round_id: str, seed_index: int, result: AnalysisResult) -> Path:
        directory = self.round_dir(round_id) / "analysis"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"seed-{seed_index}.json"
        self._immutable_write(path, result.model_dump(mode="json"))
        return path

    def load_analysis_results(self, round_id: str) -> dict[int, AnalysisResult]:
        directory = self.round_dir(round_id) / "analysis"
        if not directory.exists():
            return {}
        results: dict[int, AnalysisResult] = {}
        for path in sorted(directory.glob("seed-*.json")):
            seed_index = int(path.stem.split("-")[-1])
            results[seed_index] = AnalysisResult.model_validate(load_json_file(path))
        return results

    def save_feature_bucket_priors(self, round_id: str, payload: dict[str, Any]) -> Path:
        path = self.round_dir(round_id) / "analysis" / "feature-bucket-priors.json"
        dump_json_file(path, payload)
        return path

    def save_frontier_prior_table(self, round_id: str, table: FrontierPriorTable) -> Path:
        path = self.round_dir(round_id) / "analysis" / "frontier-priors.json"
        dump_json_file(path, table.model_dump(mode="json"))
        return path

    def load_frontier_prior_table(self, round_id: str) -> FrontierPriorTable | None:
        path = self.round_dir(round_id) / "analysis" / "frontier-priors.json"
        if not path.exists():
            return None
        return FrontierPriorTable.model_validate(load_json_file(path))

    def load_historical_feature_bucket_priors(self, *, exclude_round_id: str | None = None) -> dict[str, dict[str, Any]]:
        priors: dict[str, dict[str, Any]] = {}
        rounds_root = self.root / "rounds"
        if not rounds_root.exists():
            return priors
        for path in sorted(rounds_root.glob("*/analysis/feature-bucket-priors.json")):
            round_id = path.parent.parent.name
            if exclude_round_id is not None and round_id == exclude_round_id:
                continue
            payload = load_json_file(path)
            for key, value in payload.get("buckets", {}).items():
                bucket = priors.setdefault(key, {"counts": [0.0] * 6, "evidence": 0.0})
                bucket["counts"] = [
                    float(existing) + float(incoming)
                    for existing, incoming in zip(bucket["counts"], value["counts"], strict=True)
                ]
                bucket["evidence"] = float(bucket["evidence"]) + float(value.get("evidence", 0.0))
        return priors

    def save_aggregated_frontier_prior_table(self, table: FrontierPriorTable) -> Path:
        path = self.root / "learned" / "frontier-priors.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json_file(path, table.model_dump(mode="json"))
        return path

    def load_aggregated_frontier_prior_table(self) -> FrontierPriorTable | None:
        path = self.root / "learned" / "frontier-priors.json"
        if not path.exists():
            return None
        return FrontierPriorTable.model_validate(load_json_file(path))

    def save_frontier_model_bundle(self, bundle: ModelBundle) -> Path:
        path = self.root / "learned" / "frontier-model.joblib"
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, path)
        return path

    def load_frontier_model_bundle(self) -> ModelBundle | None:
        path = self.root / "learned" / "frontier-model.joblib"
        if not path.exists():
            return None
        return joblib.load(path)

    def save_frontier_model_metadata(self, metadata: ModelBundleMetadata) -> Path:
        path = self.root / "learned" / "frontier-model-metadata.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json_file(path, metadata.model_dump(mode="json"))
        return path

    def load_frontier_model_metadata(self) -> ModelBundleMetadata | None:
        path = self.root / "learned" / "frontier-model-metadata.json"
        if not path.exists():
            return None
        return ModelBundleMetadata.model_validate(load_json_file(path))

    def save_heuristic_calibration(self, payload: dict[str, Any]) -> Path:
        path = self.root / "learned" / "heuristic-calibration.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json_file(path, payload)
        return path

    def load_heuristic_calibration(self) -> dict[str, Any] | None:
        path = self.root / "learned" / "heuristic-calibration.json"
        if not path.exists():
            return None
        return load_json_file(path)

    def save_heuristic_backtest_summary(self, payload: dict[str, Any]) -> Path:
        path = self.root / "learned" / "heuristic-backtest-summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json_file(path, payload)
        return path

    def load_heuristic_backtest_summary(self) -> dict[str, Any] | None:
        path = self.root / "learned" / "heuristic-backtest-summary.json"
        if not path.exists():
            return None
        return load_json_file(path)

    def save_error_clusters(self, round_id: str, payload: dict[str, Any]) -> Path:
        path = self.round_dir(round_id) / "analysis" / "error-clusters.json"
        dump_json_file(path, payload)
        return path

    def load_error_clusters(self, round_id: str) -> dict[str, Any] | None:
        path = self.round_dir(round_id) / "analysis" / "error-clusters.json"
        if not path.exists():
            return None
        return load_json_file(path)

    def save_markdown_summary(self, round_id: str, name: str, content: str) -> Path:
        path = self.round_dir(round_id) / "summaries" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
