from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from astarisland.domain.models import ObservationRequest, ObservationResult, RecordedObservation, RoundDetail, Viewport
from astarisland.domain.tensors import PredictionTensor
from astarisland.infrastructure.artifact_store import ArtifactStore
from astarisland.infrastructure.json_io import load_json_file


@pytest.fixture()
def fixture_round_detail() -> RoundDetail:
    payload = load_json_file(Path(__file__).parent / "fixtures" / "round_detail.json")
    return RoundDetail.model_validate(payload)


@pytest.fixture()
def artifact_store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "data")


def _mutate_overlay_grid(overlay_grid: list[list[int]], seed_index: int, variant: int) -> list[list[int]]:
    grid = [row[:] for row in overlay_grid]
    variant_cells = [
        (2 + seed_index % 3, 2 + variant % 3),
        (7, 7),
        (10, 10 - (seed_index % 2)),
    ]
    for x, y in variant_cells:
        if 0 <= y < len(grid) and 0 <= x < len(grid[0]) and grid[y][x] not in {5, 10}:
            if variant == 0:
                grid[y][x] = 1
            elif variant == 1:
                grid[y][x] = 3
            else:
                grid[y][x] = 4

    if variant == 1 and 1 + seed_index < len(grid[0]) - 1:
        grid[1 + seed_index % 5][1 + seed_index] = 2
    return grid


def make_mock_observations(round_detail: RoundDetail, *, runs_per_seed: int = 3) -> list[RecordedObservation]:
    records: list[RecordedObservation] = []
    base_time = datetime(2026, 3, 19, 10, 0, tzinfo=UTC)
    for seed_index, initial_state in enumerate(round_detail.initial_states):
        overlay = initial_state.overlay_grid()
        for variant in range(runs_per_seed):
            grid = _mutate_overlay_grid(overlay, seed_index, variant)
            request = ObservationRequest(
                round_id=round_detail.id,
                seed_index=seed_index,
                viewport_x=0,
                viewport_y=0,
                viewport_w=round_detail.map_width,
                viewport_h=round_detail.map_height,
            )
            result = ObservationResult(
                grid=grid,
                settlements=initial_state.settlements,
                viewport=Viewport(x=0, y=0, w=round_detail.map_width, h=round_detail.map_height),
                width=round_detail.map_width,
                height=round_detail.map_height,
                queries_used=variant + 1,
                queries_max=50,
            )
            records.append(
                RecordedObservation(
                    round_id=round_detail.id,
                    seed_index=seed_index,
                    phase="A" if variant == 0 else "B" if variant == 1 else "C",
                    request=request,
                    result=result,
                    executed_at=base_time + timedelta(minutes=(seed_index * 10 + variant)),
                )
            )
    return records


def ground_truth_from_observations(records: list[RecordedObservation], *, seed_index: int, height: int, width: int) -> np.ndarray:
    counts = np.zeros((height, width, 6), dtype=float)
    for record in records:
        if record.seed_index != seed_index:
            continue
        for y, row in enumerate(record.result.grid):
            for x, value in enumerate(row):
                if value in {0, 10, 11}:
                    counts[y, x, 0] += 1.0
                else:
                    counts[y, x, int(value)] += 1.0
    counts = counts + 0.05
    counts = counts / counts.sum(axis=-1, keepdims=True)
    return counts


def uniform_prediction(height: int, width: int, *, seed_index: int = 0) -> PredictionTensor:
    return PredictionTensor.uniform(seed_index=seed_index, height=height, width=width)

