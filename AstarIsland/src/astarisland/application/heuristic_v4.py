from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from astarisland.domain.frontier import FrontierCellFeatures, FrontierFeatureCatalog, build_frontier_feature_catalog
from astarisland.domain.models import AnalysisResult, FrontierPriorBucket, FrontierPriorTable, RecordedObservation
from astarisland.domain.scoring import entropy_weighted_kl, kl_divergence, seed_score
from astarisland.domain.tensors import renormalize_probabilities, soft_mask_for_cell

HEURISTIC_MODEL_VERSION = "v4-regime-calibrated-heuristic"
REGIMES = (
    "stable_negative",
    "growth_frontier",
    "collapse_frontier",
    "maritime_frontier",
)


@dataclass(frozen=True, slots=True)
class HeuristicSeedDebug:
    target_dynamic_mass: float
    target_settlement_mass: float
    target_port_mass: float
    target_ruin_mass: float
    mean_dynamic_before_correction: float
    mean_dynamic_after_correction: float
    regime_fractions: dict[str, float]
    suppressed_cells: list[str]
    calibration_version: str


def default_heuristic_calibration() -> dict[str, Any]:
    return {
        "model_version": HEURISTIC_MODEL_VERSION,
        "generated_from_rounds": [],
        "coefficients": {
            "dynamic_target_scale": 0.82,
            "settlement_uplift_scale": 1.24,
            "port_suppression_scale": 0.34,
            "ruin_suppression_scale": 0.40,
            "stable_negative_threshold": 0.58,
            "growth_frontier_threshold": 0.46,
            "collapse_frontier_threshold": 0.35,
            "maritime_frontier_threshold": 0.40,
            "positive_frontier_weight": 1.0,
            "settlement_false_negative_weight": 1.1,
            "collapse_frontier_weight": 1.05,
            "coastal_port_weight": 0.78,
        },
    }


def merged_calibration(payload: dict[str, Any] | None) -> dict[str, Any]:
    merged = default_heuristic_calibration()
    if payload is None:
        return merged
    merged["generated_from_rounds"] = list(payload.get("generated_from_rounds", merged["generated_from_rounds"]))
    coefficients = dict(merged["coefficients"])
    coefficients.update(payload.get("coefficients", {}))
    merged["coefficients"] = coefficients
    merged["model_version"] = str(payload.get("model_version", merged["model_version"]))
    return merged


def _apply_observed_settlement_floor(
    probabilities: np.ndarray,
    same_cell_counts: np.ndarray,
) -> np.ndarray:
    """Protect cells where we directly observed high settlement rates from global rescaling."""
    adjusted = probabilities.astype(float, copy=True)
    height, width, _ = same_cell_counts.shape
    for y in range(height):
        for x in range(width):
            counts = same_cell_counts[y, x]
            total = float(counts.sum())
            if total < 1.0:
                continue
            obs_settlement_rate = float(counts[1]) / total
            if obs_settlement_rate < 0.10:
                continue
            floor = max(0.15, obs_settlement_rate * 0.70)
            if adjusted[y, x, 1] >= floor:
                continue
            deficit = floor - adjusted[y, x, 1]
            adjusted[y, x, 1] = floor
            # Transfer deficit from Empty (0) and Forest (4) proportionally
            static_mass = float(adjusted[y, x, 0] + adjusted[y, x, 4])
            if static_mass > deficit + 0.02:
                frac0 = adjusted[y, x, 0] / static_mass
                frac4 = adjusted[y, x, 4] / static_mass
                adjusted[y, x, 0] = max(0.01, adjusted[y, x, 0] - deficit * frac0)
                adjusted[y, x, 4] = max(0.01, adjusted[y, x, 4] - deficit * frac4)
    return _normalize(adjusted.reshape(-1, 6)).reshape(adjusted.shape)


def predict_seed_with_heuristic_v4(
    *,
    frontier_catalog: FrontierFeatureCatalog,
    same_cell_counts: np.ndarray,
    historical_priors: FrontierPriorTable | None,
    calibration: dict[str, Any] | None,
) -> tuple[np.ndarray, HeuristicSeedDebug]:
    config = merged_calibration(calibration)
    coefficients = config["coefficients"]
    height, width, _ = same_cell_counts.shape
    probabilities = np.zeros((height, width, 6), dtype=float)
    dynamic_before: list[float] = []
    suppression_records: list[tuple[float, str]] = []
    regime_counts: dict[str, int] = {regime: 0 for regime in REGIMES}

    per_cell_context: dict[tuple[int, int], dict[str, float | str | bool | np.ndarray]] = {}
    historical_distribution_cache: dict[str, np.ndarray] = {}

    for y in range(height):
        for x in range(width):
            feature = frontier_catalog.feature_for(x, y)
            bucket = historical_priors.buckets.get(feature.bucket_key()) if historical_priors is not None else None
            observed_distribution = _observed_distribution(same_cell_counts[y, x])
            structural = _structural_prior(feature)
            historical_distribution = _historical_distribution(
                feature=feature,
                bucket=bucket,
                cache=historical_distribution_cache,
            )
            repeated_empty_stability = _repeated_empty_stability(feature)
            low_entropy_prior = _low_entropy_prior(feature)
            regime_scores = _regime_scores(feature, coefficients, repeated_empty_stability, low_entropy_prior)
            regime = _select_regime(feature, regime_scores, coefficients)
            regime_counts[regime] += 1

            proxy_distribution = _proxy_distribution(
                feature=feature,
                observed_distribution=observed_distribution,
                structural=structural,
                historical_distribution=historical_distribution,
            )
            maritime_supported = _maritime_supported(feature)
            hard_negative = _is_hard_negative(feature, repeated_empty_stability, low_entropy_prior)
            dynamic_mass = _regime_dynamic_mass(
                feature=feature,
                regime=regime,
                proxy_distribution=proxy_distribution,
                regime_scores=regime_scores,
                coefficients=coefficients,
                hard_negative=hard_negative,
                maritime_supported=maritime_supported,
            )
            dynamic_split = _dynamic_split(
                feature=feature,
                regime=regime,
                observed_distribution=observed_distribution,
                proxy_distribution=proxy_distribution,
                coefficients=coefficients,
                maritime_supported=maritime_supported,
                collapse_advantage=float(regime_scores["collapse_frontier"] - regime_scores["growth_frontier"]),
            )
            static_split = _static_split(
                feature=feature,
                observed_distribution=observed_distribution,
                structural=structural,
                historical_distribution=historical_distribution,
            )
            cell_probabilities = np.zeros(6, dtype=float)
            cell_probabilities[1:4] = dynamic_mass * dynamic_split
            cell_probabilities[[0, 4, 5]] = (1.0 - dynamic_mass) * static_split
            cell_probabilities = _apply_structural_mask(cell_probabilities, feature)
            dynamic_before.append(float(cell_probabilities[1:4].sum()))
            cell_probabilities, suppression_note = _apply_local_caps(
                cell_probabilities,
                feature=feature,
                regime=regime,
                maritime_supported=maritime_supported,
                hard_negative=hard_negative,
                repeated_empty_stability=repeated_empty_stability,
            )
            if suppression_note is not None:
                suppression_records.append(suppression_note)
            probabilities[y, x] = cell_probabilities
            per_cell_context[(x, y)] = {
                "regime": regime,
                "hard_negative": hard_negative,
                "maritime_supported": maritime_supported,
                "repeated_empty_stability": repeated_empty_stability,
            }

    targets = _estimate_seed_targets(
        frontier_catalog=frontier_catalog,
        same_cell_counts=same_cell_counts,
        historical_priors=historical_priors,
        coefficients=coefficients,
    )
    probabilities = _align_dynamic_class_targets(probabilities, targets)
    probabilities = _rescale_seed_dynamic_mass(probabilities, _scale_for_target(probabilities, targets["dynamic"]))
    probabilities = _apply_observed_settlement_floor(probabilities, same_cell_counts)

    for y in range(height):
        for x in range(width):
            feature = frontier_catalog.feature_for(x, y)
            context = per_cell_context[(x, y)]
            probabilities[y, x], suppression_note = _apply_local_caps(
                probabilities[y, x],
                feature=feature,
                regime=str(context["regime"]),
                maritime_supported=bool(context["maritime_supported"]),
                hard_negative=bool(context["hard_negative"]),
                repeated_empty_stability=float(context["repeated_empty_stability"]),
            )
            if suppression_note is not None:
                suppression_records.append(suppression_note)

    regime_fractions = {
        regime: count / float(height * width)
        for regime, count in regime_counts.items()
    }
    debug = HeuristicSeedDebug(
        target_dynamic_mass=targets["dynamic"],
        target_settlement_mass=targets["settlement"],
        target_port_mass=targets["port"],
        target_ruin_mass=targets["ruin"],
        mean_dynamic_before_correction=float(np.mean(dynamic_before) if dynamic_before else 0.0),
        mean_dynamic_after_correction=float(probabilities[:, :, 1:4].sum(axis=-1).mean()),
        regime_fractions=regime_fractions,
        suppressed_cells=[note for _, note in sorted(suppression_records, reverse=True)[:5]],
        calibration_version=str(config["model_version"]),
    )
    return probabilities, debug


def fit_heuristic_calibration(
    artifact_store,
    historical_priors: FrontierPriorTable | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base = merged_calibration(None)
    rounds = _archived_round_ids(artifact_store)
    if not rounds:
        backtest = {
            "model_version": HEURISTIC_MODEL_VERSION,
            "rounds": [],
            "average_score": 0.0,
        }
        return base, backtest

    default_backtests = [
        replay_heuristic_round(
            artifact_store=artifact_store,
            round_id=round_id,
            calibration=base,
            historical_priors=historical_priors,
        )
        for round_id in rounds
    ]
    valid_backtests = [item for item in default_backtests if item is not None]
    if not valid_backtests:
        return base, {"model_version": HEURISTIC_MODEL_VERSION, "rounds": [], "average_score": 0.0}

    aggregate = _aggregate_replay_metrics(valid_backtests)
    coefficients = dict(base["coefficients"])
    coefficients["dynamic_target_scale"] = float(
        np.clip(aggregate["truth_dynamic_mass"] / max(aggregate["pred_dynamic_mass"], 1e-6), 0.45, 0.92)
    )
    coefficients["settlement_uplift_scale"] = float(
        np.clip(1.10 * aggregate["truth_high_entropy_settlement"] / max(aggregate["pred_high_entropy_settlement"], 1e-6), 1.0, 1.8)
    )
    coefficients["port_suppression_scale"] = float(
        np.clip(aggregate["truth_high_entropy_port"] / max(aggregate["pred_high_entropy_port"], 1e-6), 0.10, 0.60)
    )
    coefficients["ruin_suppression_scale"] = float(
        np.clip(aggregate["truth_high_entropy_ruin"] / max(aggregate["pred_high_entropy_ruin"], 1e-6), 0.10, 0.50)
    )
    coefficients["stable_negative_threshold"] = float(np.clip(0.56 + 0.18 * (1.0 - coefficients["dynamic_target_scale"]), 0.55, 0.76))
    coefficients["growth_frontier_threshold"] = float(np.clip(0.54 + 0.06 * max(0.0, 1.20 - coefficients["settlement_uplift_scale"]), 0.50, 0.66))
    coefficients["collapse_frontier_threshold"] = float(np.clip(0.56 + 0.10 * (1.0 - coefficients["ruin_suppression_scale"]), 0.54, 0.74))
    coefficients["maritime_frontier_threshold"] = float(np.clip(0.60 + 0.12 * (1.0 - coefficients["port_suppression_scale"]), 0.58, 0.78))

    fitted = {
        "model_version": HEURISTIC_MODEL_VERSION,
        "generated_from_rounds": rounds,
        "coefficients": coefficients,
        "aggregate_metrics": aggregate,
    }
    fitted_backtests = [
        replay_heuristic_round(
            artifact_store=artifact_store,
            round_id=round_id,
            calibration=fitted,
            historical_priors=historical_priors,
        )
        for round_id in rounds
    ]
    valid_fitted_backtests = [item for item in fitted_backtests if item is not None]
    backtest_summary = {
        "model_version": HEURISTIC_MODEL_VERSION,
        "rounds": valid_fitted_backtests,
        "average_score": float(np.mean([item["average_score"] for item in valid_fitted_backtests])) if valid_fitted_backtests else 0.0,
        "aggregate_metrics": _aggregate_replay_metrics(valid_fitted_backtests) if valid_fitted_backtests else {},
    }
    return fitted, backtest_summary


def replay_heuristic_round(
    *,
    artifact_store,
    round_id: str,
    calibration: dict[str, Any] | None,
    historical_priors: FrontierPriorTable | None = None,
) -> dict[str, Any] | None:
    analysis_results = artifact_store.load_analysis_results(round_id)
    if not analysis_results:
        return None
    observations = artifact_store.load_observations(round_id)
    if not observations:
        return None
    round_detail = artifact_store.load_round_detail(round_id)
    window_summaries = artifact_store.load_window_dynamics_summaries(round_id)
    per_seed_observations: dict[int, list[RecordedObservation]] = {}
    for record in observations:
        per_seed_observations.setdefault(record.seed_index, []).append(record)

    priors = historical_priors or artifact_store.load_aggregated_frontier_prior_table()
    seed_scores: list[float] = []
    dynamic_truth: list[float] = []
    dynamic_pred: list[float] = []
    high_entropy_settlement_truth: list[float] = []
    high_entropy_settlement_pred: list[float] = []
    high_entropy_port_truth: list[float] = []
    high_entropy_port_pred: list[float] = []
    high_entropy_ruin_truth: list[float] = []
    high_entropy_ruin_pred: list[float] = []
    covered_dynamic_kl: list[float] = []
    uncovered_dynamic_kl: list[float] = []

    for seed_index, initial_state in enumerate(round_detail.initial_states):
        analysis = analysis_results.get(seed_index)
        if analysis is None:
            continue
        seed_observations = per_seed_observations.get(seed_index, [])
        frontier_catalog = build_frontier_feature_catalog(
            initial_state,
            observations=seed_observations,
            window_summaries=window_summaries,
            historical_priors=priors,
        )
        same_cell_counts = _build_same_cell_counts(
            width=round_detail.map_width,
            height=round_detail.map_height,
            observations=seed_observations,
        )
        predicted, _ = predict_seed_with_heuristic_v4(
            frontier_catalog=frontier_catalog,
            same_cell_counts=same_cell_counts,
            historical_priors=priors,
            calibration=calibration,
        )
        ground_truth = np.array(analysis.ground_truth, dtype=float)
        seed_scores.append(seed_score(ground_truth, predicted))
        truth_dynamic = ground_truth[:, :, 1:4].sum(axis=-1)
        pred_dynamic = predicted[:, :, 1:4].sum(axis=-1)
        dynamic_truth.append(float(truth_dynamic.mean()))
        dynamic_pred.append(float(pred_dynamic.mean()))
        entropy = _entropy_map(ground_truth)
        high_entropy_mask = entropy >= 0.50
        if int(high_entropy_mask.sum()) > 0:
            high_entropy_settlement_truth.append(float(ground_truth[:, :, 1][high_entropy_mask].mean()))
            high_entropy_settlement_pred.append(float(predicted[:, :, 1][high_entropy_mask].mean()))
            high_entropy_port_truth.append(float(ground_truth[:, :, 2][high_entropy_mask].mean()))
            high_entropy_port_pred.append(float(predicted[:, :, 2][high_entropy_mask].mean()))
            high_entropy_ruin_truth.append(float(ground_truth[:, :, 3][high_entropy_mask].mean()))
            high_entropy_ruin_pred.append(float(predicted[:, :, 3][high_entropy_mask].mean()))
        coverage_mask = _coverage_mask(round_detail.map_height, round_detail.map_width, seed_observations)
        dynamic_mask = (truth_dynamic >= 0.20) | (entropy >= 0.55)
        covered_dynamic_kl.append(_subset_weighted_kl(ground_truth, predicted, dynamic_mask & coverage_mask))
        uncovered_dynamic_kl.append(_subset_weighted_kl(ground_truth, predicted, dynamic_mask & ~coverage_mask))

    if not seed_scores:
        return None
    return {
        "round_id": round_id,
        "average_score": float(np.mean(seed_scores)),
        "seed_scores": seed_scores,
        "pred_dynamic_mass": float(np.mean(dynamic_pred)) if dynamic_pred else 0.0,
        "truth_dynamic_mass": float(np.mean(dynamic_truth)) if dynamic_truth else 0.0,
        "pred_high_entropy_settlement": float(np.mean(high_entropy_settlement_pred)) if high_entropy_settlement_pred else 0.0,
        "truth_high_entropy_settlement": float(np.mean(high_entropy_settlement_truth)) if high_entropy_settlement_truth else 0.0,
        "pred_high_entropy_port": float(np.mean(high_entropy_port_pred)) if high_entropy_port_pred else 0.0,
        "truth_high_entropy_port": float(np.mean(high_entropy_port_truth)) if high_entropy_port_truth else 0.0,
        "pred_high_entropy_ruin": float(np.mean(high_entropy_ruin_pred)) if high_entropy_ruin_pred else 0.0,
        "truth_high_entropy_ruin": float(np.mean(high_entropy_ruin_truth)) if high_entropy_ruin_truth else 0.0,
        "covered_dynamic_weighted_kl": float(np.mean(covered_dynamic_kl)) if covered_dynamic_kl else 0.0,
        "uncovered_dynamic_weighted_kl": float(np.mean(uncovered_dynamic_kl)) if uncovered_dynamic_kl else 0.0,
    }


def _aggregate_replay_metrics(items: list[dict[str, Any]]) -> dict[str, float]:
    keys = [
        "pred_dynamic_mass",
        "truth_dynamic_mass",
        "pred_high_entropy_settlement",
        "truth_high_entropy_settlement",
        "pred_high_entropy_port",
        "truth_high_entropy_port",
        "pred_high_entropy_ruin",
        "truth_high_entropy_ruin",
        "covered_dynamic_weighted_kl",
        "uncovered_dynamic_weighted_kl",
    ]
    return {
        key: float(np.mean([float(item.get(key, 0.0)) for item in items])) if items else 0.0
        for key in keys
    }


def _archived_round_ids(artifact_store) -> list[str]:
    rounds_root = artifact_store.root / "rounds"
    if not rounds_root.exists():
        return []
    round_ids: list[str] = []
    for round_path in sorted(rounds_root.iterdir()):
        if not round_path.is_dir():
            continue
        if not (round_path / "analysis").exists():
            continue
        if not list((round_path / "analysis").glob("seed-*.json")):
            continue
        if not (round_path / "observations" / "raw").exists():
            continue
        round_ids.append(round_path.name)
    return round_ids


def _observed_distribution(counts: np.ndarray) -> np.ndarray | None:
    total = float(counts.sum())
    if total <= 0.0:
        return None
    alpha = 0.18
    return (counts + alpha) / float(total + alpha * len(counts))


def _historical_distribution(
    *,
    feature: FrontierCellFeatures,
    bucket: FrontierPriorBucket | None,
    cache: dict[str, np.ndarray],
) -> np.ndarray:
    key = feature.bucket_key()
    if key in cache:
        return cache[key]
    if bucket is not None:
        distribution = np.array(bucket.mean_distribution, dtype=float)
    else:
        distribution = _structural_prior(feature)
    cache[key] = _normalize(distribution)
    return cache[key]


def _proxy_distribution(
    *,
    feature: FrontierCellFeatures,
    observed_distribution: np.ndarray | None,
    structural: np.ndarray,
    historical_distribution: np.ndarray,
) -> np.ndarray:
    candidates: list[tuple[np.ndarray, float]] = [
        (structural, 0.28),
        (historical_distribution, 0.34),
    ]
    if observed_distribution is not None:
        weight = 0.28 if feature.observed_count == 1 else 0.42 if feature.observed_count == 2 else 0.56
        candidates.append((observed_distribution, weight))
    return _blend_distributions(candidates)


def _structural_prior(feature: FrontierCellFeatures) -> np.ndarray:
    terrain = feature.base.initial_terrain
    if terrain == 5:
        prior = np.array([0.01, 0.01, 0.01, 0.01, 0.01, 0.95], dtype=float)
    elif terrain == 10:
        prior = np.array([0.96, 0.01, 0.005, 0.005, 0.005, 0.015], dtype=float)
    elif terrain == 4:
        prior = np.array([0.34, 0.10, 0.01, 0.06, 0.47, 0.02], dtype=float)
    elif terrain == 3:
        prior = np.array([0.20, 0.18, 0.03, 0.46, 0.10, 0.03], dtype=float)
    elif terrain == 2 or feature.base.initial_port:
        prior = np.array([0.14, 0.20, 0.38, 0.12, 0.10, 0.06], dtype=float)
    elif terrain == 1:
        prior = np.array([0.14, 0.44, 0.10, 0.16, 0.12, 0.04], dtype=float)
    else:
        prior = np.array([0.82, 0.07, 0.015, 0.025, 0.05, 0.02], dtype=float)
    if feature.base.coastal:
        prior += np.array([-0.03, 0.01, 0.025, 0.0, -0.005, -0.005], dtype=float)
    return _normalize(np.clip(prior, 1e-6, None))


def _low_entropy_prior(feature: FrontierCellFeatures) -> float:
    return float(np.clip(1.0 - min(feature.historical_entropy_prior / 0.75, 1.0), 0.0, 1.0))


def _repeated_empty_stability(feature: FrontierCellFeatures) -> float:
    stability = max(
        0.0,
        1.0
        - feature.observed_dynamic_rate
        - 0.45 * feature.local_entropy
        - 0.35 * feature.window_variance
        - 0.12 * feature.observed_forest_rate,
    )
    multiplier = 1.0 if feature.repeated_count > 0 else 0.65 if feature.observed_count > 0 else 0.45
    return float(np.clip(stability * multiplier, 0.0, 1.0))


def _maritime_supported(feature: FrontierCellFeatures) -> bool:
    return bool(
        feature.base.coastal
        and (
            feature.observed_port_rate > 0.0
            or feature.base.initial_port
            or feature.nearby_wealth_mean >= 0.035
            or (feature.window_variance >= 0.08 and feature.observed_dynamic_rate >= 0.08)
        )
    )


def _is_hard_negative(feature: FrontierCellFeatures, repeated_empty_stability: float, low_entropy_prior: float) -> bool:
    frontier_support = max(
        feature.expansion_pressure,
        feature.port_pressure,
        feature.collapse_pressure,
        feature.reclaim_pressure,
        feature.observed_dynamic_rate,
        feature.hotspot_score,
        feature.frontier_uncertainty,
        feature.historical_active_frontier_rate,
    )
    return bool(
        (feature.base.buildable or feature.base.coastal)
        and repeated_empty_stability >= 0.42
        and low_entropy_prior >= 0.35
        and frontier_support < 0.22
        and feature.local_entropy < 0.25
        and feature.base.settlement_distance_bucket not in {"0", "1-2"}
    )


def _regime_scores(
    feature: FrontierCellFeatures,
    coefficients: dict[str, float],
    repeated_empty_stability: float,
    low_entropy_prior: float,
) -> dict[str, float]:
    growth_score = (
        0.92 * feature.expansion_pressure
        + 0.42 * feature.observed_settlement_rate
        + 0.26 * feature.reclaim_pressure
        + 0.22 * feature.nearby_population_mean
        + 0.18 * feature.nearby_food_mean
        + 0.16 * feature.hotspot_score
        + 0.14 * feature.historical_active_frontier_rate
        + 0.10 * feature.frontier_uncertainty
    )
    collapse_score = (
        0.98 * feature.collapse_pressure
        + 0.48 * feature.observed_ruin_rate
        + 0.20 * feature.reclaim_pressure
        + 0.16 * feature.owner_diversity
        + 0.12 * feature.hotspot_score
        + 0.10 * float(feature.base.initial_terrain == 3)
    ) * float(coefficients.get("collapse_frontier_weight", 1.0))
    maritime_score = (
        0.52 * feature.port_pressure
        + 0.34 * feature.observed_port_rate
        + 0.18 * feature.nearby_wealth_mean
        + 0.12 * float(feature.base.initial_port)
        + 0.08 * feature.historical_active_frontier_rate
    ) * float(coefficients.get("coastal_port_weight", 1.0))
    if not feature.base.coastal:
        maritime_score *= 0.1
    stable_negative_score = (
        1.08 * repeated_empty_stability
        + 0.75 * low_entropy_prior
        + 0.24 * float(feature.base.buildable or feature.base.coastal)
        - 0.90 * max(growth_score, collapse_score, maritime_score, feature.observed_dynamic_rate, feature.hotspot_score)
        - 0.20 * float(feature.base.settlement_distance_bucket in {"0", "1-2"})
    )
    return {
        "stable_negative": float(stable_negative_score),
        "growth_frontier": float(growth_score),
        "collapse_frontier": float(collapse_score),
        "maritime_frontier": float(maritime_score),
    }


def _select_regime(
    feature: FrontierCellFeatures,
    regime_scores: dict[str, float],
    coefficients: dict[str, float],
) -> str:
    # Hard override: coastal port cells must use maritime regime
    if (
        feature.base.coastal
        and (feature.base.initial_port or feature.observed_port_rate > 0.05)
        and regime_scores["maritime_frontier"] >= 0.06
        and regime_scores["stable_negative"] < 0.70
    ):
        return "maritime_frontier"
    if (
        regime_scores["stable_negative"] >= coefficients["stable_negative_threshold"]
        and regime_scores["stable_negative"] >= max(regime_scores["growth_frontier"], regime_scores["collapse_frontier"]) - 0.04
    ):
        return "stable_negative"
    if (
        feature.base.coastal
        and regime_scores["maritime_frontier"] >= coefficients["maritime_frontier_threshold"]
        and regime_scores["maritime_frontier"] >= regime_scores["growth_frontier"] + 0.04
    ):
        return "maritime_frontier"
    if (
        regime_scores["collapse_frontier"] >= coefficients["collapse_frontier_threshold"]
        and regime_scores["collapse_frontier"] >= regime_scores["growth_frontier"] - 0.02
    ):
        return "collapse_frontier"
    if regime_scores["growth_frontier"] >= coefficients["growth_frontier_threshold"] or feature.frontier_eligible:
        return "growth_frontier"
    return "stable_negative"


def _regime_dynamic_mass(
    *,
    feature: FrontierCellFeatures,
    regime: str,
    proxy_distribution: np.ndarray,
    regime_scores: dict[str, float],
    coefficients: dict[str, float],
    hard_negative: bool,
    maritime_supported: bool,
) -> float:
    proxy_dynamic = float(proxy_distribution[1:4].sum())
    frontier_support = max(regime_scores["growth_frontier"], regime_scores["collapse_frontier"], regime_scores["maritime_frontier"])
    uncertainty = min(feature.frontier_uncertainty + 0.4 * feature.historical_active_frontier_rate, 1.6)

    if regime == "stable_negative":
        dynamic_mass = 0.03 + 0.08 * proxy_dynamic + 0.06 * uncertainty
    elif regime == "growth_frontier":
        dynamic_mass = (
            0.08
            + 0.24 * proxy_dynamic
            + 0.18 * min(regime_scores["growth_frontier"], 1.4)
            + 0.08 * feature.observed_settlement_rate
            + 0.06 * feature.historical_dynamic_mass
        )
    elif regime == "collapse_frontier":
        dynamic_mass = (
            0.07
            + 0.20 * proxy_dynamic
            + 0.16 * min(regime_scores["collapse_frontier"], 1.4)
            + 0.08 * feature.observed_ruin_rate
            + 0.04 * feature.historical_dynamic_mass
        )
    else:
        dynamic_mass = (
            0.05
            + 0.12 * proxy_dynamic
            + 0.14 * min(regime_scores["maritime_frontier"], 1.2)
            + 0.08 * feature.observed_port_rate
            + 0.05 * feature.nearby_wealth_mean
        )
    if not maritime_supported and regime == "maritime_frontier":
        dynamic_mass *= 0.78
    if hard_negative:
        dynamic_mass *= 0.72
    if feature.observed_count > 0 and feature.repeated_count > 0:
        dynamic_mass = 0.55 * dynamic_mass + 0.45 * feature.observed_dynamic_rate
    elif feature.observed_count > 0:
        dynamic_mass = 0.72 * dynamic_mass + 0.28 * feature.observed_dynamic_rate
    dynamic_mass += 0.04 * max(frontier_support - 0.22, 0.0) * float(coefficients.get("positive_frontier_weight", 1.0))
    return float(np.clip(dynamic_mass, 0.01, 0.72))


def _dynamic_split(
    *,
    feature: FrontierCellFeatures,
    regime: str,
    observed_distribution: np.ndarray | None,
    proxy_distribution: np.ndarray,
    coefficients: dict[str, float],
    maritime_supported: bool,
    collapse_advantage: float,
) -> np.ndarray:
    if regime == "growth_frontier":
        base = np.array([0.86, 0.05, 0.09], dtype=float)
    elif regime == "collapse_frontier":
        base = np.array([0.38, 0.05, 0.57], dtype=float)
    elif regime == "maritime_frontier":
        base = np.array([0.42, 0.46, 0.12], dtype=float)
    else:
        base = np.array([0.58, 0.14, 0.28], dtype=float)

    signal = np.array(
        [
            1.0
            + 0.45 * feature.expansion_pressure
            + 0.30 * feature.observed_settlement_rate
            + 0.18 * feature.nearby_population_mean
            + 0.14 * feature.nearby_food_mean,
            1.0
            + 0.55 * feature.port_pressure
            + 0.42 * feature.observed_port_rate
            + 0.14 * feature.nearby_wealth_mean
            + 0.10 * float(feature.base.initial_port),
            1.0
            + 0.62 * feature.collapse_pressure
            + 0.34 * feature.observed_ruin_rate
            + 0.15 * feature.owner_diversity
            + 0.10 * float(feature.base.initial_terrain == 3),
        ],
        dtype=float,
    )
    # Suppress Port/Ruin signal when there is no direct evidence for them
    if feature.observed_port_rate < 0.01 and feature.port_pressure < 0.05 and not feature.base.initial_port:
        signal[1] = 0.30
    if feature.observed_ruin_rate < 0.01 and feature.collapse_pressure < 0.35:
        signal[2] = 0.30
    split = _normalize(base * signal)
    if observed_distribution is not None and float(observed_distribution[1:4].sum()) > 0.01:
        split = _blend_distributions(
            [
                (split, 0.60 if feature.repeated_count > 0 else 0.72),
                (_normalize(observed_distribution[1:4]), 0.40 if feature.repeated_count > 0 else 0.28),
            ]
        )
    else:
        split = _blend_distributions([(split, 0.72), (_normalize(proxy_distribution[1:4]), 0.28)])

    if regime == "growth_frontier" and collapse_advantage < 0.15:
        split[0] = max(split[0], 0.55)
        split[1] = min(split[1], 0.18)
        split = _normalize(split)
    if not maritime_supported:
        split[1] *= 0.35
        split = _normalize(split)
    split[0] *= float(coefficients["settlement_uplift_scale"])
    split[1] *= float(coefficients["port_suppression_scale"])
    split[2] *= float(coefficients["ruin_suppression_scale"])
    return _normalize(split)


def _static_split(
    *,
    feature: FrontierCellFeatures,
    observed_distribution: np.ndarray | None,
    structural: np.ndarray,
    historical_distribution: np.ndarray,
) -> np.ndarray:
    candidates: list[tuple[np.ndarray, float]] = [
        (_normalize(structural[[0, 4, 5]]), 0.48),
        (_normalize(historical_distribution[[0, 4, 5]]), 0.32),
    ]
    if observed_distribution is not None:
        candidates.append((_normalize(observed_distribution[[0, 4, 5]]), 0.20 if feature.repeated_count == 0 else 0.35))
    return _blend_distributions(candidates)


def _apply_structural_mask(probabilities: np.ndarray, feature: FrontierCellFeatures) -> np.ndarray:
    mask = soft_mask_for_cell(feature.base.initial_terrain, feature.base.coastal, feature.base.initial_port)
    masked = _normalize(probabilities * np.sqrt(mask))
    return _normalize(0.72 * probabilities + 0.28 * masked)


def _apply_local_caps(
    probabilities: np.ndarray,
    *,
    feature: FrontierCellFeatures,
    regime: str,
    maritime_supported: bool,
    hard_negative: bool,
    repeated_empty_stability: float,
) -> tuple[np.ndarray, tuple[float, str] | None]:
    adjusted = probabilities.astype(float, copy=True)
    suppression = 0.0
    notes: list[str] = []
    if not feature.base.coastal:
        suppression += max(0.0, float(adjusted[2] - 0.02))
        adjusted = _cap_class_probability(adjusted, class_index=2, cap=0.02, recipients=[0, 1, 3, 4, 5])
        if suppression > 0.0:
            notes.append("non-coastal Port cap")
    elif not maritime_supported:
        before = float(adjusted[2])
        adjusted = _cap_class_probability(adjusted, class_index=2, cap=0.05, recipients=[0, 1, 3, 4, 5])
        suppression += max(0.0, before - float(adjusted[2]))
        if before > adjusted[2]:
            notes.append("weak-maritime Port cap")
    ruin_supported = feature.observed_ruin_rate > 0.02 or feature.base.initial_terrain == 3 or feature.collapse_pressure > 0.55
    if not ruin_supported:
        before = float(adjusted[3])
        adjusted = _cap_class_probability(adjusted, class_index=3, cap=0.05, recipients=[0, 1, 4, 5])
        suppression += max(0.0, before - float(adjusted[3]))
        if before > adjusted[3]:
            notes.append("unsupported Ruin cap")
    if hard_negative or (regime == "stable_negative" and repeated_empty_stability >= 0.45):
        cap = 0.08 if feature.repeated_count > 0 else 0.12
        before = float(adjusted[1:4].sum())
        adjusted = _cap_total_dynamic_mass(adjusted, cap)
        suppression += max(0.0, before - float(adjusted[1:4].sum()))
        if before > adjusted[1:4].sum():
            notes.append("stable-negative dynamic cap")
    return _normalize(adjusted), (
        (
            suppression,
            f"({feature.x},{feature.y}) {regime} suppressed={suppression:.3f} {'; '.join(notes)}",
        )
        if suppression > 0.01
        else None
    )


def _estimate_seed_targets(
    *,
    frontier_catalog: FrontierFeatureCatalog,
    same_cell_counts: np.ndarray,
    historical_priors: FrontierPriorTable | None,
    coefficients: dict[str, float],
) -> dict[str, float]:
    height, width, _ = same_cell_counts.shape
    observed_dynamic_values: list[float] = []
    observed_settlement_values: list[float] = []
    observed_port_values: list[float] = []
    observed_ruin_values: list[float] = []
    observed_weights: list[float] = []
    prior_dynamic_values: list[float] = []
    prior_settlement_values: list[float] = []
    prior_port_values: list[float] = []
    prior_ruin_values: list[float] = []

    for y in range(height):
        for x in range(width):
            feature = frontier_catalog.feature_for(x, y)
            bucket = historical_priors.buckets.get(feature.bucket_key()) if historical_priors is not None else None
            structural = _structural_prior(feature)
            historical_distribution = _historical_distribution(feature=feature, bucket=bucket, cache={})
            prior_distribution = _blend_distributions([(structural, 0.38), (historical_distribution, 0.62)])
            prior_dynamic_values.append(float(prior_distribution[1:4].sum()))
            prior_settlement_values.append(float(prior_distribution[1]))
            prior_port_values.append(float(prior_distribution[2]))
            prior_ruin_values.append(float(prior_distribution[3]))
            observed_distribution = _observed_distribution(same_cell_counts[y, x])
            if observed_distribution is None:
                continue
            weight = (
                1.0
                + 0.9 * float(feature.frontier_eligible)
                + 0.7 * float(feature.repeated_count > 0)
                + 0.6 * feature.window_variance
                + 0.4 * feature.frontier_uncertainty
            )
            if feature.frontier_eligible:
                observed_dynamic_values.append(float(observed_distribution[1:4].sum()))
                observed_settlement_values.append(float(observed_distribution[1]))
                observed_port_values.append(float(observed_distribution[2]))
                observed_ruin_values.append(float(observed_distribution[3]))
                observed_weights.append(weight)

    prior_dynamic = float(np.mean(prior_dynamic_values)) if prior_dynamic_values else 0.0
    prior_settlement = float(np.mean(prior_settlement_values)) if prior_settlement_values else 0.0
    prior_port = float(np.mean(prior_port_values)) if prior_port_values else 0.0
    prior_ruin = float(np.mean(prior_ruin_values)) if prior_ruin_values else 0.0

    if observed_weights:
        weights = np.array(observed_weights, dtype=float)
        obs_dynamic = float(np.average(np.array(observed_dynamic_values, dtype=float), weights=weights))
        obs_settlement = float(np.average(np.array(observed_settlement_values, dtype=float), weights=weights))
        obs_port = float(np.average(np.array(observed_port_values, dtype=float), weights=weights))
        obs_ruin = float(np.average(np.array(observed_ruin_values, dtype=float), weights=weights))
    else:
        obs_dynamic = prior_dynamic
        obs_settlement = prior_settlement
        obs_port = prior_port
        obs_ruin = prior_ruin

    dynamic_target = (0.70 * obs_dynamic + 0.30 * prior_dynamic) * float(coefficients["dynamic_target_scale"])
    settlement_raw = 0.74 * obs_settlement + 0.26 * prior_settlement
    port_raw = 0.74 * obs_port + 0.26 * prior_port
    ruin_raw = 0.74 * obs_ruin + 0.26 * prior_ruin

    settlement_raw *= float(coefficients["settlement_uplift_scale"])
    port_raw *= float(coefficients["port_suppression_scale"])
    ruin_raw *= float(coefficients["ruin_suppression_scale"])
    class_raw = np.array([settlement_raw, port_raw, ruin_raw], dtype=float)
    if float(class_raw.sum()) <= 0.0:
        dynamic_split = np.array([0.70, 0.12, 0.18], dtype=float)
    else:
        dynamic_split = _normalize(class_raw)

    dynamic_target = float(np.clip(dynamic_target, 0.03, 0.50))
    settlement_target = float(dynamic_target * dynamic_split[0])
    port_target = float(dynamic_target * dynamic_split[1])
    ruin_target = float(dynamic_target * dynamic_split[2])
    return {
        "dynamic": dynamic_target,
        "settlement": settlement_target,
        "port": port_target,
        "ruin": ruin_target,
    }


def _align_dynamic_class_targets(probabilities: np.ndarray, targets: dict[str, float]) -> np.ndarray:
    adjusted = probabilities.reshape(-1, 6).astype(float, copy=True)
    current_means = adjusted.mean(axis=0)
    target_classes = np.array([targets["settlement"], targets["port"], targets["ruin"]], dtype=float)
    current_classes = np.maximum(current_means[1:4], 1e-6)
    factors = np.clip(target_classes / current_classes, 0.25, 1.8)
    adjusted[:, 1:4] *= factors
    adjusted = _normalize(adjusted)
    return adjusted.reshape(probabilities.shape)


def _cap_class_probability(
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


def _cap_total_dynamic_mass(probabilities: np.ndarray, cap: float) -> np.ndarray:
    adjusted = probabilities.astype(float, copy=True)
    dynamic_total = float(adjusted[1:4].sum())
    if dynamic_total <= cap:
        return adjusted
    scale = cap / dynamic_total if dynamic_total > 0.0 else 1.0
    adjusted[1:4] *= scale
    static_total = float(adjusted[[0, 4, 5]].sum())
    residual = 1.0 - float(adjusted[1:4].sum())
    if static_total <= 0.0:
        adjusted[[0, 4, 5]] = residual / 3.0
        return adjusted
    adjusted[[0, 4, 5]] *= residual / static_total
    return adjusted


def _scale_for_target(probabilities: np.ndarray, target_dynamic: float) -> float:
    current_dynamic = float(probabilities[:, :, 1:4].sum(axis=-1).mean())
    if current_dynamic <= 0.0:
        return 1.0
    lower = max(0.0, target_dynamic - 0.02)
    upper = min(0.95, target_dynamic + 0.02)
    desired = target_dynamic
    if lower <= current_dynamic <= upper:
        desired = current_dynamic
    return float(desired / current_dynamic)


def _rescale_seed_dynamic_mass(probabilities: np.ndarray, scale: float) -> np.ndarray:
    adjusted = probabilities.reshape(-1, 6).astype(float, copy=True)
    adjusted[:, 1:4] *= scale
    dynamic_total = adjusted[:, 1:4].sum(axis=1)
    static_total = adjusted[:, [0, 4, 5]].sum(axis=1)
    residual = np.clip(1.0 - dynamic_total, 0.01, 1.0)
    static_total = np.where(static_total <= 0.0, 1.0, static_total)
    adjusted[:, [0, 4, 5]] *= (residual / static_total)[:, None]
    adjusted = _normalize(adjusted)
    return adjusted.reshape(probabilities.shape)


def _build_same_cell_counts(width: int, height: int, observations: list[RecordedObservation]) -> np.ndarray:
    counts = np.zeros((height, width, 6), dtype=float)
    for record in observations:
        viewport = record.request.to_viewport()
        for offset_y, row in enumerate(record.result.grid):
            for offset_x, value in enumerate(row):
                x = viewport.x + offset_x
                y = viewport.y + offset_y
                counts[y, x, 0 if value in {0, 10, 11} else int(value)] += 1.0
    return counts


def _coverage_mask(height: int, width: int, observations: list[RecordedObservation]) -> np.ndarray:
    mask = np.zeros((height, width), dtype=bool)
    for record in observations:
        for x, y in record.request.to_viewport().cell_coordinates():
            mask[y, x] = True
    return mask


def _subset_weighted_kl(ground_truth: np.ndarray, prediction: np.ndarray, mask: np.ndarray) -> float:
    if not np.any(mask):
        return 0.0
    entropy_map = _entropy_map(ground_truth)
    weights = np.where(mask, entropy_map, 0.0)
    if float(weights.sum()) == 0.0:
        return 0.0
    per_cell_kl = kl_divergence(ground_truth, prediction)
    return float((weights * per_cell_kl).sum() / weights.sum())


def _entropy_map(tensor: np.ndarray) -> np.ndarray:
    clipped = np.clip(tensor, 1e-12, None)
    return -np.sum(np.where(tensor > 0.0, tensor * np.log(clipped), 0.0), axis=-1)


def _blend_distributions(candidates: list[tuple[np.ndarray, float]]) -> np.ndarray:
    blended = np.zeros_like(candidates[0][0], dtype=float)
    total_weight = 0.0
    for distribution, weight in candidates:
        if weight <= 0.0:
            continue
        blended += _normalize(distribution) * weight
        total_weight += weight
    if total_weight <= 0.0:
        return _normalize(np.ones_like(blended))
    return _normalize(blended / total_weight)


def _normalize(values: np.ndarray) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim == 1:
        return renormalize_probabilities(np.atleast_2d(array))[0]
    return renormalize_probabilities(array)
