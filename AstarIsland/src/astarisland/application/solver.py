"""Bayesian solver for Astar Island, v4.

Key improvements over v3:
1. Per-round weighted priors (Gaussian kernel similarity)
2. Lower probability floor (0.005 instead of 0.01)
3. α tuning via backtesting
4. Backtest function for validation
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from math import ceil
from pathlib import Path

import numpy as np

from astarisland.domain.enums import TerrainCode
from astarisland.domain.models import (
    InitialState,
    ObservationRequest,
    ObservationResult,
    RoundDetail,
    Viewport,
)
from astarisland.domain.scoring import entropy_weighted_kl, seed_score
from astarisland.infrastructure.api_client import AstarIslandApiClient

VIEWPORT_SIZE = 15
PRIOR_STRENGTH = 3.0
MIN_PROB_FLOOR = 0.005
SAFETY_FLOOR = 0.001
NUM_CLASSES = 6
REGIME_SIGMA = 0.3  # Gaussian kernel bandwidth for round similarity (0.3 best with observations, 0.8 for prior-only fallback)

# ─── Hardcoded fallback priors ───

_PLAINS_INLAND = {
    "0-2": [0.686, 0.234, 0.000, 0.020, 0.059, 0.000],
    "3-4": [0.809, 0.143, 0.000, 0.013, 0.035, 0.000],
    "5-6": [0.902, 0.074, 0.000, 0.007, 0.017, 0.000],
    "7-8": [0.950, 0.040, 0.000, 0.003, 0.007, 0.000],
    "9+":  [0.980, 0.016, 0.000, 0.001, 0.003, 0.000],
}
_PLAINS_COASTAL = {
    "0-2": [0.700, 0.104, 0.126, 0.019, 0.052, 0.000],
    "3-4": [0.819, 0.073, 0.066, 0.012, 0.031, 0.000],
    "5-6": [0.913, 0.037, 0.030, 0.006, 0.015, 0.000],
    "7-8": [0.959, 0.019, 0.013, 0.003, 0.006, 0.000],
    "9+":  [0.982, 0.009, 0.006, 0.001, 0.003, 0.000],
}
_FOREST_INLAND = {
    "0-2": [0.125, 0.244, 0.000, 0.021, 0.610, 0.000],
    "3-4": [0.070, 0.146, 0.000, 0.014, 0.771, 0.000],
    "5-6": [0.034, 0.079, 0.000, 0.008, 0.879, 0.000],
    "7-8": [0.014, 0.037, 0.000, 0.004, 0.945, 0.000],
    "9+":  [0.005, 0.015, 0.000, 0.001, 0.979, 0.000],
}
_FOREST_COASTAL = {
    "0-2": [0.113, 0.115, 0.138, 0.020, 0.614, 0.000],
    "3-4": [0.065, 0.075, 0.065, 0.012, 0.783, 0.000],
    "5-6": [0.035, 0.045, 0.037, 0.006, 0.877, 0.000],
    "7-8": [0.012, 0.018, 0.014, 0.003, 0.953, 0.000],
    "9+":  [0.004, 0.009, 0.006, 0.002, 0.980, 0.000],
}
_SETTLEMENT_INLAND = [0.426, 0.336, 0.000, 0.028, 0.210, 0.000]
_SETTLEMENT_COASTAL = [0.452, 0.113, 0.185, 0.025, 0.225, 0.000]
_PORT = [0.467, 0.107, 0.171, 0.022, 0.233, 0.000]
_OCEAN = [1.000, 0.000, 0.000, 0.000, 0.000, 0.000]
_MOUNTAIN = [0.000, 0.000, 0.000, 0.000, 0.000, 1.000]


# ─── Helper functions ───

def _terrain_to_class(terrain: int) -> int:
    if terrain in {0, 10, 11}:
        return 0
    return int(terrain)


def _distance_bucket(dist: int) -> str:
    if dist <= 2:
        return "0-2"
    if dist <= 4:
        return "3-4"
    if dist <= 6:
        return "5-6"
    if dist <= 8:
        return "7-8"
    return "9+"


def _is_coastal(grid: list[list[int]], x: int, y: int, height: int, width: int) -> bool:
    if grid[y][x] == TerrainCode.OCEAN:
        return False
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if nx < 0 or nx >= width or ny < 0 or ny >= height:
            return True
        if grid[ny][nx] == TerrainCode.OCEAN:
            return True
    return False


def _settlement_distance(x: int, y: int, settlements: list[tuple[int, int]]) -> int:
    if not settlements:
        return 99
    return min(abs(x - sx) + abs(y - sy) for sx, sy in settlements)


def _has_forest_neighbor(grid: list[list[int]], x: int, y: int, height: int, width: int) -> bool:
    """Check if any adjacent cell (4-directional) is forest."""
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < width and 0 <= ny < height:
            if grid[ny][nx] == TerrainCode.FOREST:
                return True
    return False


def _feature_key(terrain: int, dist_bucket: str, coastal: bool, forest_adj: bool = False) -> str:
    return f"{terrain}|{dist_bucket}|{int(coastal)}|{int(forest_adj)}"


# ─── Training pipeline ───

def train_priors(api_client: AstarIslandApiClient, data_dir: Path) -> dict:
    """Download ground truth from ALL completed rounds and compute priors."""
    rounds_info = api_client.my_rounds()
    completed = [r for r in rounds_info if r.get("status") == "completed"]
    print(f"Found {len(completed)} completed rounds")

    # Global accumulators
    prior_sums: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(NUM_CLASSES))
    prior_counts: dict[str, int] = defaultdict(int)

    # Per-round accumulators
    per_round_sums: dict[str, dict[str, np.ndarray]] = {}
    per_round_counts: dict[str, dict[str, int]] = {}

    round_rates: list[dict] = []

    for ri, round_info in enumerate(completed):
        round_id = round_info["id"]
        round_num = round_info.get("round_number", "?")
        seeds_count = round_info.get("seeds_count", 5)
        print(f"\n[{ri + 1}/{len(completed)}] Round {round_num} ({round_id[:8]}...)")

        try:
            detail = api_client.get_round_detail(round_id)
        except Exception as e:
            print(f"  Could not get round detail: {e}")
            continue

        # Per-round accumulators
        r_sums: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(NUM_CLASSES))
        r_counts: dict[str, int] = defaultdict(int)

        settlement_survival = 0.0
        settlement_total = 0
        expansion_count = 0.0
        expansion_total = 0
        port_count = 0.0
        coastal_total = 0
        ruin_count = 0.0

        for seed_idx in range(min(seeds_count, len(detail.initial_states))):
            try:
                analysis = api_client.analysis(round_id, seed_idx)
            except Exception as e:
                print(f"  Seed {seed_idx}: skipped ({e})")
                continue

            initial_state = detail.initial_states[seed_idx]
            grid = initial_state.overlay_grid()
            height, width = len(grid), len(grid[0])
            settlements = [(s.x, s.y) for s in initial_state.settlements]
            gt = analysis.ground_truth

            for y in range(height):
                for x in range(width):
                    terrain = grid[y][x]
                    coastal = _is_coastal(grid, x, y, height, width)
                    forest_adj = _has_forest_neighbor(grid, x, y, height, width)
                    dist = _settlement_distance(x, y, settlements)
                    db = _distance_bucket(dist)
                    gt_dist = np.array(gt[y][x], dtype=float)

                    key = _feature_key(terrain, db, coastal, forest_adj)
                    prior_sums[key] += gt_dist
                    prior_counts[key] += 1
                    r_sums[key] += gt_dist
                    r_counts[key] += 1

                    if terrain in {TerrainCode.SETTLEMENT, TerrainCode.PORT}:
                        settlement_survival += gt_dist[1] + gt_dist[2]
                        ruin_count += gt_dist[3]
                        settlement_total += 1
                    if terrain in {TerrainCode.EMPTY, TerrainCode.PLAINS} and dist <= 4:
                        expansion_count += gt_dist[1]
                        expansion_total += 1
                    if coastal and terrain not in {TerrainCode.OCEAN, TerrainCode.MOUNTAIN}:
                        port_count += gt_dist[2]
                        coastal_total += 1

            print(f"  Seed {seed_idx}: processed {width}x{height} grid")
            time.sleep(0.05)

        # Store per-round priors
        r_priors = {}
        for key, sums in r_sums.items():
            n = r_counts[key]
            if n >= 3:
                r_priors[key] = (sums / n).tolist()
        per_round_sums[round_id] = {k: v.tolist() for k, v in r_sums.items()}
        per_round_counts[round_id] = dict(r_counts)

        if settlement_total > 0:
            round_rates.append({
                "round_id": round_id,
                "round_number": round_num,
                "survival_rate": float(settlement_survival / settlement_total),
                "expansion_rate": float(expansion_count / max(expansion_total, 1)),
                "port_rate": float(port_count / max(coastal_total, 1)),
                "ruin_rate": float(ruin_count / settlement_total),
                "priors": r_priors,
            })

    # Compute global priors
    priors = {}
    for key, sums in prior_sums.items():
        n = prior_counts[key]
        if n >= 3:
            priors[key] = (sums / n).tolist()

    # Compute historical average rates
    historical_rates = {}
    if round_rates:
        historical_rates = {
            "survival_rate": float(np.mean([r["survival_rate"] for r in round_rates])),
            "expansion_rate": float(np.mean([r["expansion_rate"] for r in round_rates])),
            "port_rate": float(np.mean([r["port_rate"] for r in round_rates])),
            "ruin_rate": float(np.mean([r["ruin_rate"] for r in round_rates])),
        }

    # Compute rate std devs for normalization
    rate_stds = {}
    if len(round_rates) >= 3:
        rate_stds = {
            "survival_rate": float(np.std([r["survival_rate"] for r in round_rates])),
            "expansion_rate": float(np.std([r["expansion_rate"] for r in round_rates])),
            "port_rate": float(np.std([r["port_rate"] for r in round_rates])),
            "ruin_rate": float(np.std([r["ruin_rate"] for r in round_rates])),
        }

    result = {
        "version": "v4",
        "rounds_included": [r["round_id"] for r in round_rates],
        "total_cells": int(sum(prior_counts.values())),
        "groups": len(priors),
        "optimal_alpha": PRIOR_STRENGTH,
        "historical_rates": historical_rates,
        "rate_stds": rate_stds,
        "per_round_rates": round_rates,  # includes per-round priors
        "priors": priors,
    }

    out_path = data_dir / "learned" / "trained-priors.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nTraining complete!")
    print(f"  Rounds: {len(round_rates)}")
    print(f"  Total cells: {sum(prior_counts.values())}")
    print(f"  Feature groups: {len(priors)}")
    if historical_rates:
        print(f"  Historical survival: {historical_rates['survival_rate']:.3f}")
        print(f"  Historical expansion: {historical_rates['expansion_rate']:.3f}")
        print(f"  Historical port: {historical_rates['port_rate']:.3f}")
        print(f"  Historical ruin: {historical_rates['ruin_rate']:.3f}")
    print(f"  Saved to: {out_path}")

    return result


# ─── Prior loading ───

_trained_cache: dict | None = None


def load_trained_priors(data_dir: Path) -> dict | None:
    global _trained_cache
    if _trained_cache is not None:
        return _trained_cache
    path = data_dir / "learned" / "trained-priors.json"
    if not path.exists():
        return None
    with open(path) as f:
        _trained_cache = json.load(f)
    return _trained_cache


def clear_trained_cache() -> None:
    global _trained_cache
    _trained_cache = None


# ─── Per-round weighted prior lookup ───

def _round_similarity_weights(
    current_rates: dict[str, float],
    trained: dict,
    exclude_round_id: str | None = None,
) -> list[tuple[dict, float]]:
    """Compute Gaussian-kernel similarity weights for each historical round."""
    per_round = trained.get("per_round_rates", [])
    rate_stds = trained.get("rate_stds", {})
    if not per_round or not rate_stds:
        return []

    rate_keys = ["survival_rate", "expansion_rate", "port_rate", "ruin_rate"]
    stds = [max(rate_stds.get(k, 0.1), 0.01) for k in rate_keys]

    weighted_rounds = []
    for r in per_round:
        if exclude_round_id and r["round_id"] == exclude_round_id:
            continue
        if "priors" not in r:
            continue

        # Normalized euclidean distance
        d_sq = 0.0
        for i, k in enumerate(rate_keys):
            diff = (current_rates.get(k, 0) - r.get(k, 0)) / stds[i]
            d_sq += diff * diff

        weight = math.exp(-d_sq / (2 * REGIME_SIGMA ** 2))
        if weight > 0.001:  # Skip negligible weights
            weighted_rounds.append((r, weight))

    return weighted_rounds


def _weighted_round_rates(
    current_rates: dict[str, float],
    trained: dict,
    exclude_round_id: str | None = None,
) -> dict[str, float] | None:
    """Get the average regime rates implied by the weighted matching rounds."""
    weighted_rounds = _round_similarity_weights(current_rates, trained, exclude_round_id)
    if not weighted_rounds:
        return None
    total_weight = sum(w for _, w in weighted_rounds)
    if total_weight < 0.001:
        return None
    rates = {}
    for key in ["survival_rate", "expansion_rate", "port_rate", "ruin_rate"]:
        rates[key] = sum(r.get(key, 0) * w for r, w in weighted_rounds) / total_weight
    return rates


def weighted_round_prior(
    feature_key: str,
    current_rates: dict[str, float],
    trained: dict,
    exclude_round_id: str | None = None,
) -> np.ndarray | None:
    """Compute weighted-average prior from similar historical rounds."""
    weighted_rounds = _round_similarity_weights(current_rates, trained, exclude_round_id)
    if not weighted_rounds:
        return None

    total_weight = 0.0
    weighted_sum = np.zeros(NUM_CLASSES)

    for r, weight in weighted_rounds:
        if feature_key in r["priors"]:
            weighted_sum += weight * np.array(r["priors"][feature_key])
            total_weight += weight

    if total_weight < 0.01:
        return None

    result = weighted_sum / total_weight
    total = result.sum()
    if total > 0:
        result /= total
    return result


# ─── Prior lookup ───

def data_prior(
    terrain: int, coastal: bool, settlement_dist: int,
    trained: dict | None = None,
    current_rates: dict[str, float] | None = None,
    exclude_round_id: str | None = None,
    forest_adj: bool = False,
) -> np.ndarray:
    """Data-calibrated prior. Uses per-round weighted priors if available."""
    if terrain == TerrainCode.OCEAN:
        return np.array(_OCEAN, dtype=float)
    if terrain == TerrainCode.MOUNTAIN:
        return np.array(_MOUNTAIN, dtype=float)

    db = _distance_bucket(settlement_dist)
    feature_key = _feature_key(terrain, db, coastal, forest_adj)

    # Try per-round weighted priors (best)
    if trained and current_rates:
        result = weighted_round_prior(feature_key, current_rates, trained, exclude_round_id)
        if result is not None:
            return result
        # Fallback: try without forest_adj
        if forest_adj:
            key_no_forest = _feature_key(terrain, db, coastal, False)
            result = weighted_round_prior(key_no_forest, current_rates, trained, exclude_round_id)
            if result is not None:
                return result

    # Try global trained priors (good)
    if trained and "priors" in trained:
        if feature_key in trained["priors"]:
            return np.array(trained["priors"][feature_key], dtype=float)
        # Fallback: try without forest_adj
        if forest_adj:
            key_no_forest = _feature_key(terrain, db, coastal, False)
            if key_no_forest in trained["priors"]:
                return np.array(trained["priors"][key_no_forest], dtype=float)
        key_alt = _feature_key(terrain, db, not coastal, forest_adj)
        if key_alt in trained["priors"]:
            return np.array(trained["priors"][key_alt], dtype=float)
        key_alt2 = _feature_key(terrain, db, not coastal, False)
        if key_alt2 in trained["priors"]:
            return np.array(trained["priors"][key_alt2], dtype=float)
        for fallback_db in ["0-2", "3-4", "5-6", "7-8", "9+"]:
            key_fb = _feature_key(terrain, fallback_db, coastal, False)
            if key_fb in trained["priors"]:
                return np.array(trained["priors"][key_fb], dtype=float)

    # Hardcoded fallback
    if terrain == TerrainCode.SETTLEMENT:
        raw = _SETTLEMENT_COASTAL if coastal else _SETTLEMENT_INLAND
        return np.array(raw, dtype=float)
    if terrain == TerrainCode.PORT:
        return np.array(_PORT, dtype=float)
    if terrain == TerrainCode.FOREST:
        table = _FOREST_COASTAL if coastal else _FOREST_INLAND
    else:
        table = _PLAINS_COASTAL if coastal else _PLAINS_INLAND
    return np.array(table[db], dtype=float)


# ─── Context-aware probability floor ───

def _apply_context_floor(probs: np.ndarray, terrain: int, coastal: bool) -> np.ndarray:
    floored = probs.copy()
    for i in range(NUM_CLASSES):
        if i == 5 and terrain != TerrainCode.MOUNTAIN:
            floored[i] = max(floored[i], SAFETY_FLOOR)
        elif i == 2 and not coastal:
            floored[i] = max(floored[i], SAFETY_FLOOR)
        else:
            floored[i] = max(floored[i], MIN_PROB_FLOOR)
    return floored / floored.sum()


# ─── Cross-seed regime detection ───

def compute_regime(
    round_detail: RoundDetail,
    all_observations: dict[int, list[tuple[Viewport, ObservationResult]]],
) -> dict[str, float | dict]:
    """Estimate round-specific regime from cross-seed observations."""
    terrain_counts: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(NUM_CLASSES))
    terrain_totals: dict[str, int] = defaultdict(int)

    settlement_obs_survived = 0.0
    settlement_obs_ruin = 0.0
    settlement_obs_total = 0
    expansion_obs_settlement = 0.0
    expansion_obs_total = 0
    port_obs = 0.0
    coastal_obs_total = 0

    for seed_index, obs_list in all_observations.items():
        grid = round_detail.initial_states[seed_index].overlay_grid()
        settlements = [(s.x, s.y) for s in round_detail.initial_states[seed_index].settlements]
        height, width = len(grid), len(grid[0])

        for viewport, result in obs_list:
            for dy, row in enumerate(result.grid):
                for dx, terrain_obs in enumerate(row):
                    y, x = viewport.y + dy, viewport.x + dx
                    if y >= height or x >= width:
                        continue
                    initial_terrain = grid[y][x]
                    coastal = _is_coastal(grid, x, y, height, width)
                    obs_class = _terrain_to_class(terrain_obs)
                    dist = _settlement_distance(x, y, settlements)
                    db = _distance_bucket(dist)

                    # Distance-aware empirical key for more specific blending
                    fine_key = f"{initial_terrain}|{int(coastal)}|{db}"
                    terrain_counts[fine_key][obs_class] += 1
                    terrain_totals[fine_key] += 1
                    # Coarse key as fallback
                    coarse_key = f"{initial_terrain}|{int(coastal)}"
                    terrain_counts[coarse_key][obs_class] += 1
                    terrain_totals[coarse_key] += 1

                    if initial_terrain in {TerrainCode.SETTLEMENT, TerrainCode.PORT}:
                        if obs_class in {1, 2}:
                            settlement_obs_survived += 1
                        elif obs_class == 3:
                            settlement_obs_ruin += 1
                        settlement_obs_total += 1

                    dist = _settlement_distance(x, y, settlements)
                    if initial_terrain in {TerrainCode.EMPTY, TerrainCode.PLAINS} and dist <= 4:
                        if obs_class == 1:
                            expansion_obs_settlement += 1
                        expansion_obs_total += 1

                    if coastal and initial_terrain not in {TerrainCode.OCEAN, TerrainCode.MOUNTAIN}:
                        if obs_class == 2:
                            port_obs += 1
                        coastal_obs_total += 1

    regime: dict = {
        "empirical": {},
        "survival_rate": settlement_obs_survived / max(settlement_obs_total, 1),
        "ruin_rate": settlement_obs_ruin / max(settlement_obs_total, 1),
        "expansion_rate": expansion_obs_settlement / max(expansion_obs_total, 1),
        "port_rate": port_obs / max(coastal_obs_total, 1),
        "settlement_obs_total": settlement_obs_total,
        "expansion_obs_total": expansion_obs_total,
        "coastal_obs_total": coastal_obs_total,
    }

    for key, counts in terrain_counts.items():
        total = terrain_totals[key]
        if total >= 10:
            regime["empirical"][key] = {
                "dist": (counts / total).tolist(),
                "n": total,
            }

    return regime


def calibrated_prior(
    terrain: int,
    coastal: bool,
    settlement_dist: int,
    regime: dict,
    trained: dict | None = None,
    exclude_round_id: str | None = None,
    forest_adj: bool = False,
) -> np.ndarray:
    """Get prior using per-round weighting + empirical blending."""
    current_rates = {
        "survival_rate": regime.get("survival_rate", 0.31),
        "expansion_rate": regime.get("expansion_rate", 0.17),
        "port_rate": regime.get("port_rate", 0.06),
        "ruin_rate": regime.get("ruin_rate", 0.025),
    }

    db = _distance_bucket(settlement_dist)
    base = data_prior(terrain, coastal, settlement_dist, trained, current_rates, exclude_round_id, forest_adj)

    # Blend with distance-aware empirical observations (more specific)
    empirical = regime.get("empirical", {})
    fine_key = f"{terrain}|{int(coastal)}|{db}"
    coarse_key = f"{terrain}|{int(coastal)}"
    if fine_key in empirical:
        emp = empirical[fine_key]
        emp_dist = np.array(emp["dist"])
        n = emp["n"]
        weight = min(0.30, n / 200)  # Higher max weight for fine-grained keys
        base = (1 - weight) * base + weight * emp_dist
    elif coarse_key in empirical:
        emp = empirical[coarse_key]
        emp_dist = np.array(emp["dist"])
        n = emp["n"]
        weight = min(0.20, n / 500)  # Lower weight for coarse fallback
        base = (1 - weight) * base + weight * emp_dist

    # Rate correction: close gap between matched-round rates and observed rates
    matched_rates = regime.get("_matched_rates")
    if matched_rates:
        strength = 1.0  # Full correction (validated by cross-round backtest)
        if terrain in {TerrainCode.SETTLEMENT, TerrainCode.PORT}:
            # Settlement survival correction
            mr_surv = max(matched_rates.get("survival_rate", 0.3), 0.005)
            obs_surv = current_rates.get("survival_rate", 0.3)
            surv_corr = 1.0 + strength * (obs_surv / mr_surv - 1.0)
            surv_corr = max(0.3, min(3.0, surv_corr))
            base[1] *= surv_corr  # Settlement
            base[2] *= surv_corr  # Port
            # Ruin correction
            mr_ruin = max(matched_rates.get("ruin_rate", 0.02), 0.001)
            obs_ruin = current_rates.get("ruin_rate", 0.02)
            ruin_corr = 1.0 + strength * (obs_ruin / mr_ruin - 1.0)
            ruin_corr = max(0.3, min(3.0, ruin_corr))
            base[3] *= ruin_corr
        elif settlement_dist <= 4 and terrain not in {TerrainCode.OCEAN, TerrainCode.MOUNTAIN}:
            # Expansion correction for nearby cells
            mr_exp = max(matched_rates.get("expansion_rate", 0.15), 0.005)
            obs_exp = current_rates.get("expansion_rate", 0.15)
            exp_corr = 1.0 + strength * (obs_exp / mr_exp - 1.0)
            exp_corr = max(0.3, min(3.0, exp_corr))
            base[1] *= exp_corr  # Settlement expansion
        if coastal and terrain not in {TerrainCode.OCEAN, TerrainCode.MOUNTAIN}:
            # Port correction for coastal cells
            mr_port = max(matched_rates.get("port_rate", 0.05), 0.005)
            obs_port = current_rates.get("port_rate", 0.05)
            port_corr = 1.0 + strength * (obs_port / mr_port - 1.0)
            port_corr = max(0.3, min(3.0, port_corr))
            base[2] *= port_corr

    # Hard constraints
    if terrain == TerrainCode.OCEAN:
        return np.array(_OCEAN, dtype=float)
    if terrain == TerrainCode.MOUNTAIN:
        return np.array(_MOUNTAIN, dtype=float)
    if terrain != TerrainCode.MOUNTAIN:
        base[5] = 0.0
    if not coastal:
        base[2] = 0.0

    base = np.maximum(base, 0.0)
    total = base.sum()
    if total > 0:
        base /= total
    return base


# ─── Viewport selection ───

def tile_viewports(height: int, width: int) -> list[Viewport]:
    n_x = ceil(width / VIEWPORT_SIZE)
    n_y = ceil(height / VIEWPORT_SIZE)
    if n_x <= 1:
        x_positions = [0]
    else:
        x_positions = [round(i * (width - VIEWPORT_SIZE) / (n_x - 1)) for i in range(n_x)]
    if n_y <= 1:
        y_positions = [0]
    else:
        y_positions = [round(i * (height - VIEWPORT_SIZE) / (n_y - 1)) for i in range(n_y)]

    viewports = []
    for vy in y_positions:
        for vx in x_positions:
            viewports.append(Viewport(
                x=max(0, min(vx, width - VIEWPORT_SIZE)),
                y=max(0, min(vy, height - VIEWPORT_SIZE)),
                w=VIEWPORT_SIZE,
                h=VIEWPORT_SIZE,
            ))
    return viewports


def pick_bonus_viewport(
    initial_state: InitialState,
    height: int,
    width: int,
    existing: list[Viewport],
    trained: dict | None = None,
    current_rates: dict[str, float] | None = None,
    observations: list[tuple[Viewport, ObservationResult]] | None = None,
) -> Viewport:
    """Pick bonus viewport maximizing dynamic activity score.

    If observations are available, targets areas where dynamic terrain was observed
    (settlements, ports, ruins). Otherwise falls back to prior entropy.
    """
    grid = initial_state.overlay_grid()
    settlements = [(s.x, s.y) for s in initial_state.settlements]

    cell_score = np.zeros((height, width))

    if observations:
        # Use observed dynamism: cells that showed settlement/port/ruin activity
        for viewport, result in observations:
            for dy, row in enumerate(result.grid):
                for dx, terrain_obs in enumerate(row):
                    y, x = viewport.y + dy, viewport.x + dx
                    if y < height and x < width:
                        cls = _terrain_to_class(terrain_obs)
                        if cls in {1, 2, 3}:  # Settlement, Port, Ruin
                            cell_score[y, x] += 2.0
                        elif grid[y][x] in {TerrainCode.SETTLEMENT, TerrainCode.PORT}:
                            # Initial settlement that became Empty/Forest — dynamic!
                            if cls in {0, 4}:
                                cell_score[y, x] += 3.0
            # Extra weight for observed settlements with low health
            for s in result.settlements:
                if 0 <= s.x < width and 0 <= s.y < height:
                    h = _settlement_health(
                        s.population or 0, s.food or 0, s.wealth or 0, s.defense or 0
                    )
                    if h < 0.4:  # Struggling settlements = most uncertain outcome
                        cell_score[s.y, s.x] += 3.0
    else:
        # Fallback: prior entropy
        for y in range(height):
            for x in range(width):
                terrain = grid[y][x]
                if terrain in {TerrainCode.OCEAN, TerrainCode.MOUNTAIN}:
                    continue
                coastal = _is_coastal(grid, x, y, height, width)
                forest_adj = _has_forest_neighbor(grid, x, y, height, width)
                dist = _settlement_distance(x, y, settlements)
                prior = data_prior(terrain, coastal, dist, trained, current_rates, forest_adj=forest_adj)
                mask = prior > 0
                if mask.any():
                    cell_score[y, x] = -np.sum(prior[mask] * np.log(prior[mask]))

    best_score = -1.0
    best_vp = existing[0]
    max_x = max(width - VIEWPORT_SIZE, 0)
    max_y = max(height - VIEWPORT_SIZE, 0)

    for vy in range(0, max_y + 1, 2):
        for vx in range(0, max_x + 1, 2):
            score = float(cell_score[vy:vy + VIEWPORT_SIZE, vx:vx + VIEWPORT_SIZE].sum())
            if score > best_score:
                best_score = score
                best_vp = Viewport(x=vx, y=vy, w=VIEWPORT_SIZE, h=VIEWPORT_SIZE)

    return best_vp


# ─── Prediction building ───

def _settlement_health(population: float, food: float, wealth: float, defense: float) -> float:
    """Compute health score in [0, 1] from settlement stats."""
    # Normalize each stat to roughly [0, 1] based on typical ranges from training data
    pop_score = min(population / 5.0, 1.0) if population else 0.0
    food_score = min(food / 3.0, 1.0) if food else 0.0
    wealth_score = min(wealth / 3.0, 1.0) if wealth else 0.0
    defense_score = min(defense / 2.0, 1.0) if defense else 0.0
    return 0.35 * pop_score + 0.30 * food_score + 0.20 * wealth_score + 0.15 * defense_score


def build_prediction(
    initial_state: InitialState,
    observations: list[tuple[Viewport, ObservationResult]],
    height: int,
    width: int,
    regime: dict,
    trained: dict | None = None,
    alpha: float = PRIOR_STRENGTH,
    exclude_round_id: str | None = None,
) -> np.ndarray:
    grid = initial_state.overlay_grid()
    settlements = [(s.x, s.y) for s in initial_state.settlements]

    # Precompute matched round rates for regime correction (prior-only fallback)
    # Only apply rate correction when we have NO observations — with observations,
    # the empirical blending already handles regime calibration and rate correction adds noise.
    has_observations = len(observations) > 0
    if trained and "_matched_rates" not in regime and not has_observations:
        current_rates = {
            "survival_rate": regime.get("survival_rate", 0.31),
            "expansion_rate": regime.get("expansion_rate", 0.17),
            "port_rate": regime.get("port_rate", 0.06),
            "ruin_rate": regime.get("ruin_rate", 0.025),
        }
        matched = _weighted_round_rates(current_rates, trained, exclude_round_id)
        regime["_matched_rates"] = matched

    # Count terrain observations per cell
    counts = np.zeros((height, width, NUM_CLASSES), dtype=float)
    for viewport, result in observations:
        for dy, row in enumerate(result.grid):
            for dx, terrain in enumerate(row):
                y, x = viewport.y + dy, viewport.x + dx
                if y < height and x < width:
                    counts[y, x, _terrain_to_class(terrain)] += 1.0
    total_obs = counts.sum(axis=-1)

    # Collect settlement stats from observations (uses absolute coordinates)
    cell_health: dict[tuple[int, int], list[float]] = defaultdict(list)
    for viewport, result in observations:
        for s in result.settlements:
            if 0 <= s.x < width and 0 <= s.y < height and s.alive:
                h = _settlement_health(
                    s.population or 0, s.food or 0, s.wealth or 0, s.defense or 0
                )
                cell_health[(s.x, s.y)].append(h)

    prediction = np.zeros((height, width, NUM_CLASSES), dtype=float)

    for y in range(height):
        for x in range(width):
            terrain = grid[y][x]
            coastal = _is_coastal(grid, x, y, height, width)
            forest_adj = _has_forest_neighbor(grid, x, y, height, width)
            dist = _settlement_distance(x, y, settlements)

            prior = calibrated_prior(terrain, coastal, dist, regime, trained, exclude_round_id, forest_adj)

            n = total_obs[y, x]
            if n > 0:
                prior_alpha = prior * alpha
                posterior = (counts[y, x] + prior_alpha) / (n + alpha)
            else:
                posterior = prior.copy()

            # Adjust based on observed settlement health
            if (x, y) in cell_health:
                avg_health = float(np.mean(cell_health[(x, y)]))
                # Health > 0.5 → settlement is thriving → boost Settlement/Port
                # Health < 0.3 → settlement is struggling → boost Ruin/Empty
                if avg_health > 0.5:
                    boost = 0.05 * (avg_health - 0.5)  # max ~0.025
                    posterior[1] += boost  # Settlement
                    if coastal:
                        posterior[2] += boost * 0.5  # Port
                    posterior[3] = max(posterior[3] - boost * 0.3, 0)  # Ruin down
                    posterior[0] = max(posterior[0] - boost * 0.7, 0)  # Empty down
                elif avg_health < 0.3:
                    boost = 0.05 * (0.3 - avg_health)  # max ~0.015
                    posterior[3] += boost  # Ruin up
                    posterior[0] += boost  # Empty up
                    posterior[1] = max(posterior[1] - boost, 0)  # Settlement down

            prediction[y, x] = _apply_context_floor(posterior, terrain, coastal)

    return prediction


# ─── Backtesting ───

def backtest_round(
    trained: dict,
    round_detail: RoundDetail,
    ground_truths: list[np.ndarray],
    alpha: float,
    use_weighted: bool = True,
    exclude_round_id: str | None = None,
) -> float:
    """Backtest a single round: simulate observations + Bayesian update vs ground truth.

    Simulates 1 observation per cell (sampled from ground truth distribution),
    then applies Bayesian update with the given α to test prior quality.
    """
    height = round_detail.map_height
    width = round_detail.map_width
    rng = np.random.default_rng(42)  # Fixed seed for reproducibility

    # Build regime from actual ground truth rates (simulates what queries reveal)
    gt_regime = _compute_gt_regime(round_detail, ground_truths)

    # Precompute matched rates for rate correction
    if use_weighted and trained:
        current_rates = {
            "survival_rate": gt_regime.get("survival_rate", 0.31),
            "expansion_rate": gt_regime.get("expansion_rate", 0.17),
            "port_rate": gt_regime.get("port_rate", 0.06),
            "ruin_rate": gt_regime.get("ruin_rate", 0.025),
        }
        gt_regime["_matched_rates"] = _weighted_round_rates(current_rates, trained, exclude_round_id)

    seed_scores = []
    for seed_idx, gt in enumerate(ground_truths):
        initial_state = round_detail.initial_states[seed_idx]
        regime = gt_regime if use_weighted else {"empirical": {}}
        prediction = np.zeros((height, width, NUM_CLASSES), dtype=float)
        grid = initial_state.overlay_grid()
        settlements = [(s.x, s.y) for s in initial_state.settlements]

        for y in range(height):
            for x in range(width):
                terrain = grid[y][x]
                coastal = _is_coastal(grid, x, y, height, width)
                forest_adj = _has_forest_neighbor(grid, x, y, height, width)
                dist = _settlement_distance(x, y, settlements)

                prior = calibrated_prior(
                    terrain, coastal, dist, regime, trained, exclude_round_id, forest_adj
                )

                # Simulate 1 observation by sampling from ground truth
                gt_dist = gt[y, x]
                gt_dist_safe = np.maximum(gt_dist, 0)
                gt_sum = gt_dist_safe.sum()
                if gt_sum > 0:
                    gt_dist_safe /= gt_sum
                    sampled_class = rng.choice(NUM_CLASSES, p=gt_dist_safe)
                    obs_counts = np.zeros(NUM_CLASSES, dtype=float)
                    obs_counts[sampled_class] = 1.0
                    posterior = (obs_counts + alpha * prior) / (1.0 + alpha)
                else:
                    posterior = prior.copy()

                prediction[y, x] = _apply_context_floor(posterior, terrain, coastal)

        score = seed_score(gt, prediction)
        seed_scores.append(score)

    return float(np.mean(seed_scores))


def _compute_gt_regime(
    round_detail: RoundDetail,
    ground_truths: list[np.ndarray],
) -> dict:
    """Compute regime rates from ground truth (simulates observation).

    Also builds empirical distributions matching what compute_regime produces,
    so that backtest accurately tests the empirical blending path.
    """
    survival_sum = 0.0
    settlement_total = 0
    expansion_sum = 0.0
    expansion_total = 0
    port_sum = 0.0
    coastal_total = 0
    ruin_sum = 0.0

    # Empirical accumulators (matching compute_regime structure)
    rng = np.random.default_rng(123)  # Separate RNG for regime sampling
    terrain_counts: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(NUM_CLASSES))
    terrain_totals: dict[str, int] = defaultdict(int)

    for seed_idx, gt in enumerate(ground_truths):
        initial_state = round_detail.initial_states[seed_idx]
        grid = initial_state.overlay_grid()
        height, width = len(grid), len(grid[0])
        settlements = [(s.x, s.y) for s in initial_state.settlements]

        for y in range(height):
            for x in range(width):
                terrain = grid[y][x]
                coastal = _is_coastal(grid, x, y, height, width)
                gt_dist = gt[y, x]
                dist = _settlement_distance(x, y, settlements)
                db = _distance_bucket(dist)

                # Sample 1 observation for empirical (simulates what solver sees)
                gt_safe = np.maximum(gt_dist, 0)
                gt_sum = gt_safe.sum()
                if gt_sum > 0:
                    gt_safe /= gt_sum
                    obs_class = int(rng.choice(NUM_CLASSES, p=gt_safe))
                else:
                    obs_class = 0

                # Fine-grained key (distance-aware)
                fine_key = f"{terrain}|{int(coastal)}|{db}"
                terrain_counts[fine_key][obs_class] += 1
                terrain_totals[fine_key] += 1
                # Coarse key
                coarse_key = f"{terrain}|{int(coastal)}"
                terrain_counts[coarse_key][obs_class] += 1
                terrain_totals[coarse_key] += 1

                if terrain in {TerrainCode.SETTLEMENT, TerrainCode.PORT}:
                    survival_sum += gt_dist[1] + gt_dist[2]
                    ruin_sum += gt_dist[3]
                    settlement_total += 1

                if terrain in {TerrainCode.EMPTY, TerrainCode.PLAINS} and dist <= 4:
                    expansion_sum += gt_dist[1]
                    expansion_total += 1

                if coastal and terrain not in {TerrainCode.OCEAN, TerrainCode.MOUNTAIN}:
                    port_sum += gt_dist[2]
                    coastal_total += 1

    # Build empirical dict
    empirical = {}
    for key, counts in terrain_counts.items():
        total = terrain_totals[key]
        if total >= 10:
            empirical[key] = {
                "dist": (counts / total).tolist(),
                "n": total,
            }

    return {
        "empirical": empirical,
        "survival_rate": survival_sum / max(settlement_total, 1),
        "ruin_rate": ruin_sum / max(settlement_total, 1),
        "expansion_rate": expansion_sum / max(expansion_total, 1),
        "port_rate": port_sum / max(coastal_total, 1),
        "settlement_obs_total": settlement_total,
        "expansion_obs_total": expansion_total,
        "coastal_obs_total": coastal_total,
    }


def run_backtest(api_client: AstarIslandApiClient, data_dir: Path) -> dict:
    """Full backtest: test per-round weighting and find optimal alpha."""
    clear_trained_cache()
    trained = load_trained_priors(data_dir)
    if not trained:
        print("No trained priors found. Run 'train' first.")
        return {}

    rounds_info = api_client.my_rounds()
    completed = [r for r in rounds_info if r.get("status") == "completed"]

    alpha_values = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 25.0]
    results_by_alpha: dict[float, list[float]] = {a: [] for a in alpha_values}
    v3_scores: list[float] = []  # Global average prior (no weighting)

    for ri, round_info in enumerate(completed):
        round_id = round_info["id"]
        round_num = round_info.get("round_number", "?")

        try:
            detail = api_client.get_round_detail(round_id)
        except Exception:
            continue

        seeds_count = detail.seeds_count
        ground_truths = []
        for seed_idx in range(min(seeds_count, len(detail.initial_states))):
            try:
                analysis = api_client.analysis(round_id, seed_idx)
                ground_truths.append(np.array(analysis.ground_truth))
            except Exception:
                break

        if len(ground_truths) != seeds_count:
            continue

        # v3 baseline: global priors, no per-round weighting
        v3_score = backtest_round(
            trained, detail, ground_truths,
            alpha=3.0, use_weighted=False,
        )
        v3_scores.append(v3_score)

        # v4: per-round weighted, different α values
        for alpha in alpha_values:
            score = backtest_round(
                trained, detail, ground_truths,
                alpha=alpha, use_weighted=True,
                exclude_round_id=round_id,  # Leave-one-out
            )
            results_by_alpha[alpha].append(score)

        print(f"Round {round_num}: v3={v3_score:.1f} | "
              + " | ".join(f"a={a}:{results_by_alpha[a][-1]:.1f}" for a in alpha_values))

    # Find best alpha
    avg_by_alpha = {a: float(np.mean(scores)) if scores else 0 for a, scores in results_by_alpha.items()}
    best_alpha = max(avg_by_alpha, key=avg_by_alpha.get)
    v3_avg = float(np.mean(v3_scores)) if v3_scores else 0

    print(f"\n{'='*60}")
    print(f"v3 (global avg prior): {v3_avg:.2f}")
    for a in alpha_values:
        marker = " <-- BEST" if a == best_alpha else ""
        print(f"v4 a={a}: {avg_by_alpha[a]:.2f}{marker}")
    print(f"{'='*60}")
    print(f"Best a = {best_alpha}, improvement over v3: {avg_by_alpha[best_alpha] - v3_avg:+.2f}")

    # Save optimal alpha
    trained["optimal_alpha"] = best_alpha
    out_path = data_dir / "learned" / "trained-priors.json"
    with open(out_path, "w") as f:
        json.dump(trained, f, indent=2)
    print(f"Saved optimal a={best_alpha} to {out_path}")

    return {
        "v3_avg": v3_avg,
        "best_alpha": best_alpha,
        "best_score": avg_by_alpha[best_alpha],
        "all_scores": avg_by_alpha,
        "num_rounds": len(v3_scores),
    }


# ─── Solver class ───

class Solver:
    def __init__(self, api_client: AstarIslandApiClient, data_dir: Path | None = None) -> None:
        self.api = api_client
        self.data_dir = data_dir or Path("data")
        self.trained = load_trained_priors(self.data_dir)
        if self.trained:
            n_rounds = len(self.trained.get("rounds_included", []))
            print(f"Loaded trained priors from {n_rounds} rounds")
        else:
            print("No trained priors found, using hardcoded fallback")

    def solve(self, round_detail: RoundDetail) -> list[int]:
        """Query, predict, and submit. Always submits — never waste queries on dry runs."""
        height = round_detail.map_height
        width = round_detail.map_width
        seeds_count = round_detail.seeds_count
        queries_per_seed = 50 // seeds_count

        base_viewports = tile_viewports(height, width)
        tile_count = len(base_viewports)
        bonus_count = queries_per_seed - tile_count

        all_observations: dict[int, list[tuple[Viewport, ObservationResult]]] = {}

        # Phase 1: Full map tile
        budget_exhausted = False
        print(f"Phase 1: Tiling map with {tile_count} viewports per seed...")
        for seed_index in range(seeds_count):
            all_observations[seed_index] = []
            for vp in base_viewports:
                if budget_exhausted:
                    break
                result = self._query(round_detail.id, seed_index, vp)
                if result is None:
                    budget_exhausted = True
                    break
                all_observations[seed_index].append((vp, result))
                print(f"  Seed {seed_index}: tile ({vp.x},{vp.y}) - "
                      f"{result.queries_used}/{result.queries_max}")

        # Phase 2: Bonus viewports (targeted at dynamic areas from phase 1)
        if bonus_count > 0 and not budget_exhausted:
            print(f"Phase 2: {bonus_count} bonus viewport(s) per seed...")
            for seed_index in range(seeds_count):
                initial_state = round_detail.initial_states[seed_index]
                for _ in range(bonus_count):
                    if budget_exhausted:
                        break
                    bonus = pick_bonus_viewport(
                        initial_state, height, width, base_viewports, self.trained,
                        observations=all_observations[seed_index],
                    )
                    result = self._query(round_detail.id, seed_index, bonus)
                    if result is None:
                        budget_exhausted = True
                        break
                    all_observations[seed_index].append((bonus, result))
                    print(f"  Seed {seed_index}: bonus ({bonus.x},{bonus.y}) - "
                          f"{result.queries_used}/{result.queries_max}")

        if budget_exhausted:
            print("Budget exhausted — building predictions from priors only for remaining seeds")

        # Regime detection
        print("Detecting round regime from observations...")
        regime = compute_regime(round_detail, all_observations)
        print(f"  Survival rate: {regime['survival_rate']:.3f}")
        print(f"  Expansion rate: {regime['expansion_rate']:.3f}")
        print(f"  Port rate: {regime['port_rate']:.3f}")
        print(f"  Ruin rate: {regime['ruin_rate']:.3f}")
        if self.trained and "historical_rates" in self.trained:
            hist = self.trained["historical_rates"]
            print("  vs Historical averages:")
            for k in ["survival_rate", "expansion_rate", "port_rate", "ruin_rate"]:
                if k in hist:
                    print(f"    {k}: {regime[k]:.3f} vs {hist[k]:.3f}")

        # Build predictions
        alpha = PRIOR_STRENGTH
        if self.trained and "optimal_alpha" in self.trained:
            alpha = self.trained["optimal_alpha"]

        print(f"Building predictions (alpha={alpha})...")
        submitted: list[int] = []

        for seed_index in range(seeds_count):
            prediction = build_prediction(
                round_detail.initial_states[seed_index],
                all_observations.get(seed_index, []),
                height, width,
                regime,
                self.trained,
                alpha,
            )
            result = self.api.submit_prediction(
                round_detail.id, seed_index, prediction.tolist(),
            )
            submitted.append(seed_index)
            print(f"  Seed {seed_index}: {result.status}")

        return submitted

    def _query(self, round_id: str, seed_index: int, vp: Viewport) -> ObservationResult | None:
        try:
            return self.api.simulate(ObservationRequest(
                round_id=round_id,
                seed_index=seed_index,
                viewport_x=vp.x,
                viewport_y=vp.y,
                viewport_w=vp.w,
                viewport_h=vp.h,
            ))
        except Exception as e:
            print(f"  Query failed ({e}), skipping viewport ({vp.x},{vp.y})")
            return None
