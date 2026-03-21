from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from astarisland.application.frontier_model import (
    MODEL_VERSION as FITTED_MODEL_VERSION,
    build_seed_feature_frame,
    predict_seed_with_model_bundle,
)
from astarisland.application.heuristic_v4 import (
    HEURISTIC_MODEL_VERSION,
    HeuristicSeedDebug,
    merged_calibration,
    predict_seed_with_heuristic_v4,
)
from astarisland.domain.frontier import FrontierCellFeatures, FrontierFeatureCatalog, build_frontier_feature_catalog, build_window_dynamics_summaries
from astarisland.domain.models import FrontierPriorBucket, FrontierPriorTable, RecordedObservation, RoundDetail
from astarisland.domain.tensors import PredictionTensor, calibrate_prediction_tensor, renormalize_probabilities, soft_mask_for_cell
from astarisland.infrastructure.artifact_store import ArtifactStore


@dataclass(slots=True)
class _BucketPosterior:
    counts: np.ndarray
    evidence: float
    dynamic_mass_total: float

    def distribution(self, alpha: float = 0.35) -> np.ndarray:
        return (self.counts + alpha) / float(self.counts.sum() + alpha * len(self.counts))

    def dynamic_mass_mean(self) -> float:
        if self.evidence <= 0.0:
            return 0.0
        return self.dynamic_mass_total / self.evidence

    def dynamic_split(self, alpha: float = 0.15) -> np.ndarray:
        dynamic_counts = self.counts[1:4]
        return (dynamic_counts + alpha) / float(dynamic_counts.sum() + alpha * len(dynamic_counts))


class PredictionWorkflow:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    def build_predictions(self, round_detail: RoundDetail) -> list[PredictionTensor]:
        observations = self.artifact_store.load_observations(round_detail.id)
        window_summaries = self.artifact_store.load_window_dynamics_summaries(round_detail.id)
        if not window_summaries and observations:
            window_summaries = build_window_dynamics_summaries(round_detail.id, observations)
            self.artifact_store.save_window_dynamics_summaries(round_detail.id, window_summaries)

        historical_priors = self.artifact_store.load_aggregated_frontier_prior_table()
        model_bundle = self.artifact_store.load_frontier_model_bundle()
        model_metadata = self.artifact_store.load_frontier_model_metadata()
        heuristic_calibration = merged_calibration(self.artifact_store.load_heuristic_calibration())
        fitted_model_usable = self._should_use_fitted_model(model_bundle, model_metadata)
        if not fitted_model_usable:
            model_bundle = None

        predictions: list[PredictionTensor] = []
        markdown_lines = [
            f"# Prediction Summary for Round {round_detail.id}",
            "",
            f"- Observations loaded: {len(observations)}",
            f"- Window summaries loaded: {len(window_summaries)}",
            f"- Historical frontier priors: {'yes' if historical_priors is not None else 'no'}",
            f"- Fitted frontier model: {model_bundle.model_version if model_bundle is not None else 'no (heuristic fallback)'}",
            f"- Fitted model safety gate: {'pass' if fitted_model_usable else 'fallback to heuristic'}",
            f"- Production heuristic calibration: {heuristic_calibration['model_version']}",
            f"- Calibration rounds: {len(heuristic_calibration.get('generated_from_rounds', []))}",
            "",
        ]

        per_seed_observations = self._group_observations_by_seed(observations)
        for seed_index, initial_state in enumerate(round_detail.initial_states):
            seed_observations = per_seed_observations.get(seed_index, [])
            if model_bundle is not None:
                frame = build_seed_feature_frame(
                    initial_state,
                    seed_observations,
                    window_summaries,
                    historical_priors,
                )
                values, debug = predict_seed_with_model_bundle(
                    model_bundle,
                    frame,
                    width=round_detail.map_width,
                    height=round_detail.map_height,
                )
                tensor = calibrate_prediction_tensor(PredictionTensor(seed_index=seed_index, values=values))
                summary_lines = self._prediction_summary_lines_fitted(
                    seed_index=seed_index,
                    tensor=tensor,
                    frame=frame,
                    debug=debug,
                )
            else:
                frontier_catalog = build_frontier_feature_catalog(
                    initial_state,
                    observations=seed_observations,
                    window_summaries=window_summaries,
                    historical_priors=historical_priors,
                )
                same_cell_counts = self._build_same_cell_counts(
                    width=round_detail.map_width,
                    height=round_detail.map_height,
                    observations=seed_observations,
                )
                values, debug = predict_seed_with_heuristic_v4(
                    frontier_catalog=frontier_catalog,
                    same_cell_counts=same_cell_counts,
                    historical_priors=historical_priors,
                    calibration=heuristic_calibration,
                )
                tensor = calibrate_prediction_tensor(PredictionTensor(seed_index=seed_index, values=values))
                summary_lines = self._prediction_summary_lines_heuristic_v4(
                    seed_index=seed_index,
                    tensor=tensor,
                    catalog=frontier_catalog,
                    debug=debug,
                )

            self.artifact_store.save_prediction_tensor(round_detail.id, tensor)
            predictions.append(tensor)
            markdown_lines.extend(summary_lines)
            markdown_lines.append("")

        self.artifact_store.save_markdown_summary(round_detail.id, "prediction-summary", "\n".join(markdown_lines).strip())
        return predictions

    def _should_use_fitted_model(self, model_bundle, model_metadata) -> bool:
        if model_bundle is None or model_metadata is None:
            return False
        if not model_metadata.replay_diagnostics:
            return False
        replay_scores = [diagnostics.average_score for diagnostics in model_metadata.replay_diagnostics]
        return min(replay_scores) >= 30.0

    def _group_observations_by_seed(self, observations: list[RecordedObservation]) -> dict[int, list[RecordedObservation]]:
        grouped: dict[int, list[RecordedObservation]] = defaultdict(list)
        for record in observations:
            grouped[record.seed_index].append(record)
        return grouped

    def _build_same_cell_counts(self, width: int, height: int, observations: list[RecordedObservation]) -> np.ndarray:
        counts = np.zeros((height, width, 6), dtype=float)
        for record in observations:
            viewport = record.request.to_viewport()
            for offset_y, row in enumerate(record.result.grid):
                for offset_x, value in enumerate(row):
                    x = viewport.x + offset_x
                    y = viewport.y + offset_y
                    counts[y, x, self._terrain_to_class_index(value)] += 1.0
        return counts

    def _build_current_round_bucket_posteriors(
        self,
        round_detail: RoundDetail,
        observations: list[RecordedObservation],
        window_summaries,
        historical_priors: FrontierPriorTable | None,
    ) -> dict[str, _BucketPosterior]:
        per_seed_observations = self._group_observations_by_seed(observations)
        frontier_catalogs: dict[int, FrontierFeatureCatalog] = {
            seed_index: build_frontier_feature_catalog(
                initial_state,
                observations=per_seed_observations.get(seed_index, []),
                window_summaries=window_summaries,
                historical_priors=historical_priors,
            )
            for seed_index, initial_state in enumerate(round_detail.initial_states)
        }

        buckets: dict[str, _BucketPosterior] = {}
        for record in observations:
            catalog = frontier_catalogs[record.seed_index]
            viewport = record.request.to_viewport()
            for offset_y, row in enumerate(record.result.grid):
                for offset_x, value in enumerate(row):
                    x = viewport.x + offset_x
                    y = viewport.y + offset_y
                    bucket_key = catalog.feature_for(x, y).bucket_key()
                    posterior = buckets.setdefault(
                        bucket_key,
                        _BucketPosterior(
                            counts=np.zeros(6, dtype=float),
                            evidence=0.0,
                            dynamic_mass_total=0.0,
                        ),
                    )
                    class_index = self._terrain_to_class_index(value)
                    posterior.counts[class_index] += 1.0
                    posterior.evidence += 1.0
                    posterior.dynamic_mass_total += float(class_index in {1, 2, 3})
        return buckets

    def _predict_cell(
        self,
        features: FrontierCellFeatures,
        cell_counts: np.ndarray,
        bucket_prior: _BucketPosterior | None,
        historical_bucket: FrontierPriorBucket | None,
    ) -> np.ndarray:
        observed_count = int(cell_counts.sum())
        structural = self._structural_prior(features)
        observed_distribution = self._smoothed_distribution(cell_counts, alpha=0.15) if observed_count > 0 else None
        bucket_distribution = bucket_prior.distribution(alpha=0.45) if bucket_prior is not None else None
        historical_distribution = (
            self._normalize(np.array(historical_bucket.mean_distribution, dtype=float))
            if historical_bucket is not None
            else None
        )

        dynamic_mass = self._predict_dynamic_mass(
            features,
            structural=structural,
            observed_distribution=observed_distribution,
            bucket_prior=bucket_prior,
            historical_bucket=historical_bucket,
        )
        dynamic_split = self._predict_dynamic_split(
            features,
            structural=structural,
            observed_distribution=observed_distribution,
            bucket_prior=bucket_prior,
            historical_bucket=historical_bucket,
        )
        static_split = self._predict_static_split(
            features,
            structural=structural,
            observed_distribution=observed_distribution,
            bucket_distribution=bucket_distribution,
            historical_distribution=historical_distribution,
        )

        probabilities = np.zeros(6, dtype=float)
        probabilities[1:4] = dynamic_mass * dynamic_split
        probabilities[[0, 4, 5]] = (1.0 - dynamic_mass) * static_split

        mask = soft_mask_for_cell(features.base.initial_terrain, features.base.coastal, features.base.initial_port)
        masked = self._normalize(probabilities * np.sqrt(mask))
        probabilities = self._normalize(0.70 * probabilities + 0.30 * masked)
        probabilities = self._apply_conservative_caps(probabilities, features)

        temperature = self._temperature_for_cell(features)
        if temperature > 1.0:
            probabilities = self._temperature_scale(probabilities, temperature)
        return self._normalize(probabilities)

    def _predict_dynamic_mass(
        self,
        features: FrontierCellFeatures,
        *,
        structural: np.ndarray,
        observed_distribution: np.ndarray | None,
        bucket_prior: _BucketPosterior | None,
        historical_bucket: FrontierPriorBucket | None,
    ) -> float:
        pressure_mass = self._pressure_dynamic_mass(features)
        candidate_values = [
            (float(structural[1:4].sum()), 0.18),
            (pressure_mass, 0.38 if features.frontier_eligible else 0.12),
        ]
        if observed_distribution is not None:
            candidate_values.append((float(observed_distribution[1:4].sum()), min(0.50, 0.14 * max(features.observed_count, 1))))
        if bucket_prior is not None:
            candidate_values.append((bucket_prior.dynamic_mass_mean(), min(0.18, 0.02 * bucket_prior.evidence)))
        if historical_bucket is not None:
            candidate_values.append((historical_bucket.mean_dynamic_mass, 0.16))

        dynamic_mass = self._weighted_scalar_average(candidate_values)
        if features.frontier_eligible and features.base.buildable:
            dynamic_mass = max(dynamic_mass, 0.18 + 0.18 * pressure_mass)
        if features.base.coastal:
            dynamic_mass = max(dynamic_mass, 0.06 + 0.05 * features.port_pressure)
        return float(np.clip(dynamic_mass, 0.02, 0.88))

    def _predict_dynamic_split(
        self,
        features: FrontierCellFeatures,
        *,
        structural: np.ndarray,
        observed_distribution: np.ndarray | None,
        bucket_prior: _BucketPosterior | None,
        historical_bucket: FrontierPriorBucket | None,
    ) -> np.ndarray:
        settlement_signal = (
            0.25
            + 1.20 * features.expansion_pressure
            + 0.55 * features.observed_settlement_rate
            + 0.35 * features.reclaim_pressure
            + 0.18 * features.nearby_population_mean
            + 0.14 * features.nearby_food_mean
            + 0.12 * float(features.base.settlement_distance_bucket in {"0", "1-2"})
        )
        port_signal = (
            0.08
            + 1.30 * features.port_pressure
            + 0.80 * features.observed_port_rate
            + 0.26 * float(features.base.coastal)
            + 0.20 * float(features.base.initial_port)
            + 0.18 * features.nearby_wealth_mean
        )
        ruin_signal = (
            0.12
            + 1.10 * features.collapse_pressure
            + 0.65 * features.observed_ruin_rate
            + 0.35 * float(features.base.initial_terrain == 3)
            + 0.16 * features.hotspot_score
        )

        pressure_split = self._normalize(np.array([settlement_signal, port_signal, ruin_signal], dtype=float))
        candidate_distributions: list[tuple[np.ndarray, float]] = [
            (pressure_split, 0.46 if features.frontier_eligible else 0.18),
            (self._normalize(structural[1:4]), 0.10),
        ]
        if observed_distribution is not None and float(observed_distribution[1:4].sum()) > 0.01:
            candidate_distributions.append(
                (
                    self._normalize(observed_distribution[1:4]),
                    min(0.42, 0.14 * max(features.observed_count, 1) + 0.06 * features.repeated_count),
                )
            )
        if bucket_prior is not None and bucket_prior.dynamic_mass_mean() > 0.01:
            candidate_distributions.append((bucket_prior.dynamic_split(), min(0.16, 0.018 * bucket_prior.evidence)))
        if historical_bucket is not None and historical_bucket.mean_dynamic_mass > 0.01:
            candidate_distributions.append(
                (
                    self._normalize(np.array(historical_bucket.mean_distribution[1:4], dtype=float)),
                    0.18,
                )
            )
        return self._blend_distributions(candidate_distributions)

    def _predict_static_split(
        self,
        features: FrontierCellFeatures,
        *,
        structural: np.ndarray,
        observed_distribution: np.ndarray | None,
        bucket_distribution: np.ndarray | None,
        historical_distribution: np.ndarray | None,
    ) -> np.ndarray:
        candidate_distributions: list[tuple[np.ndarray, float]] = [
            (self._normalize(structural[[0, 4, 5]]), 0.48),
        ]
        if observed_distribution is not None:
            candidate_distributions.append(
                (
                    self._normalize(observed_distribution[[0, 4, 5]]),
                    min(0.30, 0.10 * max(features.observed_count, 1)),
                )
            )
        if bucket_distribution is not None:
            candidate_distributions.append((self._normalize(bucket_distribution[[0, 4, 5]]), 0.12))
        if historical_distribution is not None:
            candidate_distributions.append((self._normalize(historical_distribution[[0, 4, 5]]), 0.10))

        if features.frontier_eligible and features.base.buildable:
            frontier_static = np.array([0.72, 0.24, 0.04], dtype=float)
            candidate_distributions.append((frontier_static, 0.16))
        return self._blend_distributions(candidate_distributions)

    def _structural_prior(self, features: FrontierCellFeatures) -> np.ndarray:
        terrain = features.base.initial_terrain
        if terrain == 5:
            prior = np.array([0.01, 0.01, 0.01, 0.01, 0.01, 0.95], dtype=float)
        elif terrain == 10:
            prior = np.array([0.95, 0.01, 0.01, 0.005, 0.005, 0.02], dtype=float)
        elif terrain == 4:
            prior = np.array([0.25, 0.12, 0.02, 0.07, 0.52, 0.02], dtype=float)
        elif terrain == 3:
            prior = np.array([0.18, 0.20, 0.05, 0.42, 0.12, 0.03], dtype=float)
        elif terrain == 2 or features.base.initial_port:
            prior = np.array([0.08, 0.18, 0.46, 0.14, 0.10, 0.04], dtype=float)
        elif terrain == 1:
            prior = np.array([0.10, 0.42, 0.12, 0.18, 0.14, 0.04], dtype=float)
        else:
            prior = np.array([0.78, 0.09, 0.03, 0.04, 0.04, 0.02], dtype=float)
        if features.frontier_eligible and features.base.buildable:
            prior = prior + np.array([-0.08, 0.04, 0.02, 0.02, 0.00, 0.00], dtype=float)
        if features.base.coastal:
            prior = prior + np.array([-0.03, 0.01, 0.05, 0.00, -0.01, -0.02], dtype=float)
        return self._normalize(np.clip(prior, 1e-6, None))

    def _pressure_dynamic_mass(self, features: FrontierCellFeatures) -> float:
        raw_pressure = (
            0.62 * features.expansion_pressure
            + 0.48 * features.port_pressure
            + 0.44 * features.collapse_pressure
            + 0.30 * features.reclaim_pressure
            + 0.26 * features.observed_dynamic_rate
            + 0.18 * features.hotspot_score
            + 0.18 * features.frontier_uncertainty
            + 0.12 * features.historical_dynamic_mass
        )
        normalized_pressure = raw_pressure / (1.0 + raw_pressure)
        base = 0.03 + 0.08 * float(features.frontier_eligible) + 0.05 * float(features.base.buildable)
        base += 0.04 * float(features.base.coastal)
        return float(np.clip(base + 0.68 * normalized_pressure, 0.02, 0.82))

    def _apply_conservative_caps(self, probabilities: np.ndarray, features: FrontierCellFeatures) -> np.ndarray:
        adjusted = probabilities.astype(float, copy=True)
        if features.frontier_eligible:
            if features.repeated_count > 0:
                empty_cap = 0.55
            elif features.observed_count > 0:
                empty_cap = 0.60
            else:
                empty_cap = 0.65
            adjusted = self._cap_class_probability(adjusted, class_index=0, cap=empty_cap, recipients=[1, 2, 3, 4])
            if features.base.buildable:
                adjusted = self._cap_class_probability(adjusted, class_index=4, cap=0.45, recipients=[0, 1, 2, 3])
        return self._normalize(adjusted)

    def _cap_class_probability(
        self,
        probabilities: np.ndarray,
        *,
        class_index: int,
        cap: float,
        recipients: list[int],
    ) -> np.ndarray:
        adjusted = probabilities.astype(float, copy=True)
        excess = max(0.0, float(adjusted[class_index] - cap))
        if excess <= 0.0:
            return adjusted
        adjusted[class_index] = cap
        recipient_mass = float(adjusted[recipients].sum())
        if recipient_mass <= 0.0:
            adjusted[recipients] += excess / len(recipients)
            return adjusted
        adjusted[recipients] += excess * adjusted[recipients] / recipient_mass
        return adjusted

    def _temperature_for_cell(self, features: FrontierCellFeatures) -> float:
        if not features.frontier_eligible:
            return 1.0
        sparse = 1.0 if features.observed_count == 0 else 0.55 if features.observed_count == 1 else 0.20
        uncertainty = min(features.frontier_uncertainty + 0.5 * features.historical_entropy_prior, 1.8)
        temperature = 1.0 + 0.35 * sparse + 0.45 * uncertainty + 0.12 * features.historical_hotspot_prior
        if features.repeated_count > 0:
            temperature += 0.10
        return float(min(max(temperature, 1.0), 2.4))

    def _temperature_scale(self, probabilities: np.ndarray, temperature: float) -> np.ndarray:
        scaled = np.power(np.clip(probabilities, 1e-12, None), 1.0 / temperature)
        return self._normalize(scaled)

    def _blend_distributions(self, candidates: list[tuple[np.ndarray, float]]) -> np.ndarray:
        blended = np.zeros_like(candidates[0][0], dtype=float)
        total_weight = 0.0
        for distribution, weight in candidates:
            if weight <= 0.0:
                continue
            blended += self._normalize(distribution) * weight
            total_weight += weight
        if total_weight <= 0.0:
            return self._normalize(np.ones_like(blended))
        return self._normalize(blended / total_weight)

    def _weighted_scalar_average(self, values: list[tuple[float, float]]) -> float:
        total = 0.0
        total_weight = 0.0
        for value, weight in values:
            if weight <= 0.0:
                continue
            total += value * weight
            total_weight += weight
        if total_weight <= 0.0:
            return 0.0
        return total / total_weight

    def _normalize(self, values: np.ndarray) -> np.ndarray:
        normalized = renormalize_probabilities(np.atleast_2d(values))[0]
        return normalized

    def _terrain_to_class_index(self, terrain: int) -> int:
        if terrain in {0, 10, 11}:
            return 0
        return int(terrain)

    def _smoothed_distribution(self, counts: np.ndarray, alpha: float) -> np.ndarray:
        return (counts + alpha) / float(counts.sum() + alpha * len(counts))

    def _prediction_summary_lines_heuristic_v4(
        self,
        seed_index: int,
        tensor: PredictionTensor,
        catalog: FrontierFeatureCatalog,
        debug: HeuristicSeedDebug,
    ) -> list[str]:
        class_marginals = tensor.values.mean(axis=(0, 1))
        dynamic_mass = float(tensor.values[:, :, 1:4].sum(axis=-1).mean())
        frontier_cells = [feature for feature in catalog.per_cell.values() if feature.frontier_eligible]
        frontier_total = len(frontier_cells)
        frontier_observed = sum(1 for feature in frontier_cells if feature.observed_count > 0)
        top_risk = sorted(
            frontier_cells,
            key=lambda feature: (
                feature.frontier_uncertainty + 0.45 * feature.historical_hotspot_prior + 0.12 * (feature.observed_count == 0)
            ),
            reverse=True,
        )[:5]
        risk_summary = ", ".join(
            f"({feature.x},{feature.y}) u={feature.frontier_uncertainty:.2f} obs={feature.observed_count}"
            for feature in top_risk
        )
        return [
            f"## Seed {seed_index}",
            "",
            f"- Predictor: {HEURISTIC_MODEL_VERSION}",
            f"- Frontier coverage: {frontier_observed}/{frontier_total}",
            f"- Mean dynamic mass: {dynamic_mass:.3f}",
            f"- Seed target dynamic mass vs actual: {debug.target_dynamic_mass:.3f} vs {debug.mean_dynamic_after_correction:.3f}",
            f"- Seed target Settlement mass vs actual: {debug.target_settlement_mass:.3f} vs {class_marginals[1]:.3f}",
            f"- Seed target Port mass vs actual: {debug.target_port_mass:.3f} vs {class_marginals[2]:.3f}",
            f"- Seed target Ruin mass vs actual: {debug.target_ruin_mass:.3f} vs {class_marginals[3]:.3f}",
            f"- Mean dynamic mass before calibration: {debug.mean_dynamic_before_correction:.3f}",
            "- Class marginals: "
            + ", ".join(
                f"{label}={value:.3f}"
                for label, value in zip(("Empty", "Settlement", "Port", "Ruin", "Forest", "Mountain"), class_marginals, strict=True)
            ),
            "- Regime fractions: "
            + ", ".join(
                f"{regime}={fraction:.3f}"
                for regime, fraction in debug.regime_fractions.items()
            ),
            f"- High-risk frontier cells: {risk_summary if risk_summary else 'none'}",
            f"- Top suppressed false-positive maritime/collapse cells: {', '.join(debug.suppressed_cells) if debug.suppressed_cells else 'none'}",
        ]

    def _prediction_summary_lines_fitted(
        self,
        *,
        seed_index: int,
        tensor: PredictionTensor,
        frame,
        debug: dict[str, float],
    ) -> list[str]:
        class_marginals = tensor.values.mean(axis=(0, 1))
        dynamic_mass = float(tensor.values[:, :, 1:4].sum(axis=-1).mean())
        frontier_cells = [feature for feature in frame.frontier_catalog.per_cell.values() if feature.frontier_eligible]
        frontier_total = len(frontier_cells)
        frontier_observed = sum(1 for feature in frontier_cells if feature.observed_count > 0)
        hard_negative_total = int(np.count_nonzero(frame.hard_negative_mask))
        top_risk = sorted(
            frontier_cells,
            key=lambda feature: (
                feature.frontier_uncertainty
                + 0.55 * feature.historical_active_frontier_rate
                + 0.20 * feature.observed_dynamic_rate
                + 0.10 * (feature.observed_count == 0)
            ),
            reverse=True,
        )[:5]
        risk_summary = ", ".join(
            f"({feature.x},{feature.y}) u={feature.frontier_uncertainty:.2f} dyn={feature.observed_dynamic_rate:.2f}"
            for feature in top_risk
        )
        return [
            f"## Seed {seed_index}",
            "",
            f"- Predictor: {FITTED_MODEL_VERSION}",
            f"- Frontier coverage: {frontier_observed}/{frontier_total}",
            f"- Hard-negative candidates: {hard_negative_total}",
            f"- Mean dynamic mass: {dynamic_mass:.3f}",
            f"- Dynamic target / before / after correction: {debug['target_dynamic_mass']:.3f} / {debug['mean_dynamic_before_correction']:.3f} / {debug['mean_dynamic_after_correction']:.3f}",
            "- Class marginals: "
            + ", ".join(
                f"{label}={value:.3f}"
                for label, value in zip(("Empty", "Settlement", "Port", "Ruin", "Forest", "Mountain"), class_marginals, strict=True)
            ),
            f"- High-risk frontier cells: {risk_summary if risk_summary else 'none'}",
        ]
