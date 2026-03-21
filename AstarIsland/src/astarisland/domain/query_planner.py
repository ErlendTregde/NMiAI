from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astarisland.domain.frontier import build_frontier_feature_catalog, viewport_frontier_score
from astarisland.domain.models import ObservationPlan, ObservationPlanItem, RoundDetail, Viewport

VIEWPORT_SIZE = 15
PHASE_A_PER_SEED = 4
PHASE_B_PER_SEED = 4
PHASE_C_TOTAL = 10


@dataclass(frozen=True, slots=True)
class WindowScore:
    viewport: Viewport
    saliency: float
    rationale: str


def enumerate_candidate_windows(width: int, height: int) -> list[Viewport]:
    candidates: list[Viewport] = []
    max_x = max(width - VIEWPORT_SIZE, 0)
    max_y = max(height - VIEWPORT_SIZE, 0)
    for y in range(max_y + 1):
        for x in range(max_x + 1):
            candidates.append(Viewport(x=x, y=y, w=VIEWPORT_SIZE, h=VIEWPORT_SIZE))
    return candidates


def rank_candidate_windows(
    *,
    catalog,
    width: int,
    height: int,
    category: str,
    coverage_mask: np.ndarray | None = None,
) -> list[WindowScore]:
    candidates: list[WindowScore] = []
    for viewport in enumerate_candidate_windows(width, height):
        score, rationale = viewport_frontier_score(catalog, viewport, category=category, coverage_mask=coverage_mask)
        candidates.append(WindowScore(viewport=viewport, saliency=score, rationale=rationale))
    candidates.sort(key=lambda item: item.saliency, reverse=True)
    return candidates


def choose_diverse_window(candidates: list[WindowScore], selected: list[Viewport]) -> WindowScore:
    if not candidates:
        raise ValueError("No candidate windows available")
    selected_cells = {cell for viewport in selected for cell in viewport.cell_coordinates()}
    for candidate in candidates:
        candidate_cells = set(candidate.viewport.cell_coordinates())
        uncovered = len(candidate_cells - selected_cells)
        if not selected or uncovered >= 65:
            return candidate
    return candidates[0]


def plan_initial_observations(round_detail: RoundDetail) -> ObservationPlan:
    items: list[ObservationPlanItem] = []
    notes = [
        "Phase A contains 20 fixed queries, 4 per seed in ordered corridor categories including a hard-negative corridor.",
        "Phase B repeats every Phase A window once to estimate stochastic transitions and settlement-stat volatility.",
        "Phase C is reserved for 10 adaptive windows split between positive-frontier follow-ups and hard-negative checks.",
    ]

    category_order = [
        ("inland_growth", "Best inland growth corridor"),
        ("coastal_port", "Best coastal and port corridor"),
        ("ruin_rebuild", "Best ruin and rebuild corridor"),
        ("hard_negative", "Best hard-negative corridor"),
    ]

    for seed_index, initial_state in enumerate(round_detail.initial_states):
        catalog = build_frontier_feature_catalog(initial_state, observations=[], window_summaries={}, historical_priors=None)
        selected_viewports: list[Viewport] = []
        coverage_mask = np.zeros((round_detail.map_height, round_detail.map_width), dtype=bool)
        for category, label in category_order:
            candidates = rank_candidate_windows(
                catalog=catalog,
                width=round_detail.map_width,
                height=round_detail.map_height,
                category=category,
                coverage_mask=coverage_mask,
            )
            candidate = choose_diverse_window(candidates, selected_viewports)
            selected_viewports.append(candidate.viewport)
            for x, y in candidate.viewport.cell_coordinates():
                coverage_mask[y, x] = True
            items.append(
                ObservationPlanItem(
                    phase="A",
                    seed_index=seed_index,
                    viewport=candidate.viewport,
                    saliency_score=round(candidate.saliency, 3),
                    rationale=f"{label} for seed {seed_index}: {candidate.rationale}",
                )
            )

    return ObservationPlan(
        round_id=round_detail.id,
        planner_version="v4-regime-calibrated-heuristic",
        phase_budgets={"A": 20, "B": 20, "C": 10},
        items=items,
        notes=notes,
    )
