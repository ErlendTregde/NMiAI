from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from astarisland.application.frontier_model import train_frontier_model_bundle
from astarisland.application.heuristic_v4 import HEURISTIC_MODEL_VERSION, fit_heuristic_calibration
from astarisland.domain.features import build_feature_catalog
from astarisland.domain.frontier import FrontierFeatureCatalog, build_frontier_feature_catalog, build_window_dynamics_summaries
from astarisland.domain.models import (
    AnalysisResult,
    CalibrationBin,
    CalibrationProfile,
    FrontierPriorBucket,
    FrontierPriorTable,
    RecordedObservation,
    RoundDetail,
)
from astarisland.domain.scoring import entropy_weighted_kl, kl_divergence, seed_score
from astarisland.infrastructure.api_client import AstarIslandApiClient
from astarisland.infrastructure.artifact_store import ArtifactStore
from astarisland.infrastructure.json_io import load_json_file


class AnalysisWorkflow:
    def __init__(self, api_client: AstarIslandApiClient, artifact_store: ArtifactStore) -> None:
        self.api_client = api_client
        self.artifact_store = artifact_store

    def analyze_completed_round(self, round_detail: RoundDetail) -> dict[str, Any]:
        self.artifact_store.save_round_detail(round_detail)
        my_predictions = self.api_client.my_predictions(round_detail.id)
        self.artifact_store.save_my_predictions(round_detail.id, my_predictions)

        observations = self.artifact_store.load_observations(round_detail.id)
        window_summaries = self.artifact_store.load_window_dynamics_summaries(round_detail.id)
        if not window_summaries and observations:
            window_summaries = build_window_dynamics_summaries(round_detail.id, observations)
            self.artifact_store.save_window_dynamics_summaries(round_detail.id, window_summaries)
        per_seed_observations = self._group_observations_by_seed(observations)
        saved_analysis_results = self.artifact_store.load_analysis_results(round_detail.id)

        results: dict[int, AnalysisResult] = {}
        seed_scores_local: list[float] = []
        feature_bucket_counts: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(6, dtype=float))
        feature_bucket_evidence: dict[str, float] = defaultdict(float)
        frontier_bucket_totals: dict[str, dict[str, Any]] = defaultdict(self._new_frontier_bucket_total)
        calibration_totals: dict[str, dict[tuple[float, float], dict[str, float]]] = defaultdict(dict)
        round_error_clusters: dict[str, list[dict[str, Any]]] = {
            "false_positive_port": [],
            "false_positive_ruin": [],
            "false_negative_settlement": [],
        }
        markdown_lines = [
            f"# Analysis Summary for Round {round_detail.id}",
            "",
        ]

        for seed_index, initial_state in enumerate(round_detail.initial_states):
            analysis = saved_analysis_results.get(seed_index)
            if analysis is None:
                analysis = self.api_client.analysis(round_detail.id, seed_index)
                self.artifact_store.save_analysis_result(round_detail.id, seed_index, analysis)
            results[seed_index] = analysis

            prediction = np.array(analysis.prediction, dtype=float)
            ground_truth = np.array(analysis.ground_truth, dtype=float)
            local_score = seed_score(ground_truth, prediction)
            weighted_kl = entropy_weighted_kl(ground_truth, prediction)
            seed_scores_local.append(local_score)

            feature_catalog = build_feature_catalog(initial_state)
            seed_observations = per_seed_observations.get(seed_index, [])
            coverage_mask = self._coverage_mask(
                height=round_detail.map_height,
                width=round_detail.map_width,
                observations=seed_observations,
            )
            frontier_catalog = build_frontier_feature_catalog(
                initial_state,
                observations=seed_observations,
                window_summaries=window_summaries,
                historical_priors=None,
            )
            entropy_map = self._entropy_map(ground_truth)
            dynamic_mask = self._dynamic_cell_mask(ground_truth, entropy_map, frontier_catalog)
            high_entropy_mask = entropy_map >= 0.50
            covered_dynamic_kl = self._subset_weighted_kl(ground_truth, prediction, dynamic_mask & coverage_mask)
            uncovered_dynamic_kl = self._subset_weighted_kl(ground_truth, prediction, dynamic_mask & ~coverage_mask)
            truth_dynamic_mean = float(ground_truth[:, :, 1:4].sum(axis=-1).mean())
            pred_dynamic_mean = float(prediction[:, :, 1:4].sum(axis=-1).mean())
            settlement_gap = float(prediction[:, :, 1].mean() - ground_truth[:, :, 1].mean())
            port_gap = float(prediction[:, :, 2].mean() - ground_truth[:, :, 2].mean())
            ruin_gap = float(prediction[:, :, 3].mean() - ground_truth[:, :, 3].mean())
            high_entropy_dynamic_gap = self._masked_mean(
                prediction[:, :, 1:4].sum(axis=-1) - ground_truth[:, :, 1:4].sum(axis=-1),
                high_entropy_mask,
            )
            high_entropy_settlement_gap = self._masked_mean(prediction[:, :, 1] - ground_truth[:, :, 1], high_entropy_mask)
            high_entropy_port_gap = self._masked_mean(prediction[:, :, 2] - ground_truth[:, :, 2], high_entropy_mask)
            high_entropy_ruin_gap = self._masked_mean(prediction[:, :, 3] - ground_truth[:, :, 3], high_entropy_mask)
            hotspots = self._top_dynamic_hotspots(entropy_map, limit=5)
            false_positive_cells = self._top_dynamic_error_cells(
                ground_truth=ground_truth,
                prediction=prediction,
                entropy_map=entropy_map,
                mode="false_positive",
                limit=5,
            )
            false_negative_cells = self._top_dynamic_error_cells(
                ground_truth=ground_truth,
                prediction=prediction,
                entropy_map=entropy_map,
                mode="false_negative",
                limit=5,
            )
            round_error_clusters["false_positive_port"].extend(
                self._top_class_gap_cells(
                    ground_truth=ground_truth,
                    prediction=prediction,
                    entropy_map=entropy_map,
                    class_index=2,
                    mode="false_positive",
                    seed_index=seed_index,
                )
            )
            round_error_clusters["false_positive_ruin"].extend(
                self._top_class_gap_cells(
                    ground_truth=ground_truth,
                    prediction=prediction,
                    entropy_map=entropy_map,
                    class_index=3,
                    mode="false_positive",
                    seed_index=seed_index,
                )
            )
            round_error_clusters["false_negative_settlement"].extend(
                self._top_class_gap_cells(
                    ground_truth=ground_truth,
                    prediction=prediction,
                    entropy_map=entropy_map,
                    class_index=1,
                    mode="false_negative",
                    seed_index=seed_index,
                )
            )

            for y in range(round_detail.map_height):
                for x in range(round_detail.map_width):
                    bucket_key = feature_catalog.feature_for(x, y).bucket_key()
                    feature_bucket_counts[bucket_key] += ground_truth[y, x]
                    feature_bucket_evidence[bucket_key] += 1.0
                    self._record_frontier_bucket(
                        frontier_bucket_totals,
                        frontier_catalog=frontier_catalog,
                        x=x,
                        y=y,
                        ground_truth_cell=ground_truth[y, x],
                        entropy=float(entropy_map[y, x]),
                        active_frontier=bool(entropy_map[y, x] >= 0.45 or ground_truth[y, x, 1:4].sum() >= 0.15),
                    )

            self._record_calibration(calibration_totals["overall"], prediction, ground_truth, np.ones_like(dynamic_mask, dtype=bool))
            self._record_calibration(calibration_totals["frontier_dynamic"], prediction, ground_truth, dynamic_mask)
            self._record_calibration(calibration_totals["high_entropy"], prediction, ground_truth, high_entropy_mask)

            markdown_lines.extend(
                [
                    f"## Seed {seed_index}",
                    "",
                    f"- API score: {analysis.score}",
                    f"- Local score reproduction: {local_score:.4f}",
                    f"- Weighted KL: {weighted_kl:.6f}",
                    f"- Covered dynamic weighted KL: {covered_dynamic_kl:.6f}",
                    f"- Uncovered dynamic weighted KL: {uncovered_dynamic_kl:.6f}",
                    f"- Dynamic mass gap (pred - truth): {pred_dynamic_mean - truth_dynamic_mean:.4f}",
                    f"- High-entropy dynamic mass gap (pred - truth): {high_entropy_dynamic_gap:.4f}",
                    f"- Settlement marginal gap (pred - truth): {settlement_gap:.4f}",
                    f"- Port marginal gap (pred - truth): {port_gap:.4f}",
                    f"- Ruin marginal gap (pred - truth): {ruin_gap:.4f}",
                    f"- High-entropy Settlement gap (pred - truth): {high_entropy_settlement_gap:.4f}",
                    f"- High-entropy Port gap (pred - truth): {high_entropy_port_gap:.4f}",
                    f"- High-entropy Ruin gap (pred - truth): {high_entropy_ruin_gap:.4f}",
                    "- Dynamic hotspots: " + ", ".join(hotspots),
                    "- Top false-positive dynamic cells: " + ", ".join(false_positive_cells),
                    "- Top false-negative dynamic cells: " + ", ".join(false_negative_cells),
                    "",
                ]
            )

        priors_payload = {
            "round_id": round_detail.id,
            "buckets": {
                key: {
                    "counts": counts.tolist(),
                    "evidence": feature_bucket_evidence[key],
                    "mean_distribution": (counts / counts.sum()).tolist() if counts.sum() else [1 / 6.0] * 6,
                }
                for key, counts in feature_bucket_counts.items()
            },
        }
        self.artifact_store.save_feature_bucket_priors(round_detail.id, priors_payload)

        frontier_prior_table = self._build_frontier_prior_table(
            round_id=round_detail.id,
            bucket_totals=frontier_bucket_totals,
            calibration_totals=calibration_totals,
        )
        self.artifact_store.save_frontier_prior_table(round_detail.id, frontier_prior_table)
        aggregated_table = self._aggregate_frontier_prior_tables()
        self.artifact_store.save_aggregated_frontier_prior_table(aggregated_table)
        model_bundle, model_metadata = train_frontier_model_bundle(self.artifact_store, aggregated_table)
        if model_bundle is not None:
            self.artifact_store.save_frontier_model_bundle(model_bundle)
        self.artifact_store.save_frontier_model_metadata(model_metadata)
        heuristic_calibration, heuristic_backtest = fit_heuristic_calibration(self.artifact_store, aggregated_table)
        self.artifact_store.save_heuristic_calibration(heuristic_calibration)
        self.artifact_store.save_heuristic_backtest_summary(heuristic_backtest)
        self.artifact_store.save_error_clusters(
            round_detail.id,
            {
                "round_id": round_detail.id,
                "false_positive_port": self._truncate_cluster_list(round_error_clusters["false_positive_port"]),
                "false_positive_ruin": self._truncate_cluster_list(round_error_clusters["false_positive_ruin"]),
                "false_negative_settlement": self._truncate_cluster_list(round_error_clusters["false_negative_settlement"]),
            },
        )
        current_round_replay = next(
            (diagnostics for diagnostics in model_metadata.replay_diagnostics if diagnostics.round_id == round_detail.id),
            None,
        )

        markdown_lines.extend(
            [
                "## Learned Frontier Priors",
                "",
                f"- Round frontier buckets: {len(frontier_prior_table.buckets)}",
                f"- Aggregated frontier buckets: {len(aggregated_table.buckets)}",
                f"- Calibration profiles: {', '.join(sorted(frontier_prior_table.calibration_profiles)) or 'none'}",
                "",
                "## Fitted Frontier Model",
                "",
                f"- Model version: {model_metadata.model_version}",
                f"- Training rounds: {len(model_metadata.rounds_included)}",
                f"- Training examples: {model_metadata.training_examples}",
                f"- Gate examples: {model_metadata.gate_examples}",
                f"- Persisted bundle: {'yes' if model_bundle is not None else 'no'}",
            ]
        )
        if current_round_replay is not None:
            markdown_lines.extend(
                [
                    f"- Replay average score on this round: {current_round_replay.average_score:.3f}",
                    f"- Replay mean dynamic mass gap: {current_round_replay.mean_dynamic_mass_gap:.4f}",
                    f"- Replay high-entropy Port gap: {current_round_replay.mean_high_entropy_port_gap:.4f}",
                    f"- Replay max per-seed dynamic-mass error: {current_round_replay.max_seed_dynamic_mass_error:.4f}",
                ]
            )
        markdown_lines.extend(
            [
                "",
                "## Heuristic Calibration",
                "",
                f"- Heuristic model version: {heuristic_calibration['model_version']}",
                f"- Calibration rounds: {len(heuristic_calibration.get('generated_from_rounds', []))}",
                f"- Backtest average score: {float(heuristic_backtest.get('average_score', 0.0)):.3f}",
                "- Coefficients: "
                + ", ".join(
                    f"{key}={value:.3f}" if isinstance(value, (int, float)) else f"{key}={value}"
                    for key, value in sorted(heuristic_calibration.get("coefficients", {}).items())
                ),
                "",
                "## Error Clusters",
                "",
                "- Top false-positive Port cells: "
                + ", ".join(self._format_cluster_entry(item) for item in self._truncate_cluster_list(round_error_clusters["false_positive_port"])),
                "- Top false-positive Ruin cells: "
                + ", ".join(self._format_cluster_entry(item) for item in self._truncate_cluster_list(round_error_clusters["false_positive_ruin"])),
                "- Top false-negative Settlement cells: "
                + ", ".join(self._format_cluster_entry(item) for item in self._truncate_cluster_list(round_error_clusters["false_negative_settlement"])),
            ]
        )
        self.artifact_store.save_markdown_summary(round_detail.id, "analysis-summary", "\n".join(markdown_lines))

        return {
            "seed_scores": seed_scores_local,
            "average_score": float(sum(seed_scores_local) / len(seed_scores_local)) if seed_scores_local else 0.0,
            "analysis_results": results,
            "my_predictions_count": len(my_predictions),
            "frontier_prior_buckets": len(frontier_prior_table.buckets),
            "model_bundle_trained": model_bundle is not None,
            "model_bundle_rounds": len(model_metadata.rounds_included),
            "model_bundle_version": model_metadata.model_version,
            "heuristic_model_version": HEURISTIC_MODEL_VERSION,
            "heuristic_backtest_average": float(heuristic_backtest.get("average_score", 0.0)),
        }

    def _group_observations_by_seed(self, observations: list[RecordedObservation]) -> dict[int, list[RecordedObservation]]:
        grouped: dict[int, list[RecordedObservation]] = defaultdict(list)
        for record in observations:
            grouped[record.seed_index].append(record)
        return grouped

    def _coverage_mask(self, *, height: int, width: int, observations: list[RecordedObservation]) -> np.ndarray:
        mask = np.zeros((height, width), dtype=bool)
        for record in observations:
            for x, y in record.request.to_viewport().cell_coordinates():
                mask[y, x] = True
        return mask

    def _entropy_map(self, tensor: np.ndarray) -> np.ndarray:
        clipped = np.clip(tensor, 1e-12, None)
        return -np.sum(np.where(tensor > 0, tensor * np.log(clipped), 0.0), axis=-1)

    def _dynamic_cell_mask(
        self,
        ground_truth: np.ndarray,
        entropy_map: np.ndarray,
        frontier_catalog: FrontierFeatureCatalog,
    ) -> np.ndarray:
        dynamic_mass = ground_truth[:, :, 1:4].sum(axis=-1)
        mask = dynamic_mass >= 0.20
        mask |= entropy_map >= 0.55
        for (x, y), feature in frontier_catalog.per_cell.items():
            if feature.frontier_eligible and (feature.hotspot_score > 0.0 or feature.observed_dynamic_rate > 0.0):
                mask[y, x] = True
        return mask

    def _subset_weighted_kl(self, ground_truth: np.ndarray, prediction: np.ndarray, mask: np.ndarray) -> float:
        if not np.any(mask):
            return 0.0
        entropy_map = self._entropy_map(ground_truth)
        weights = np.where(mask, entropy_map, 0.0)
        if float(weights.sum()) == 0.0:
            return 0.0
        per_cell_kl = kl_divergence(ground_truth, prediction)
        return float((weights * per_cell_kl).sum() / weights.sum())

    def _top_dynamic_hotspots(self, entropy_map: np.ndarray, *, limit: int) -> list[str]:
        flat_indices = np.argsort(entropy_map, axis=None)[::-1][:limit]
        hotspots: list[str] = []
        for flat_index in flat_indices:
            y, x = np.unravel_index(flat_index, entropy_map.shape)
            hotspots.append(f"({x},{y}) H={entropy_map[y, x]:.3f}")
        return hotspots

    def _new_frontier_bucket_total(self) -> dict[str, Any]:
        return {
            "counts": np.zeros(6, dtype=float),
            "evidence": 0.0,
            "entropy_total": 0.0,
            "dynamic_mass_total": 0.0,
            "hotspot_total": 0.0,
            "active_frontier_total": 0.0,
            "settlement_mass_total": 0.0,
            "port_mass_total": 0.0,
            "ruin_mass_total": 0.0,
        }

    def _record_frontier_bucket(
        self,
        bucket_totals: dict[str, dict[str, Any]],
        *,
        frontier_catalog: FrontierFeatureCatalog,
        x: int,
        y: int,
        ground_truth_cell: np.ndarray,
        entropy: float,
        active_frontier: bool,
    ) -> None:
        feature = frontier_catalog.feature_for(x, y)
        bucket_key = feature.bucket_key()
        bucket = bucket_totals[bucket_key]
        bucket["counts"] += ground_truth_cell
        bucket["evidence"] += 1.0
        bucket["entropy_total"] += entropy
        bucket["dynamic_mass_total"] += float(ground_truth_cell[1:4].sum())
        bucket["hotspot_total"] += 0.5 * float(feature.hotspot_score) + 0.5 * entropy
        bucket["active_frontier_total"] += float(active_frontier)
        bucket["settlement_mass_total"] += float(ground_truth_cell[1])
        bucket["port_mass_total"] += float(ground_truth_cell[2])
        bucket["ruin_mass_total"] += float(ground_truth_cell[3])

    def _record_calibration(
        self,
        profile_totals: dict[tuple[float, float], dict[str, float]],
        prediction: np.ndarray,
        ground_truth: np.ndarray,
        mask: np.ndarray,
    ) -> None:
        confidence = prediction.max(axis=-1)
        correct = (prediction.argmax(axis=-1) == ground_truth.argmax(axis=-1)).astype(float)
        bin_edges = (0.0, 0.25, 0.45, 0.60, 0.75, 0.90, 1.01)
        for lower, upper in zip(bin_edges[:-1], bin_edges[1:], strict=True):
            in_bin = mask & (confidence >= lower) & (confidence < upper)
            count = int(in_bin.sum())
            if count == 0:
                profile_totals.setdefault((lower, upper), {"count": 0.0, "correct": 0.0, "confidence": 0.0})
                continue
            bucket = profile_totals.setdefault((lower, upper), {"count": 0.0, "correct": 0.0, "confidence": 0.0})
            bucket["count"] += float(count)
            bucket["correct"] += float(correct[in_bin].sum())
            bucket["confidence"] += float(confidence[in_bin].sum())

    def _build_frontier_prior_table(
        self,
        *,
        round_id: str,
        bucket_totals: dict[str, dict[str, Any]],
        calibration_totals: dict[str, dict[tuple[float, float], dict[str, float]]],
    ) -> FrontierPriorTable:
        buckets = {
            key: FrontierPriorBucket(
                counts=value["counts"].tolist(),
                evidence=float(value["evidence"]),
                entropy_total=float(value["entropy_total"]),
                dynamic_mass_total=float(value["dynamic_mass_total"]),
                hotspot_total=float(value["hotspot_total"]),
                active_frontier_total=float(value["active_frontier_total"]),
                settlement_mass_total=float(value["settlement_mass_total"]),
                port_mass_total=float(value["port_mass_total"]),
                ruin_mass_total=float(value["ruin_mass_total"]),
            )
            for key, value in bucket_totals.items()
        }
        calibration_profiles = {
            name: CalibrationProfile(name=name, bins=self._calibration_bins_for_profile(profile_totals))
            for name, profile_totals in calibration_totals.items()
        }
        return FrontierPriorTable(
            rounds_included=[round_id],
            buckets=buckets,
            calibration_profiles=calibration_profiles,
        )

    def _calibration_bins_for_profile(
        self,
        profile_totals: dict[tuple[float, float], dict[str, float]],
    ) -> list[CalibrationBin]:
        bins: list[CalibrationBin] = []
        for (lower, upper), totals in sorted(profile_totals.items()):
            count = int(totals.get("count", 0.0))
            avg_confidence = float(totals["confidence"] / count) if count else 0.0
            accuracy = float(totals["correct"] / count) if count else 0.0
            if count == 0 or accuracy >= avg_confidence:
                recommended_temperature = 1.0
            else:
                recommended_temperature = min(2.5, max(1.0, avg_confidence / max(accuracy, 0.05)))
            bins.append(
                CalibrationBin(
                    lower=lower,
                    upper=upper,
                    accuracy=accuracy,
                    avg_confidence=avg_confidence,
                    count=count,
                    recommended_temperature=recommended_temperature,
                )
            )
        return bins

    def _aggregate_frontier_prior_tables(self) -> FrontierPriorTable:
        rounds_root = self.artifact_store.root / "rounds"
        aggregated_buckets: dict[str, dict[str, Any]] = defaultdict(self._new_frontier_bucket_total)
        calibration_totals: dict[str, dict[tuple[float, float], dict[str, float]]] = defaultdict(dict)
        rounds_included: list[str] = []

        if rounds_root.exists():
            for path in sorted(rounds_root.glob("*/analysis/frontier-priors.json")):
                table = FrontierPriorTable.model_validate(load_json_file(path))
                rounds_included.extend(round_id for round_id in table.rounds_included if round_id not in rounds_included)
                for key, bucket in table.buckets.items():
                    aggregate = aggregated_buckets[key]
                    aggregate["counts"] += np.array(bucket.counts, dtype=float)
                    aggregate["evidence"] += float(bucket.evidence)
                    aggregate["entropy_total"] += float(bucket.entropy_total)
                    aggregate["dynamic_mass_total"] += float(bucket.dynamic_mass_total)
                    aggregate["hotspot_total"] += float(bucket.hotspot_total)
                    aggregate["active_frontier_total"] += float(bucket.active_frontier_total)
                    aggregate["settlement_mass_total"] += float(bucket.settlement_mass_total)
                    aggregate["port_mass_total"] += float(bucket.port_mass_total)
                    aggregate["ruin_mass_total"] += float(bucket.ruin_mass_total)

                for profile_name, profile in table.calibration_profiles.items():
                    for calibration_bin in profile.bins:
                        key = (float(calibration_bin.lower), float(calibration_bin.upper))
                        aggregate = calibration_totals[profile_name].setdefault(
                            key,
                            {"count": 0.0, "correct": 0.0, "confidence": 0.0},
                        )
                        aggregate["count"] += float(calibration_bin.count)
                        aggregate["correct"] += float(calibration_bin.accuracy * calibration_bin.count)
                        aggregate["confidence"] += float(calibration_bin.avg_confidence * calibration_bin.count)

        return FrontierPriorTable(
            rounds_included=rounds_included,
            buckets={
                key: FrontierPriorBucket(
                    counts=value["counts"].tolist(),
                    evidence=float(value["evidence"]),
                    entropy_total=float(value["entropy_total"]),
                    dynamic_mass_total=float(value["dynamic_mass_total"]),
                    hotspot_total=float(value["hotspot_total"]),
                    active_frontier_total=float(value["active_frontier_total"]),
                    settlement_mass_total=float(value["settlement_mass_total"]),
                    port_mass_total=float(value["port_mass_total"]),
                    ruin_mass_total=float(value["ruin_mass_total"]),
                )
                for key, value in aggregated_buckets.items()
            },
            calibration_profiles={
                name: CalibrationProfile(name=name, bins=self._calibration_bins_for_profile(profile_totals))
                for name, profile_totals in calibration_totals.items()
            },
        )

    def _masked_mean(self, values: np.ndarray, mask: np.ndarray) -> float:
        if not np.any(mask):
            return 0.0
        return float(values[mask].mean())

    def _top_dynamic_error_cells(
        self,
        *,
        ground_truth: np.ndarray,
        prediction: np.ndarray,
        entropy_map: np.ndarray,
        mode: str,
        limit: int,
    ) -> list[str]:
        dynamic_diff = prediction[:, :, 1:4].sum(axis=-1) - ground_truth[:, :, 1:4].sum(axis=-1)
        ranking = np.argsort(dynamic_diff, axis=None)
        if mode == "false_positive":
            ranking = ranking[::-1]
        cells: list[str] = []
        for flat_index in ranking:
            y, x = np.unravel_index(flat_index, dynamic_diff.shape)
            diff = float(dynamic_diff[y, x])
            if mode == "false_positive" and diff <= 0.0:
                continue
            if mode == "false_negative" and diff >= 0.0:
                continue
            cells.append(
                f"({x},{y}) d={diff:+.3f} H={entropy_map[y, x]:.3f} P={prediction[y, x, 1:4].sum():.3f} T={ground_truth[y, x, 1:4].sum():.3f}"
            )
            if len(cells) == limit:
                break
        return cells or ["none"]

    def _top_class_gap_cells(
        self,
        *,
        ground_truth: np.ndarray,
        prediction: np.ndarray,
        entropy_map: np.ndarray,
        class_index: int,
        mode: str,
        seed_index: int,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        gap = prediction[:, :, class_index] - ground_truth[:, :, class_index]
        ranking = np.argsort(gap, axis=None)
        if mode == "false_positive":
            ranking = ranking[::-1]
        entries: list[dict[str, Any]] = []
        for flat_index in ranking:
            y, x = np.unravel_index(flat_index, gap.shape)
            value = float(gap[y, x])
            if mode == "false_positive" and value <= 0.0:
                continue
            if mode == "false_negative" and value >= 0.0:
                continue
            entries.append(
                {
                    "seed_index": seed_index,
                    "x": int(x),
                    "y": int(y),
                    "gap": value,
                    "entropy": float(entropy_map[y, x]),
                    "predicted": float(prediction[y, x, class_index]),
                    "truth": float(ground_truth[y, x, class_index]),
                }
            )
            if len(entries) == limit:
                break
        return entries

    def _truncate_cluster_list(self, items: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: abs(float(item["gap"])), reverse=True)[:limit]

    def _format_cluster_entry(self, item: dict[str, Any]) -> str:
        return (
            f"s{item['seed_index']}({item['x']},{item['y']}) "
            f"gap={float(item['gap']):+.3f} H={float(item['entropy']):.3f}"
        )
