from __future__ import annotations

import math

import numpy as np


def kl_divergence(ground_truth: np.ndarray, prediction: np.ndarray) -> np.ndarray:
    safe_ground_truth = np.where(ground_truth > 0, ground_truth, 1.0)
    safe_prediction = np.clip(prediction, 1e-12, None)
    terms = np.where(
        ground_truth > 0,
        ground_truth * np.log(safe_ground_truth / safe_prediction),
        0.0,
    )
    return terms.sum(axis=-1)


def entropy_weighted_kl(ground_truth: np.ndarray, prediction: np.ndarray) -> float:
    clipped_ground_truth = np.clip(ground_truth, 1e-12, None)
    entropy = -np.sum(np.where(ground_truth > 0, ground_truth * np.log(clipped_ground_truth), 0.0), axis=-1)
    weights = np.where(entropy > 1e-8, entropy, 0.0)
    if float(weights.sum()) == 0.0:
        return 0.0
    per_cell_kl = kl_divergence(ground_truth, prediction)
    return float((weights * per_cell_kl).sum() / weights.sum())


def seed_score(ground_truth: np.ndarray, prediction: np.ndarray) -> float:
    weighted_kl = entropy_weighted_kl(ground_truth, prediction)
    score = 100.0 * math.exp(-3.0 * weighted_kl)
    return max(0.0, min(100.0, score))


def round_score(seed_scores: list[float]) -> float:
    if not seed_scores:
        return 0.0
    return float(sum(seed_scores) / len(seed_scores))

