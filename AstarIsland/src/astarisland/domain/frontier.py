from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

import numpy as np

from astarisland.domain.enums import BUILDABLE_TERRAINS, LAND_TERRAINS, TerrainCode, terrain_to_prediction_class
from astarisland.domain.features import FeatureCatalog, CellFeatures, build_feature_catalog, bucket_distance
from astarisland.domain.models import (
    FrontierPriorTable,
    InitialState,
    RecordedObservation,
    Viewport,
    WindowDynamicsSummary,
)

SETTLEMENT_RADIUS = 4


@dataclass(frozen=True, slots=True)
class ObservedSettlementSnapshot:
    seed_index: int
    x: int
    y: int
    population: float
    food: float
    wealth: float
    defense: float
    has_port: bool
    alive: bool
    owner_id: int | None
    window_key: str


@dataclass(frozen=True, slots=True)
class FrontierCellFeatures:
    x: int
    y: int
    base: CellFeatures
    frontier_eligible: bool
    observed_count: int
    repeated_count: int
    observed_settlement_rate: float
    observed_port_rate: float
    observed_ruin_rate: float
    observed_forest_rate: float
    observed_dynamic_rate: float
    local_entropy: float
    window_variance: float
    hotspot_score: float
    nearby_population_mean: float
    nearby_population_max: float
    nearby_food_mean: float
    nearby_wealth_mean: float
    nearby_defense_mean: float
    owner_diversity: int
    expansion_pressure: float
    port_pressure: float
    collapse_pressure: float
    reclaim_pressure: float
    historical_entropy_prior: float
    historical_dynamic_mass: float
    historical_hotspot_prior: float
    historical_active_frontier_rate: float
    frontier_uncertainty: float

    def bucket_key(self) -> str:
        return "|".join(
            [
                self.base.bucket_key(),
                f"frontier:{int(self.frontier_eligible)}",
                f"obs:{observation_bucket(self.observed_count)}",
                f"repeat:{observation_bucket(self.repeated_count)}",
                f"variance:{float_bucket(self.window_variance, (0.05, 0.12, 0.25, 0.4))}",
                f"pop:{float_bucket(self.nearby_population_mean, (0.4, 0.8, 1.2, 1.8))}",
                f"food:{float_bucket(self.nearby_food_mean, (0.25, 0.45, 0.65, 0.8))}",
                f"wealth:{float_bucket(self.nearby_wealth_mean, (0.005, 0.02, 0.05, 0.1))}",
                f"defense:{float_bucket(self.nearby_defense_mean, (0.2, 0.4, 0.6, 0.8))}",
                f"owner_div:{owner_diversity_bucket(self.owner_diversity)}",
            ]
        )


@dataclass(frozen=True, slots=True)
class FrontierFeatureCatalog:
    per_cell: dict[tuple[int, int], FrontierCellFeatures]

    def feature_for(self, x: int, y: int) -> FrontierCellFeatures:
        return self.per_cell[(x, y)]


@dataclass(frozen=True, slots=True)
class SeedObservationContext:
    class_counts: np.ndarray
    observation_count: np.ndarray
    class_distribution: np.ndarray
    coverage_mask: np.ndarray
    local_entropy: np.ndarray
    window_variance: np.ndarray
    hotspot_score: np.ndarray
    ruin_rate: np.ndarray
    settlement_rate: np.ndarray
    port_rate: np.ndarray
    forest_rate: np.ndarray
    dynamic_rate: np.ndarray
    nearby_population_mean: np.ndarray
    nearby_population_max: np.ndarray
    nearby_food_mean: np.ndarray
    nearby_wealth_mean: np.ndarray
    nearby_defense_mean: np.ndarray
    owner_diversity: np.ndarray
    expansion_pressure: np.ndarray
    port_pressure: np.ndarray
    collapse_pressure: np.ndarray
    reclaim_pressure: np.ndarray


def float_bucket(value: float, bounds: tuple[float, ...]) -> str:
    for bound in bounds:
        if value <= bound:
            return f"<={bound:.2f}"
    return f">{bounds[-1]:.2f}"


def observation_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value == 2:
        return "2"
    return "3+"


def owner_diversity_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 3:
        return "2-3"
    return "4+"


def iter_neighbors(x: int, y: int, width: int, height: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for delta_x, delta_y in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        next_x = x + delta_x
        next_y = y + delta_y
        if 0 <= next_x < width and 0 <= next_y < height:
            out.append((next_x, next_y))
    return out


def overlay_initial_grid(initial_state: InitialState) -> list[list[int]]:
    return initial_state.overlay_grid()


def build_observed_settlement_snapshots(observations: list[RecordedObservation]) -> list[ObservedSettlementSnapshot]:
    snapshots: list[ObservedSettlementSnapshot] = []
    for record in observations:
        for settlement in record.result.settlements:
            snapshots.append(
                ObservedSettlementSnapshot(
                    seed_index=record.seed_index,
                    x=settlement.x,
                    y=settlement.y,
                    population=float(settlement.population or 0.0),
                    food=float(settlement.food or 0.0),
                    wealth=float(settlement.wealth or 0.0),
                    defense=float(settlement.defense or 0.0),
                    has_port=settlement.has_port,
                    alive=settlement.alive,
                    owner_id=settlement.owner_id,
                    window_key=record.window_key,
                )
            )
    return snapshots


def build_window_dynamics_summaries(round_id: str, observations: list[RecordedObservation]) -> dict[str, WindowDynamicsSummary]:
    grouped: dict[str, list[RecordedObservation]] = defaultdict(list)
    for record in observations:
        grouped[record.window_key].append(record)

    summaries: dict[str, WindowDynamicsSummary] = {}
    for window_key, window_records in grouped.items():
        prototype = window_records[0]
        viewport = prototype.request.to_viewport()
        per_cell_counts: dict[tuple[int, int], np.ndarray] = defaultdict(lambda: np.zeros(6, dtype=float))
        settlement_counts: list[int] = []
        port_counts: list[int] = []
        ruin_counts: list[int] = []
        owner_diversity: list[int] = []
        populations: list[float] = []
        foods: list[float] = []
        wealths: list[float] = []
        defenses: list[float] = []
        dynamic_cells = 0
        total_cells = 0

        for record in window_records:
            settlements = record.result.settlements
            settlement_counts.append(len(settlements))
            port_counts.append(sum(1 for settlement in settlements if settlement.has_port))
            owner_diversity.append(len({settlement.owner_id for settlement in settlements if settlement.owner_id is not None}))
            populations.extend(float(settlement.population or 0.0) for settlement in settlements)
            foods.extend(float(settlement.food or 0.0) for settlement in settlements)
            wealths.extend(float(settlement.wealth or 0.0) for settlement in settlements)
            defenses.extend(float(settlement.defense or 0.0) for settlement in settlements)

            ruin_count = 0
            for offset_y, row in enumerate(record.result.grid):
                for offset_x, terrain in enumerate(row):
                    terrain_class = int(terrain_to_prediction_class(terrain))
                    absolute_x = viewport.x + offset_x
                    absolute_y = viewport.y + offset_y
                    per_cell_counts[(absolute_x, absolute_y)][terrain_class] += 1.0
                    total_cells += 1
                    if terrain in {1, 2, 3, 4}:
                        dynamic_cells += 1
                    if terrain == TerrainCode.RUIN:
                        ruin_count += 1
            ruin_counts.append(ruin_count)

        entropy_values: list[float] = []
        for counts in per_cell_counts.values():
            probabilities = counts / counts.sum()
            safe = np.where(probabilities > 0.0, probabilities, 1.0)
            entropy = -np.sum(np.where(probabilities > 0.0, probabilities * np.log(safe), 0.0))
            entropy_values.append(float(entropy))

        summaries[window_key] = WindowDynamicsSummary(
            round_id=round_id,
            seed_index=prototype.seed_index,
            window_key=window_key,
            viewport=viewport,
            repeat_count=len(window_records),
            unique_covered_cells=viewport.w * viewport.h,
            repeated_window_variance=float(np.mean(entropy_values) if entropy_values else 0.0),
            frontier_density=float(dynamic_cells / max(total_cells, 1)),
            settlement_count_mean=float(np.mean(settlement_counts) if settlement_counts else 0.0),
            port_count_mean=float(np.mean(port_counts) if port_counts else 0.0),
            ruin_count_mean=float(np.mean(ruin_counts) if ruin_counts else 0.0),
            owner_diversity_mean=float(np.mean(owner_diversity) if owner_diversity else 0.0),
            population_mean=float(np.mean(populations) if populations else 0.0),
            population_max=float(np.max(populations) if populations else 0.0),
            food_mean=float(np.mean(foods) if foods else 0.0),
            wealth_mean=float(np.mean(wealths) if wealths else 0.0),
            defense_mean=float(np.mean(defenses) if defenses else 0.0),
        )
    return summaries


def _terrain_to_class_index(terrain: int) -> int:
    if terrain in {0, 10, 11}:
        return 0
    return int(terrain)


def _scale_wealth(wealth: float) -> float:
    return min(wealth * 12.0, 1.5)


def build_seed_observation_context(
    initial_state: InitialState,
    observations: list[RecordedObservation],
    window_summaries: dict[str, WindowDynamicsSummary],
) -> SeedObservationContext:
    base_catalog = build_feature_catalog(initial_state)
    overlay_grid = overlay_initial_grid(initial_state)
    height = len(overlay_grid)
    width = len(overlay_grid[0])

    class_counts = np.zeros((height, width, 6), dtype=float)
    observation_count = np.zeros((height, width), dtype=float)
    window_variance_sum = np.zeros((height, width), dtype=float)
    window_variance_count = np.zeros((height, width), dtype=float)
    hotspot_sum = np.zeros((height, width), dtype=float)
    hotspot_count = np.zeros((height, width), dtype=float)
    owner_sets: dict[tuple[int, int], set[int | None]] = defaultdict(set)

    snapshots = build_observed_settlement_snapshots(observations)
    nearby_population_sum = np.zeros((height, width), dtype=float)
    nearby_population_weight = np.zeros((height, width), dtype=float)
    nearby_population_max = np.zeros((height, width), dtype=float)
    nearby_food_sum = np.zeros((height, width), dtype=float)
    nearby_wealth_sum = np.zeros((height, width), dtype=float)
    nearby_defense_sum = np.zeros((height, width), dtype=float)
    expansion_pressure = np.zeros((height, width), dtype=float)
    port_pressure = np.zeros((height, width), dtype=float)
    collapse_pressure = np.zeros((height, width), dtype=float)
    reclaim_pressure = np.zeros((height, width), dtype=float)

    for record in observations:
        viewport = record.request.to_viewport()
        summary = window_summaries.get(record.window_key)
        for offset_y, row in enumerate(record.result.grid):
            for offset_x, terrain in enumerate(row):
                x = viewport.x + offset_x
                y = viewport.y + offset_y
                class_counts[y, x, _terrain_to_class_index(int(terrain))] += 1.0
                observation_count[y, x] += 1.0
                if summary is not None:
                    window_variance_sum[y, x] += summary.repeated_window_variance
                    window_variance_count[y, x] += 1.0
                    hotspot_sum[y, x] += summary.repeated_window_variance + summary.frontier_density
                    hotspot_count[y, x] += 1.0
        for settlement in record.result.settlements:
            owner_sets[(settlement.x, settlement.y)].add(settlement.owner_id)

    for snapshot in snapshots:
        base_label = base_catalog.feature_for(snapshot.x, snapshot.y).landmass_membership
        expansion_signal = (1.1 if snapshot.alive else 0.2) * (
            0.55 * min(snapshot.population / 2.5, 1.5)
            + 0.30 * snapshot.food
            + 0.15 * _scale_wealth(snapshot.wealth)
        )
        port_signal = (
            (0.85 if snapshot.has_port else 0.15)
            + 0.25 * min(snapshot.population / 2.5, 1.5)
            + 0.20 * _scale_wealth(snapshot.wealth)
        )
        collapse_signal = (
            0.50 * max(0.0, 0.45 - snapshot.food)
            + 0.30 * max(0.0, 0.40 - snapshot.defense)
            + 0.20 * max(0.0, 0.40 - snapshot.population / 2.5)
        )

        for y in range(height):
            for x in range(width):
                feature = base_catalog.feature_for(x, y)
                if feature.landmass_membership != base_label or feature.landmass_membership == "water":
                    continue
                distance = abs(snapshot.x - x) + abs(snapshot.y - y)
                if distance > SETTLEMENT_RADIUS:
                    continue
                weight = (SETTLEMENT_RADIUS + 1 - distance) / float(SETTLEMENT_RADIUS + 1)
                nearby_population_sum[y, x] += snapshot.population * weight
                nearby_population_weight[y, x] += weight
                nearby_population_max[y, x] = max(nearby_population_max[y, x], snapshot.population)
                nearby_food_sum[y, x] += snapshot.food * weight
                nearby_wealth_sum[y, x] += snapshot.wealth * weight
                nearby_defense_sum[y, x] += snapshot.defense * weight
                expansion_pressure[y, x] += expansion_signal * weight
                port_pressure[y, x] += port_signal * weight
                collapse_pressure[y, x] += collapse_signal * weight

    class_distribution = class_counts / np.maximum(observation_count[..., None], 1.0)
    local_entropy = np.zeros((height, width), dtype=float)
    for class_index in range(6):
        probabilities = class_distribution[:, :, class_index]
        safe = np.where(probabilities > 0.0, probabilities, 1.0)
        local_entropy -= np.where(probabilities > 0.0, probabilities * np.log(safe), 0.0)

    settlement_rate = class_distribution[:, :, 1]
    port_rate = class_distribution[:, :, 2]
    ruin_rate = class_distribution[:, :, 3]
    forest_rate = class_distribution[:, :, 4]
    dynamic_rate = settlement_rate + port_rate + ruin_rate
    window_variance = window_variance_sum / np.maximum(window_variance_count, 1.0)
    hotspot_score = hotspot_sum / np.maximum(hotspot_count, 1.0)
    coverage_mask = observation_count > 0.0

    nearby_population_mean = nearby_population_sum / np.maximum(nearby_population_weight, 1.0)
    nearby_food_mean = nearby_food_sum / np.maximum(nearby_population_weight, 1.0)
    nearby_wealth_mean = nearby_wealth_sum / np.maximum(nearby_population_weight, 1.0)
    nearby_defense_mean = nearby_defense_sum / np.maximum(nearby_population_weight, 1.0)
    owner_diversity = np.zeros((height, width), dtype=float)
    for (x, y), owners in owner_sets.items():
        owner_diversity[y, x] = float(len({owner for owner in owners if owner is not None}))

    reclaim_seed_cells = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if ruin_rate[y, x] > 0.0 or int(overlay_grid[y][x]) == TerrainCode.RUIN
    ]
    for ruin_x, ruin_y in reclaim_seed_cells:
        local_forest = forest_rate[ruin_y, ruin_x]
        nearby_growth = expansion_pressure[ruin_y, ruin_x]
        for y in range(height):
            for x in range(width):
                distance = abs(ruin_x - x) + abs(ruin_y - y)
                if distance > SETTLEMENT_RADIUS:
                    continue
                weight = (SETTLEMENT_RADIUS + 1 - distance) / float(SETTLEMENT_RADIUS + 1)
                reclaim_pressure[y, x] += weight * (0.45 * local_forest + 0.55 * nearby_growth)

    return SeedObservationContext(
        class_counts=class_counts,
        observation_count=observation_count,
        class_distribution=class_distribution,
        coverage_mask=coverage_mask,
        local_entropy=local_entropy,
        window_variance=window_variance,
        hotspot_score=hotspot_score,
        ruin_rate=ruin_rate,
        settlement_rate=settlement_rate,
        port_rate=port_rate,
        forest_rate=forest_rate,
        dynamic_rate=dynamic_rate,
        nearby_population_mean=nearby_population_mean,
        nearby_population_max=nearby_population_max,
        nearby_food_mean=nearby_food_mean,
        nearby_wealth_mean=nearby_wealth_mean,
        nearby_defense_mean=nearby_defense_mean,
        owner_diversity=owner_diversity,
        expansion_pressure=expansion_pressure,
        port_pressure=port_pressure,
        collapse_pressure=collapse_pressure,
        reclaim_pressure=reclaim_pressure,
    )


def build_frontier_feature_catalog(
    initial_state: InitialState,
    observations: list[RecordedObservation],
    window_summaries: dict[str, WindowDynamicsSummary],
    historical_priors: FrontierPriorTable | None = None,
) -> FrontierFeatureCatalog:
    base_catalog = build_feature_catalog(initial_state)
    overlay_grid = overlay_initial_grid(initial_state)
    context = build_seed_observation_context(initial_state, observations, window_summaries)
    height = len(overlay_grid)
    width = len(overlay_grid[0])

    initial_settlement_sources = {(settlement.x, settlement.y) for settlement in initial_state.settlements}
    observed_dynamic_cells = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if context.dynamic_rate[y, x] > 0.0 or context.hotspot_score[y, x] > 0.0
    }
    ruins_or_reclaim = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if int(overlay_grid[y][x]) == TerrainCode.RUIN or context.ruin_rate[y, x] > 0.0
    }

    per_cell: dict[tuple[int, int], FrontierCellFeatures] = {}
    for y in range(height):
        for x in range(width):
            base = base_catalog.feature_for(x, y)
            terrain = TerrainCode(int(overlay_grid[y][x]))
            frontier_eligible = (
                terrain in BUILDABLE_TERRAINS
                or base.coastal
                or terrain == TerrainCode.RUIN
                or any(abs(source_x - x) + abs(source_y - y) <= 4 for source_x, source_y in initial_settlement_sources)
                or any(abs(source_x - x) + abs(source_y - y) <= 4 for source_x, source_y in observed_dynamic_cells)
                or any(abs(source_x - x) + abs(source_y - y) <= 4 for source_x, source_y in ruins_or_reclaim)
            )

            provisional = FrontierCellFeatures(
                x=x,
                y=y,
                base=base,
                frontier_eligible=frontier_eligible,
                observed_count=int(context.observation_count[y, x]),
                repeated_count=max(0, int(context.observation_count[y, x]) - 1),
                observed_settlement_rate=float(context.settlement_rate[y, x]),
                observed_port_rate=float(context.port_rate[y, x]),
                observed_ruin_rate=float(context.ruin_rate[y, x]),
                observed_forest_rate=float(context.forest_rate[y, x]),
                observed_dynamic_rate=float(context.dynamic_rate[y, x]),
                local_entropy=float(context.local_entropy[y, x]),
                window_variance=float(context.window_variance[y, x]),
                hotspot_score=float(context.hotspot_score[y, x]),
                nearby_population_mean=float(context.nearby_population_mean[y, x]),
                nearby_population_max=float(context.nearby_population_max[y, x]),
                nearby_food_mean=float(context.nearby_food_mean[y, x]),
                nearby_wealth_mean=float(context.nearby_wealth_mean[y, x]),
                nearby_defense_mean=float(context.nearby_defense_mean[y, x]),
                owner_diversity=int(context.owner_diversity[y, x]),
                expansion_pressure=float(context.expansion_pressure[y, x]),
                port_pressure=float(context.port_pressure[y, x]),
                collapse_pressure=float(context.collapse_pressure[y, x]),
                reclaim_pressure=float(context.reclaim_pressure[y, x]),
                historical_entropy_prior=0.0,
                historical_dynamic_mass=0.0,
                historical_hotspot_prior=0.0,
                historical_active_frontier_rate=0.0,
                frontier_uncertainty=0.0,
            )
            bucket_key = provisional.bucket_key()
            historical_bucket = historical_priors.buckets.get(bucket_key) if historical_priors is not None else None
            historical_entropy = historical_bucket.mean_entropy if historical_bucket is not None else 0.0
            historical_dynamic_mass = historical_bucket.mean_dynamic_mass if historical_bucket is not None else 0.0
            historical_hotspot = historical_bucket.mean_hotspot_score if historical_bucket is not None else 0.0
            historical_active_frontier_rate = historical_bucket.mean_active_frontier_rate if historical_bucket is not None else 0.0
            frontier_uncertainty = (
                0.55 * provisional.local_entropy
                + 0.25 * provisional.window_variance
                + 0.20 * historical_entropy
            )
            per_cell[(x, y)] = replace(
                provisional,
                historical_entropy_prior=historical_entropy,
                historical_dynamic_mass=historical_dynamic_mass,
                historical_hotspot_prior=historical_hotspot,
                historical_active_frontier_rate=historical_active_frontier_rate,
                frontier_uncertainty=frontier_uncertainty,
            )

    return FrontierFeatureCatalog(per_cell=per_cell)


def viewport_frontier_score(
    catalog: FrontierFeatureCatalog,
    viewport: Viewport,
    *,
    category: str,
    coverage_mask: np.ndarray | None = None,
) -> tuple[float, str]:
    score = 0.0
    frontier_cells = 0
    uncovered_frontier = 0
    hard_negative_cells = 0
    for x, y in viewport.cell_coordinates():
        feature = catalog.feature_for(x, y)
        if not feature.frontier_eligible and category not in {"hard_negative", "adaptive_negative"}:
            continue
        frontier_cells += 1
        if coverage_mask is not None and not bool(coverage_mask[y, x]):
            uncovered_frontier += 1

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
        low_entropy_prior = max(0.0, 1.0 - min(feature.historical_entropy_prior / 0.75, 1.0))
        repeated_empty_stability = max(
            0.0,
            1.0 - feature.observed_dynamic_rate - feature.observed_forest_rate - 0.35 * feature.window_variance,
        )
        hard_negative_signal = (
            0.50 * repeated_empty_stability
            + 0.35 * low_entropy_prior
            + 0.25 * float(feature.base.buildable or feature.base.coastal)
            - 0.60 * frontier_support
            - 0.18 * float(feature.base.settlement_distance_bucket in {"0", "1-2"})
        )
        if hard_negative_signal > 0.0:
            hard_negative_cells += 1

        if category == "inland_growth":
            weight = feature.expansion_pressure + 0.6 * feature.observed_settlement_rate
            if not feature.base.coastal:
                score += weight + 0.4
        elif category == "coastal_port":
            if feature.base.coastal:
                score += feature.port_pressure + 0.8 * feature.observed_port_rate + 0.2
        elif category == "ruin_rebuild":
            score += feature.reclaim_pressure + 0.9 * feature.observed_ruin_rate + 0.4 * int(feature.base.initial_terrain == TerrainCode.RUIN)
        elif category == "uncovered_frontier":
            score += feature.frontier_uncertainty + feature.historical_hotspot_prior
            if coverage_mask is not None and not bool(coverage_mask[y, x]):
                score += 0.5
        elif category in {"adaptive", "adaptive_positive"}:
            settlement_frontier = (
                0.90 * feature.expansion_pressure
                + 0.45 * feature.observed_settlement_rate
                + 0.30 * feature.reclaim_pressure
                + 0.18 * feature.nearby_population_mean
                + 0.16 * feature.historical_active_frontier_rate
            )
            collapse_frontier = (
                0.88 * feature.collapse_pressure
                + 0.42 * feature.observed_ruin_rate
                + 0.20 * feature.owner_diversity
                + 0.16 * feature.hotspot_score
            )
            moderated_coastal = (
                0.32 * feature.port_pressure
                + 0.24 * feature.observed_port_rate
                + 0.08 * float(feature.base.coastal)
            )
            score += (
                feature.frontier_uncertainty
                + 0.55 * feature.historical_hotspot_prior
                + 0.70 * settlement_frontier
                + 0.45 * collapse_frontier
                - 0.28 * moderated_coastal
            )
            if coverage_mask is not None and not bool(coverage_mask[y, x]):
                score += 0.40
        elif category == "hard_negative":
            score += max(hard_negative_signal, 0.0)
            if coverage_mask is not None and not bool(coverage_mask[y, x]):
                score += 0.15
        elif category == "adaptive_negative":
            predicted_without_support = max(feature.historical_dynamic_mass + 0.40 * feature.frontier_uncertainty - 0.55 * frontier_support, 0.0)
            score += predicted_without_support + 0.25 * max(hard_negative_signal, 0.0)
            if coverage_mask is not None and not bool(coverage_mask[y, x]):
                score += 0.20

    if category in {"hard_negative", "adaptive_negative"}:
        score += 0.12 * hard_negative_cells + 0.10 * uncovered_frontier
    else:
        score += 0.15 * frontier_cells + 0.25 * uncovered_frontier
    rationale = (
        f"{category}: score={score:.2f}, frontier_cells={frontier_cells}, "
        f"hard_negative_cells={hard_negative_cells}, uncovered_frontier={uncovered_frontier}"
    )
    return score, rationale
