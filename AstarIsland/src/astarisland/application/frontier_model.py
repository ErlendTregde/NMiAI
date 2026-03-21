from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from astarisland.domain.features import compute_distance_map
from astarisland.domain.frontier import build_frontier_feature_catalog, build_observed_settlement_snapshots
from astarisland.domain.models import (
    AnalysisResult,
    FrontierPriorTable,
    InitialState,
    ModelBundle,
    ModelBundleMetadata,
    RecordedObservation,
    RoundDetail,
    RoundReplayDiagnostics,
    TrainingExample,
    WindowDynamicsSummary,
)
from astarisland.domain.scoring import seed_score
from astarisland.infrastructure.artifact_store import ArtifactStore

MODEL_VERSION = "v3-active-frontier-gating"
FEATURE_NAMES: tuple[str, ...] = (
    "x_norm",
    "y_norm",
    "initial_terrain",
    "mapped_class",
    "coastal",
    "initial_port",
    "buildable",
    "settlement_distance",
    "forest_distance",
    "ruin_distance",
    "coast_distance",
    "landmass_size",
    "frontier_eligible",
    "observed_count",
    "repeated_count",
    "observed_empty_rate",
    "observed_settlement_rate",
    "observed_port_rate",
    "observed_ruin_rate",
    "observed_forest_rate",
    "observed_mountain_rate",
    "observed_dynamic_rate",
    "repeated_empty_stability",
    "local_entropy",
    "window_variance",
    "hotspot_score",
    "nearby_population_mean",
    "nearby_population_max",
    "nearby_food_mean",
    "nearby_wealth_mean",
    "nearby_defense_mean",
    "owner_diversity",
    "owner_conflict_count",
    "expansion_pressure",
    "port_pressure",
    "collapse_pressure",
    "reclaim_pressure",
    "frontier_uncertainty",
    "observed_settlement_distance",
    "observed_port_distance",
    "observed_ruin_distance",
    "same_landmass_frontier_count_radius4",
    "local_dynamic_density_3",
    "local_dynamic_density_5",
    "historical_entropy_prior",
    "historical_dynamic_mass",
    "historical_hotspot_prior",
    "historical_active_frontier_rate",
    "historical_low_entropy_prior",
    "support_strength",
    "hard_negative_candidate",
)


@dataclass(slots=True)
class SeedFeatureFrame:
    frontier_catalog: object
    coordinates: list[tuple[int, int]]
    matrix: np.ndarray
    same_cell_counts: np.ndarray
    observed_distribution: np.ndarray
    observed_count: np.ndarray
    support_strength: np.ndarray
    repeated_empty_stability: np.ndarray
    hard_negative_mask: np.ndarray
    historical_dynamic_mass: np.ndarray
    historical_entropy_prior: np.ndarray
    owner_conflict_count: np.ndarray
    same_landmass_frontier_count_radius4: np.ndarray
    local_dynamic_density_3: np.ndarray
    local_dynamic_density_5: np.ndarray
    observed_settlement_distance: np.ndarray
    observed_port_distance: np.ndarray
    observed_ruin_distance: np.ndarray


def _bucket_distance_to_numeric(label: str) -> float:
    return {
        "none": 15.0,
        "0": 0.0,
        "1-2": 1.5,
        "3-5": 4.0,
        "6-9": 7.5,
        "10+": 10.0,
    }.get(label, 15.0)


def _landmass_size_to_numeric(label: str) -> float:
    return {
        "water": 0.0,
        "tiny": 1.0,
        "small": 2.0,
        "medium": 3.0,
        "large": 4.0,
    }.get(label, 0.0)


def _normalize(values: np.ndarray) -> np.ndarray:
    totals = values.sum(axis=-1, keepdims=True)
    totals[totals == 0.0] = 1.0
    return values / totals


def _aligned_probabilities(model, matrix: np.ndarray, class_count: int) -> np.ndarray:
    probabilities = model.predict_proba(matrix)
    classes = getattr(model, "classes_", np.arange(probabilities.shape[1]))
    aligned = np.zeros((matrix.shape[0], class_count), dtype=float)
    for source_index, class_index in enumerate(classes):
        aligned[:, int(class_index)] = probabilities[:, source_index]
    return _normalize(aligned)


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


def _radius_mean(values: np.ndarray, radius: int) -> np.ndarray:
    height, width = values.shape
    out = np.zeros_like(values, dtype=float)
    for y in range(height):
        for x in range(width):
            y0 = max(0, y - radius)
            y1 = min(height, y + radius + 1)
            x0 = max(0, x - radius)
            x1 = min(width, x + radius + 1)
            patch = values[y0:y1, x0:x1]
            out[y, x] = float(patch.mean()) if patch.size else 0.0
    return out


def _build_distance_map(width: int, height: int, sources: list[tuple[int, int]]) -> dict[tuple[int, int], int | None]:
    return compute_distance_map(sources, width, height)


def build_seed_feature_frame(
    initial_state: InitialState,
    observations: list[RecordedObservation],
    window_summaries: dict[str, WindowDynamicsSummary],
    historical_priors: FrontierPriorTable | None,
) -> SeedFeatureFrame:
    frontier_catalog = build_frontier_feature_catalog(
        initial_state,
        observations=observations,
        window_summaries=window_summaries,
        historical_priors=historical_priors,
    )
    grid = initial_state.overlay_grid()
    height = len(grid)
    width = len(grid[0])
    same_cell_counts = _build_same_cell_counts(width, height, observations)
    observed_count = same_cell_counts.sum(axis=-1)
    observed_distribution = same_cell_counts / np.maximum(observed_count[..., None], 1.0)
    snapshots = build_observed_settlement_snapshots(observations)

    observed_settlement_sources = [(snapshot.x, snapshot.y) for snapshot in snapshots]
    observed_port_sources = [(snapshot.x, snapshot.y) for snapshot in snapshots if snapshot.has_port]
    observed_ruin_sources = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if observed_distribution[y, x, 3] > 0.0 or int(grid[y][x]) == 3
    ]
    settlement_distances = _build_distance_map(width, height, observed_settlement_sources)
    port_distances = _build_distance_map(width, height, observed_port_sources)
    ruin_distances = _build_distance_map(width, height, observed_ruin_sources)

    observed_dynamic_rate = observed_distribution[:, :, 1:4].sum(axis=-1)
    observed_empty_rate = observed_distribution[:, :, 0]
    observed_mountain_rate = observed_distribution[:, :, 5]
    repeated_empty_stability = observed_empty_rate * (1.0 - np.minimum(1.2 * np.array(
        [[frontier_catalog.feature_for(x, y).window_variance for x in range(width)] for y in range(height)],
        dtype=float,
    ), 0.85))
    repeated_empty_stability *= np.where(observed_count > 1.0, 1.0, 0.6)
    density3 = _radius_mean(observed_dynamic_rate, radius=1)
    density5 = _radius_mean(observed_dynamic_rate, radius=2)

    support_strength_grid = np.zeros((height, width), dtype=float)
    for y in range(height):
        for x in range(width):
            feature = frontier_catalog.feature_for(x, y)
            support_strength_grid[y, x] = max(
                feature.expansion_pressure,
                feature.port_pressure,
                feature.collapse_pressure,
                feature.reclaim_pressure,
                feature.observed_dynamic_rate,
                feature.hotspot_score,
                feature.frontier_uncertainty,
                feature.historical_active_frontier_rate,
            )

    hard_negative_mask = np.zeros((height, width), dtype=bool)
    same_landmass_frontier_count = np.zeros((height, width), dtype=float)
    owner_conflict_count = np.zeros((height, width), dtype=float)
    owner_snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.owner_id is not None
    ]
    for y in range(height):
        for x in range(width):
            feature = frontier_catalog.feature_for(x, y)
            landmass_label = feature.base.landmass_membership
            if landmass_label != "water":
                count = 0
                owners: set[int] = set()
                for neighbor_y in range(height):
                    for neighbor_x in range(width):
                        neighbor = frontier_catalog.feature_for(neighbor_x, neighbor_y)
                        if neighbor.base.landmass_membership != landmass_label:
                            continue
                        if abs(neighbor_x - x) + abs(neighbor_y - y) > 4:
                            continue
                        if support_strength_grid[neighbor_y, neighbor_x] > 0.18:
                            count += 1
                    same_landmass_frontier_count[y, x] = float(count)
                for snapshot in owner_snapshots:
                    if abs(snapshot.x - x) + abs(snapshot.y - y) <= 4:
                        owners.add(int(snapshot.owner_id))
                owner_conflict_count[y, x] = float(max(0, len(owners) - 1))

            low_entropy_prior = max(0.0, 1.0 - min(feature.historical_entropy_prior / 0.75, 1.0))
            hard_negative_mask[y, x] = bool(
                (feature.base.buildable or feature.base.coastal)
                and support_strength_grid[y, x] < 0.22
                and repeated_empty_stability[y, x] > 0.45
                and density5[y, x] < 0.18
                and low_entropy_prior > 0.35
                and _bucket_distance_to_numeric(feature.base.settlement_distance_bucket) >= 3.0
            )

    coordinates: list[tuple[int, int]] = []
    rows: list[list[float]] = []
    historical_dynamic_mass = np.zeros(height * width, dtype=float)
    historical_entropy_prior = np.zeros(height * width, dtype=float)
    support_strength = np.zeros(height * width, dtype=float)
    flat_repeated_empty_stability = np.zeros(height * width, dtype=float)

    for y in range(height):
        for x in range(width):
            index = y * width + x
            feature = frontier_catalog.feature_for(x, y)
            historical_low_entropy_prior = max(0.0, 1.0 - min(feature.historical_entropy_prior / 0.75, 1.0))
            coordinates.append((x, y))
            support_strength[index] = support_strength_grid[y, x]
            flat_repeated_empty_stability[index] = repeated_empty_stability[y, x]
            historical_dynamic_mass[index] = feature.historical_dynamic_mass
            historical_entropy_prior[index] = feature.historical_entropy_prior
            rows.append(
                [
                    x / max(width - 1, 1),
                    y / max(height - 1, 1),
                    float(feature.base.initial_terrain),
                    float(feature.base.mapped_class),
                    float(feature.base.coastal),
                    float(feature.base.initial_port),
                    float(feature.base.buildable),
                    _bucket_distance_to_numeric(feature.base.settlement_distance_bucket),
                    _bucket_distance_to_numeric(feature.base.forest_distance_bucket),
                    _bucket_distance_to_numeric(feature.base.ruin_distance_bucket),
                    _bucket_distance_to_numeric(feature.base.coast_distance_bucket),
                    _landmass_size_to_numeric(feature.base.landmass_size_bucket),
                    float(feature.frontier_eligible),
                    float(feature.observed_count),
                    float(feature.repeated_count),
                    float(observed_empty_rate[y, x]),
                    float(feature.observed_settlement_rate),
                    float(feature.observed_port_rate),
                    float(feature.observed_ruin_rate),
                    float(feature.observed_forest_rate),
                    float(observed_mountain_rate[y, x]),
                    float(feature.observed_dynamic_rate),
                    float(repeated_empty_stability[y, x]),
                    float(feature.local_entropy),
                    float(feature.window_variance),
                    float(feature.hotspot_score),
                    float(feature.nearby_population_mean),
                    float(feature.nearby_population_max),
                    float(feature.nearby_food_mean),
                    float(feature.nearby_wealth_mean),
                    float(feature.nearby_defense_mean),
                    float(feature.owner_diversity),
                    float(owner_conflict_count[y, x]),
                    float(feature.expansion_pressure),
                    float(feature.port_pressure),
                    float(feature.collapse_pressure),
                    float(feature.reclaim_pressure),
                    float(feature.frontier_uncertainty),
                    float(settlement_distances[(x, y)] if settlement_distances[(x, y)] is not None else 15.0),
                    float(port_distances[(x, y)] if port_distances[(x, y)] is not None else 15.0),
                    float(ruin_distances[(x, y)] if ruin_distances[(x, y)] is not None else 15.0),
                    float(same_landmass_frontier_count[y, x]),
                    float(density3[y, x]),
                    float(density5[y, x]),
                    float(feature.historical_entropy_prior),
                    float(feature.historical_dynamic_mass),
                    float(feature.historical_hotspot_prior),
                    float(feature.historical_active_frontier_rate),
                    float(historical_low_entropy_prior),
                    float(support_strength_grid[y, x]),
                    float(hard_negative_mask[y, x]),
                ]
            )

    return SeedFeatureFrame(
        frontier_catalog=frontier_catalog,
        coordinates=coordinates,
        matrix=np.array(rows, dtype=float),
        same_cell_counts=same_cell_counts,
        observed_distribution=observed_distribution.reshape(height * width, 6),
        observed_count=observed_count.reshape(height * width),
        support_strength=support_strength,
        repeated_empty_stability=flat_repeated_empty_stability,
        hard_negative_mask=hard_negative_mask.reshape(height * width),
        historical_dynamic_mass=historical_dynamic_mass,
        historical_entropy_prior=historical_entropy_prior,
        owner_conflict_count=owner_conflict_count.reshape(height * width),
        same_landmass_frontier_count_radius4=same_landmass_frontier_count.reshape(height * width),
        local_dynamic_density_3=density3.reshape(height * width),
        local_dynamic_density_5=density5.reshape(height * width),
        observed_settlement_distance=np.array(
            [float(settlement_distances[(x, y)] if settlement_distances[(x, y)] is not None else 15.0) for x, y in coordinates],
            dtype=float,
        ),
        observed_port_distance=np.array(
            [float(port_distances[(x, y)] if port_distances[(x, y)] is not None else 15.0) for x, y in coordinates],
            dtype=float,
        ),
        observed_ruin_distance=np.array(
            [float(ruin_distances[(x, y)] if ruin_distances[(x, y)] is not None else 15.0) for x, y in coordinates],
            dtype=float,
        ),
    )


def _fit_gate_model(matrix: np.ndarray, labels: np.ndarray) -> object:
    if len(np.unique(labels)) < 2:
        model = DummyClassifier(strategy="prior")
        model.fit(matrix, labels)
        return model
    model = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_depth=4,
        max_iter=250,
        min_samples_leaf=20,
        random_state=0,
    )
    model.fit(matrix, labels)
    return model


def _fit_dynamic_mass_model(matrix: np.ndarray, targets: np.ndarray, sample_weight: np.ndarray) -> object:
    if np.allclose(targets, targets[0]):
        model = DummyRegressor(strategy="constant", constant=float(targets[0]))
        model.fit(matrix, targets)
        return model
    model = HistGradientBoostingRegressor(
        learning_rate=0.05,
        max_depth=4,
        max_iter=250,
        min_samples_leaf=20,
        random_state=0,
    )
    model.fit(matrix, targets, sample_weight=sample_weight)
    return model


def _fit_multiclass_model(matrix: np.ndarray, labels: np.ndarray, sample_weight: np.ndarray) -> object:
    if len(np.unique(labels)) < 2:
        model = DummyClassifier(strategy="prior")
        model.fit(matrix, labels)
        return model
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(
            max_iter=500,
            solver="lbfgs",
            random_state=0,
        ),
    )
    model.fit(matrix, labels, logisticregression__sample_weight=sample_weight)
    return model


def train_frontier_model_bundle(
    artifact_store: ArtifactStore,
    historical_priors: FrontierPriorTable | None = None,
) -> tuple[ModelBundle | None, ModelBundleMetadata]:
    rounds_root = artifact_store.root / "rounds"
    if not rounds_root.exists():
        return None, ModelBundleMetadata(model_version=MODEL_VERSION)

    priors = historical_priors or artifact_store.load_aggregated_frontier_prior_table()
    training_examples: list[TrainingExample] = []
    round_ids: list[str] = []

    for round_path in sorted(rounds_root.iterdir()):
        if not round_path.is_dir():
            continue
        round_id = round_path.name
        analysis_results = artifact_store.load_analysis_results(round_id)
        if not analysis_results:
            continue
        observations = artifact_store.load_observations(round_id)
        if not observations:
            continue
        round_detail = artifact_store.load_round_detail(round_id)
        window_summaries = artifact_store.load_window_dynamics_summaries(round_id)
        per_seed_observations: dict[int, list[RecordedObservation]] = {}
        for record in observations:
            per_seed_observations.setdefault(record.seed_index, []).append(record)

        round_ids.append(round_id)
        for seed_index, initial_state in enumerate(round_detail.initial_states):
            analysis = analysis_results.get(seed_index)
            if analysis is None:
                continue
            frame = build_seed_feature_frame(
                initial_state,
                per_seed_observations.get(seed_index, []),
                window_summaries,
                priors,
            )
            ground_truth = np.array(analysis.ground_truth, dtype=float)
            entropy = -np.sum(np.where(ground_truth > 0, ground_truth * np.log(np.clip(ground_truth, 1e-12, None)), 0.0), axis=-1)
            width = round_detail.map_width
            height = round_detail.map_height
            for index, (x, y) in enumerate(frame.coordinates):
                dynamic_mass_target = float(ground_truth[y, x, 1:4].sum())
                active_target = int(entropy[y, x] >= 0.45 or dynamic_mass_target >= 0.15)
                dynamic_class_target = int(np.argmax(ground_truth[y, x, 1:4]))
                static_class_target = int(np.argmax(ground_truth[y, x, [0, 4, 5]]))
                training_examples.append(
                    TrainingExample(
                        round_id=round_id,
                        seed_index=seed_index,
                        x=x,
                        y=y,
                        features=tuple(float(value) for value in frame.matrix[index]),
                        active_frontier_target=active_target,
                        dynamic_mass_target=dynamic_mass_target,
                        dynamic_class_target=dynamic_class_target,
                        static_class_target=static_class_target,
                        hard_negative_candidate=bool(frame.hard_negative_mask[index]),
                    )
                )

    if not training_examples:
        return None, ModelBundleMetadata(model_version=MODEL_VERSION)

    feature_matrix = np.array([example.features for example in training_examples], dtype=float)
    active_targets = np.array([example.active_frontier_target for example in training_examples], dtype=int)
    dynamic_targets = np.array([example.dynamic_mass_target for example in training_examples], dtype=float)
    hard_negative_mask = np.array([example.hard_negative_candidate for example in training_examples], dtype=bool)

    positive_indices = np.flatnonzero(active_targets == 1)
    negative_indices = np.flatnonzero((active_targets == 0) & hard_negative_mask)
    if len(negative_indices) == 0:
        negative_indices = np.flatnonzero(active_targets == 0)
    negative_limit = min(len(negative_indices), max(len(positive_indices) * 3, 1))
    gate_indices = np.concatenate([positive_indices, negative_indices[:negative_limit]])
    gate_matrix = feature_matrix[gate_indices]
    gate_targets = active_targets[gate_indices]

    dynamic_sample_weight = 0.5 + 1.5 * active_targets + 2.0 * dynamic_targets
    dynamic_mask = dynamic_targets >= 0.05
    dynamic_matrix = feature_matrix[dynamic_mask]
    dynamic_class_targets = np.array([example.dynamic_class_target for example in training_examples], dtype=int)[dynamic_mask]
    dynamic_class_weights = np.maximum(dynamic_targets[dynamic_mask], 0.05)
    static_class_targets = np.array([example.static_class_target for example in training_examples], dtype=int)
    static_class_weights = np.maximum(1.0 - dynamic_targets, 0.05)

    gate_model = _fit_gate_model(gate_matrix, gate_targets)
    dynamic_mass_model = _fit_dynamic_mass_model(feature_matrix, dynamic_targets, dynamic_sample_weight)
    dynamic_split_model = _fit_multiclass_model(dynamic_matrix, dynamic_class_targets, dynamic_class_weights)
    static_split_model = _fit_multiclass_model(feature_matrix, static_class_targets, static_class_weights)

    bundle = ModelBundle(
        model_version=MODEL_VERSION,
        trained_rounds=tuple(sorted(set(round_ids))),
        feature_names=FEATURE_NAMES,
        global_dynamic_mass_mean=float(dynamic_targets.mean()),
        global_active_frontier_rate=float(active_targets.mean()),
        gate_model=gate_model,
        dynamic_mass_model=dynamic_mass_model,
        dynamic_split_model=dynamic_split_model,
        static_split_model=static_split_model,
    )

    replay_diagnostics: list[RoundReplayDiagnostics] = []
    for round_id in sorted(set(round_ids)):
        diagnostics = replay_model_bundle(artifact_store, round_id, bundle, priors)
        if diagnostics is not None:
            replay_diagnostics.append(diagnostics)

    metadata = ModelBundleMetadata(
        model_version=MODEL_VERSION,
        rounds_included=list(bundle.trained_rounds),
        feature_names=list(FEATURE_NAMES),
        training_examples=len(training_examples),
        gate_examples=len(gate_indices),
        replay_diagnostics=replay_diagnostics,
    )
    return bundle, metadata


def predict_seed_with_model_bundle(
    bundle: ModelBundle,
    frame: SeedFeatureFrame,
    width: int,
    height: int,
) -> tuple[np.ndarray, dict[str, float]]:
    matrix = frame.matrix
    gate_prob = _aligned_probabilities(bundle.gate_model, matrix, 2)[:, 1]
    dynamic_mass = np.clip(bundle.dynamic_mass_model.predict(matrix), 0.0, 1.0)
    dynamic_mass = dynamic_mass * gate_prob
    observed_dynamic = frame.observed_distribution[:, 1:4].sum(axis=1)
    dynamic_mass = 0.65 * dynamic_mass + 0.25 * observed_dynamic + 0.10 * np.maximum(frame.historical_dynamic_mass, bundle.global_dynamic_mass_mean)

    dynamic_split = _aligned_probabilities(bundle.dynamic_split_model, matrix, 3)
    static_split = _aligned_probabilities(bundle.static_split_model, matrix, 3)
    probabilities = np.zeros((matrix.shape[0], 6), dtype=float)
    probabilities[:, 1:4] = dynamic_mass[:, None] * dynamic_split
    probabilities[:, [0, 4, 5]] = (1.0 - dynamic_mass)[:, None] * static_split

    for index, (x, y) in enumerate(frame.coordinates):
        feature = frame.frontier_catalog.feature_for(x, y)
        observed = frame.observed_distribution[index]
        support = frame.support_strength[index]
        probabilities[index] = _apply_cell_caps(
            probabilities[index],
            feature=feature,
            support_strength=support,
            repeated_empty_stability=float(frame.repeated_empty_stability[index]),
            hard_negative=bool(frame.hard_negative_mask[index]),
        )
        if frame.observed_count[index] > 0.0:
            weight = 0.25 if frame.observed_count[index] == 1 else 0.45 if frame.observed_count[index] == 2 else 0.60
            probabilities[index] = _normalize(
                np.atleast_2d((1.0 - weight) * probabilities[index] + weight * observed)
            )[0]

    target_dynamic = _estimate_seed_dynamic_target(bundle, frame)
    current_dynamic = float(probabilities[:, 1:4].sum(axis=1).mean())
    lower = max(0.0, target_dynamic - 0.02)
    upper = min(0.95, target_dynamic + 0.02)
    if current_dynamic < lower:
        desired = lower
    elif current_dynamic > upper:
        desired = upper
    else:
        desired = current_dynamic
    scale = desired / current_dynamic if current_dynamic > 0 else 1.0
    probabilities = _rescale_seed_dynamic_mass(probabilities, scale)
    probabilities = _normalize(probabilities)

    return probabilities.reshape(height, width, 6), {
        "mean_dynamic_before_correction": current_dynamic,
        "mean_dynamic_after_correction": float(probabilities[:, 1:4].sum(axis=1).mean()),
        "target_dynamic_mass": target_dynamic,
    }


def _estimate_seed_dynamic_target(bundle: ModelBundle, frame: SeedFeatureFrame) -> float:
    observed_dynamic = frame.observed_distribution[:, 1:4].sum(axis=1)
    historical_dynamic = np.where(frame.historical_dynamic_mass > 0.0, frame.historical_dynamic_mass, bundle.global_dynamic_mass_mean)
    covered = frame.observed_count > 0.0
    cell_targets = np.where(covered, 0.25 * observed_dynamic + 0.75 * historical_dynamic, historical_dynamic)
    cell_targets += 0.02 * np.clip(frame.support_strength - 0.20, 0.0, 1.0)
    target = float(cell_targets.mean())
    return float(np.clip(target, max(0.04, bundle.global_dynamic_mass_mean - 0.03), bundle.global_dynamic_mass_mean + 0.05))


def _apply_cell_caps(
    probabilities: np.ndarray,
    *,
    feature,
    support_strength: float,
    repeated_empty_stability: float,
    hard_negative: bool,
) -> np.ndarray:
    adjusted = probabilities.astype(float, copy=True)
    if not feature.base.coastal:
        adjusted = _cap_class_probability(adjusted, 2, 0.04)
    if support_strength < 0.18 and repeated_empty_stability > 0.55:
        adjusted = _cap_total_dynamic_mass(adjusted, 0.12)
    if hard_negative and support_strength < 0.16:
        adjusted = _cap_total_dynamic_mass(adjusted, 0.08)
    if feature.base.initial_terrain != 3 and feature.collapse_pressure < 0.12 and feature.reclaim_pressure < 0.12:
        adjusted = _cap_class_probability(adjusted, 3, 0.12)
    return _normalize(np.atleast_2d(adjusted))[0]


def _cap_total_dynamic_mass(probabilities: np.ndarray, cap: float) -> np.ndarray:
    adjusted = probabilities.astype(float, copy=True)
    dynamic = float(adjusted[1:4].sum())
    if dynamic <= cap:
        return adjusted
    excess = dynamic - cap
    dynamic_split = adjusted[1:4] / max(dynamic, 1e-9)
    adjusted[1:4] = cap * dynamic_split
    static_mass = float(adjusted[[0, 4, 5]].sum())
    if static_mass <= 0.0:
        adjusted[[0, 4, 5]] += excess / 3.0
    else:
        adjusted[[0, 4, 5]] += excess * adjusted[[0, 4, 5]] / static_mass
    return adjusted


def _cap_class_probability(probabilities: np.ndarray, class_index: int, cap: float) -> np.ndarray:
    adjusted = probabilities.astype(float, copy=True)
    excess = max(0.0, float(adjusted[class_index] - cap))
    if excess <= 0.0:
        return adjusted
    adjusted[class_index] = cap
    recipients = np.array([0, 1, 2, 3, 4, 5], dtype=int)
    recipients = recipients[recipients != class_index]
    recipient_mass = float(adjusted[recipients].sum())
    if recipient_mass <= 0.0:
        adjusted[recipients] += excess / len(recipients)
    else:
        adjusted[recipients] += excess * adjusted[recipients] / recipient_mass
    return adjusted


def _rescale_seed_dynamic_mass(probabilities: np.ndarray, scale: float) -> np.ndarray:
    adjusted = probabilities.astype(float, copy=True)
    for index in range(adjusted.shape[0]):
        dynamic = float(adjusted[index, 1:4].sum())
        if dynamic <= 0.0:
            continue
        new_dynamic = float(np.clip(dynamic * scale, 0.0, 0.95))
        diff = dynamic - new_dynamic
        dynamic_split = adjusted[index, 1:4] / max(dynamic, 1e-9)
        adjusted[index, 1:4] = new_dynamic * dynamic_split
        if diff > 0.0:
            static_mass = float(adjusted[index, [0, 4, 5]].sum())
            if static_mass <= 0.0:
                adjusted[index, [0, 4, 5]] += diff / 3.0
            else:
                adjusted[index, [0, 4, 5]] += diff * adjusted[index, [0, 4, 5]] / static_mass
        elif diff < 0.0:
            removal = min(-diff, float(adjusted[index, [0, 4, 5]].sum()))
            static_mass = float(adjusted[index, [0, 4, 5]].sum())
            if static_mass > 0.0:
                adjusted[index, [0, 4, 5]] -= removal * adjusted[index, [0, 4, 5]] / static_mass
    return adjusted


def replay_model_bundle(
    artifact_store: ArtifactStore,
    round_id: str,
    bundle: ModelBundle,
    historical_priors: FrontierPriorTable | None,
) -> RoundReplayDiagnostics | None:
    analysis_results = artifact_store.load_analysis_results(round_id)
    if not analysis_results:
        return None
    round_detail = artifact_store.load_round_detail(round_id)
    observations = artifact_store.load_observations(round_id)
    window_summaries = artifact_store.load_window_dynamics_summaries(round_id)
    per_seed_observations: dict[int, list[RecordedObservation]] = {}
    for record in observations:
        per_seed_observations.setdefault(record.seed_index, []).append(record)

    seed_scores: list[float] = []
    dynamic_gaps: list[float] = []
    port_gaps: list[float] = []
    dynamic_mass_errors: list[float] = []
    for seed_index, initial_state in enumerate(round_detail.initial_states):
        analysis = analysis_results.get(seed_index)
        if analysis is None:
            continue
        frame = build_seed_feature_frame(
            initial_state,
            per_seed_observations.get(seed_index, []),
            window_summaries,
            historical_priors,
        )
        prediction, debug = predict_seed_with_model_bundle(
            bundle,
            frame,
            width=round_detail.map_width,
            height=round_detail.map_height,
        )
        truth = np.array(analysis.ground_truth, dtype=float)
        seed_scores.append(seed_score(truth, prediction))
        truth_dynamic = truth[:, :, 1:4].sum(axis=-1)
        pred_dynamic = prediction[:, :, 1:4].sum(axis=-1)
        dynamic_gaps.append(float(pred_dynamic.mean() - truth_dynamic.mean()))
        entropy = -np.sum(np.where(truth > 0, truth * np.log(np.clip(truth, 1e-12, None)), 0.0), axis=-1)
        high_entropy = entropy >= 0.5
        if int(high_entropy.sum()) > 0:
            port_gaps.append(float(prediction[:, :, 2][high_entropy].mean() - truth[:, :, 2][high_entropy].mean()))
        dynamic_mass_errors.append(abs(debug["mean_dynamic_after_correction"] - float(truth_dynamic.mean())))

    if not seed_scores:
        return None
    return RoundReplayDiagnostics(
        round_id=round_id,
        model_version=bundle.model_version,
        average_score=float(sum(seed_scores) / len(seed_scores)),
        seed_scores=seed_scores,
        mean_dynamic_mass_gap=float(np.mean(dynamic_gaps) if dynamic_gaps else 0.0),
        mean_high_entropy_port_gap=float(np.mean(port_gaps) if port_gaps else 0.0),
        max_seed_dynamic_mass_error=float(max(dynamic_mass_errors) if dynamic_mass_errors else 0.0),
    )
