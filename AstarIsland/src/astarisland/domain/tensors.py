from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astarisland.domain.enums import PredictionClass

MIN_PROBABILITY_FLOOR = 0.01
UNIFORM_BLEND_WEIGHT = 0.03
CLASS_COUNT = len(PredictionClass)


@dataclass(frozen=True, slots=True)
class PredictionTensor:
    seed_index: int
    values: np.ndarray

    def __post_init__(self) -> None:
        if self.values.ndim != 3:
            raise ValueError("prediction tensor must be 3D")
        if self.values.shape[-1] != CLASS_COUNT:
            raise ValueError("prediction tensor must have 6 classes per cell")

    @property
    def height(self) -> int:
        return int(self.values.shape[0])

    @property
    def width(self) -> int:
        return int(self.values.shape[1])

    def tolist(self) -> list[list[list[float]]]:
        return self.values.tolist()

    @classmethod
    def uniform(cls, seed_index: int, height: int, width: int) -> "PredictionTensor":
        values = np.full((height, width, CLASS_COUNT), 1.0 / CLASS_COUNT, dtype=float)
        return cls(seed_index=seed_index, values=values)


def soft_mask_for_cell(initial_terrain: int, is_coastal: bool, initial_port: bool) -> np.ndarray:
    mask = np.ones(CLASS_COUNT, dtype=float)

    if initial_terrain == 5:
        mask *= np.array([0.02, 0.01, 0.01, 0.01, 0.01, 0.94], dtype=float)
    elif initial_terrain == 10:
        mask *= np.array([0.96, 0.01, 0.01, 0.005, 0.005, 0.01], dtype=float)
    elif initial_terrain == 4:
        mask *= np.array([0.22, 0.12, 0.02, 0.08, 0.54, 0.02], dtype=float)
    elif initial_terrain == 3:
        mask *= np.array([0.14, 0.15, 0.05, 0.52, 0.12, 0.02], dtype=float)
    elif initial_terrain == 2 or initial_port:
        mask *= np.array([0.09, 0.20, 0.48, 0.15, 0.06, 0.02], dtype=float)
    elif initial_terrain == 1:
        mask *= np.array([0.10, 0.45, 0.17, 0.20, 0.06, 0.02], dtype=float)
    else:
        mask *= np.array([0.73, 0.10, 0.04, 0.06, 0.05, 0.02], dtype=float)

    if not is_coastal:
        mask[int(PredictionClass.PORT)] *= 0.2

    return mask / mask.sum()


def renormalize_probabilities(values: np.ndarray) -> np.ndarray:
    totals = values.sum(axis=-1, keepdims=True)
    totals[totals == 0] = 1.0
    return values / totals


def apply_probability_floor(values: np.ndarray, floor: float = MIN_PROBABILITY_FLOOR) -> np.ndarray:
    if floor * CLASS_COUNT >= 1.0:
        raise ValueError("probability floor is too large for the number of classes")
    normalized = renormalize_probabilities(np.clip(values, 0.0, None))
    remainder = 1.0 - floor * CLASS_COUNT
    return floor + remainder * normalized


def calibrate_prediction_tensor(tensor: PredictionTensor) -> PredictionTensor:
    values = tensor.values.astype(float, copy=True)
    uniform = np.full_like(values, 1.0 / CLASS_COUNT)
    values = (1.0 - UNIFORM_BLEND_WEIGHT) * values + UNIFORM_BLEND_WEIGHT * uniform
    values = apply_probability_floor(values, MIN_PROBABILITY_FLOOR)
    validate_prediction_tensor(values)
    return PredictionTensor(seed_index=tensor.seed_index, values=values)


def validate_prediction_tensor(values: np.ndarray, tolerance: float = 0.01) -> None:
    if values.ndim != 3:
        raise ValueError("prediction tensor must be rank-3")
    if values.shape[-1] != CLASS_COUNT:
        raise ValueError("prediction tensor must end with 6 classes")
    if np.any(values < 0):
        raise ValueError("prediction tensor cannot contain negative probabilities")
    sums = values.sum(axis=-1)
    if not np.allclose(sums, 1.0, atol=tolerance):
        raise ValueError("prediction tensor cells must sum to 1.0")
