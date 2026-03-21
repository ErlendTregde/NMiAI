from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime

import numpy as np

from astarisland.domain.frontier import build_frontier_feature_catalog, build_window_dynamics_summaries, viewport_frontier_score
from astarisland.domain.models import ObservationPlan, ObservationPlanItem, ObservationRequest, RecordedObservation, RoundDetail, Viewport
from astarisland.domain.query_planner import PHASE_C_TOTAL, enumerate_candidate_windows, plan_initial_observations
from astarisland.infrastructure.api_client import AstarIslandApiClient
from astarisland.infrastructure.artifact_store import ArtifactStore


def _matching_record(record: RecordedObservation, item: ObservationPlanItem) -> bool:
    viewport = item.viewport
    request = record.request
    return (
        record.phase == item.phase
        and record.seed_index == item.seed_index
        and request.viewport_x == viewport.x
        and request.viewport_y == viewport.y
        and request.viewport_w == viewport.w
        and request.viewport_h == viewport.h
    )


def _overlap_with_selected(viewport: Viewport, selected: list[Viewport]) -> int:
    selected_cells = {cell for candidate in selected for cell in candidate.cell_coordinates()}
    return len(set(viewport.cell_coordinates()) & selected_cells)


def _viewport_key(viewport: Viewport) -> tuple[int, int, int, int]:
    return (viewport.x, viewport.y, viewport.w, viewport.h)


class ObservationWorkflow:
    def __init__(self, api_client: AstarIslandApiClient, artifact_store: ArtifactStore) -> None:
        self.api_client = api_client
        self.artifact_store = artifact_store

    def create_plan(self, round_detail: RoundDetail) -> ObservationPlan:
        plan = plan_initial_observations(round_detail)
        self.artifact_store.save_observation_plan(plan)
        markdown = self._plan_to_markdown(plan)
        self.artifact_store.save_plan_summary_markdown(round_detail.id, markdown)
        return plan

    def run_plan(self, round_detail: RoundDetail, existing_plan: ObservationPlan | None = None) -> ObservationPlan:
        plan = existing_plan or self.create_plan(round_detail)
        executed_records = self.artifact_store.load_observations(round_detail.id)
        self._sync_plan_with_records(plan, executed_records)

        self._execute_pending_items(round_detail.id, plan.items_for_phase("A"), plan, executed_records)

        if not plan.items_for_phase("B"):
            plan.items.extend(self._build_phase_b_items(plan))
        self._sync_plan_with_records(plan, executed_records)
        self._execute_pending_items(round_detail.id, plan.items_for_phase("B"), plan, executed_records)

        window_summaries = build_window_dynamics_summaries(round_detail.id, executed_records)
        self.artifact_store.save_window_dynamics_summaries(round_detail.id, window_summaries)

        if not plan.items_for_phase("C"):
            plan.items.extend(self._build_phase_c_items(round_detail, executed_records, window_summaries))
        self._sync_plan_with_records(plan, executed_records)
        self._execute_pending_items(round_detail.id, plan.items_for_phase("C"), plan, executed_records)

        window_summaries = build_window_dynamics_summaries(round_detail.id, executed_records)
        self.artifact_store.save_window_dynamics_summaries(round_detail.id, window_summaries)
        self.artifact_store.save_observation_plan(plan)
        summary = self._run_summary_markdown(plan, executed_records, window_summaries)
        self.artifact_store.save_markdown_summary(round_detail.id, "observation-run", summary)
        return plan

    def _sync_plan_with_records(self, plan: ObservationPlan, executed_records: list[RecordedObservation]) -> None:
        updated_items: list[ObservationPlanItem] = []
        for item in plan.items:
            if item.executed:
                updated_items.append(item)
                continue
            matching = next((record for record in reversed(executed_records) if _matching_record(record, item)), None)
            if matching is None:
                updated_items.append(item)
                continue
            updated_items.append(
                item.model_copy(
                    update={
                        "executed": True,
                        "observation_file": matching.observation_file,
                    }
                )
            )
        plan.items[:] = updated_items

    def _execute_pending_items(
        self,
        round_id: str,
        items: list[ObservationPlanItem],
        plan: ObservationPlan,
        executed_records: list[RecordedObservation],
    ) -> None:
        for item in items:
            if item.executed:
                continue
            record = self._execute_item(round_id, item)
            path = self.artifact_store.save_observation(record)
            record = record.model_copy(update={"observation_file": str(path)})
            self._mark_item_executed(plan, item, str(path))
            executed_records.append(record)

    def _execute_item(self, round_id: str, item: ObservationPlanItem) -> RecordedObservation:
        request = ObservationRequest(
            round_id=round_id,
            seed_index=item.seed_index,
            viewport_x=item.viewport.x,
            viewport_y=item.viewport.y,
            viewport_w=item.viewport.w,
            viewport_h=item.viewport.h,
        )
        result = self.api_client.simulate(request)
        return RecordedObservation(
            round_id=round_id,
            seed_index=item.seed_index,
            phase=item.phase,
            request=request,
            result=result,
            executed_at=datetime.now(UTC),
        )

    def _mark_item_executed(self, plan: ObservationPlan, item: ObservationPlanItem, observation_file: str) -> None:
        for index, candidate in enumerate(plan.items):
            if candidate is item:
                plan.items[index] = item.model_copy(update={"executed": True, "observation_file": observation_file})
                return
        for index, candidate in enumerate(plan.items):
            if candidate == item and not candidate.executed:
                plan.items[index] = item.model_copy(update={"executed": True, "observation_file": observation_file})
                return
        raise ValueError("Could not find observation plan item to mark as executed")

    def _build_phase_b_items(self, plan: ObservationPlan) -> list[ObservationPlanItem]:
        items: list[ObservationPlanItem] = []
        for item in plan.items_for_phase("A"):
            items.append(
                ObservationPlanItem(
                    phase="B",
                    seed_index=item.seed_index,
                    viewport=item.viewport,
                    saliency_score=item.saliency_score,
                    rationale=f"Repeat Phase A corridor window once for variance and settlement-stat volatility: {item.rationale}",
                )
            )
        return items

    def _build_phase_c_items(
        self,
        round_detail: RoundDetail,
        records: list[RecordedObservation],
        window_summaries,
    ) -> list[ObservationPlanItem]:
        per_seed_records: dict[int, list[RecordedObservation]] = defaultdict(list)
        for record in records:
            per_seed_records[record.seed_index].append(record)

        historical_priors = self.artifact_store.load_aggregated_frontier_prior_table()
        used_viewports: dict[int, list[Viewport]] = defaultdict(list)
        positive_pool: list[ObservationPlanItem] = []
        hard_negative_pool: list[ObservationPlanItem] = []
        unique_coverage_by_seed: dict[int, int] = {}

        for seed_index, initial_state in enumerate(round_detail.initial_states):
            seed_records = per_seed_records.get(seed_index, [])
            coverage_mask = np.zeros((round_detail.map_height, round_detail.map_width), dtype=bool)
            for record in seed_records:
                viewport = record.request.to_viewport()
                used_viewports[seed_index].append(viewport)
                for x, y in viewport.cell_coordinates():
                    coverage_mask[y, x] = True
            unique_coverage_by_seed[seed_index] = int(coverage_mask.sum())

            catalog = build_frontier_feature_catalog(
                initial_state,
                observations=seed_records,
                window_summaries=window_summaries,
                historical_priors=historical_priors,
            )

            for viewport in enumerate_candidate_windows(round_detail.map_width, round_detail.map_height):
                if any(candidate == viewport for candidate in used_viewports[seed_index]):
                    continue
                positive_score, positive_rationale = viewport_frontier_score(
                    catalog,
                    viewport,
                    category="adaptive_positive",
                    coverage_mask=coverage_mask,
                )
                if positive_score > 0.0:
                    positive_pool.append(
                        ObservationPlanItem(
                            phase="C",
                            seed_index=seed_index,
                            viewport=viewport,
                            saliency_score=round(positive_score, 3),
                            rationale=f"Adaptive positive-frontier follow-up: {positive_rationale}",
                        )
                    )

                negative_score, negative_rationale = viewport_frontier_score(
                    catalog,
                    viewport,
                    category="adaptive_negative",
                    coverage_mask=coverage_mask,
                )
                if negative_score > 0.0:
                    hard_negative_pool.append(
                        ObservationPlanItem(
                            phase="C",
                            seed_index=seed_index,
                            viewport=viewport,
                            saliency_score=round(negative_score, 3),
                            rationale=f"Adaptive hard-negative check: {negative_rationale}",
                        )
                    )

        positive_pool.sort(key=lambda item: item.saliency_score, reverse=True)
        hard_negative_pool.sort(key=lambda item: item.saliency_score, reverse=True)
        phase_c_items: list[ObservationPlanItem] = []
        per_seed_counts: Counter[int] = Counter()
        selected_per_seed: dict[int, list[Viewport]] = defaultdict(list)
        selected_keys: set[tuple[int, int, int, int, int]] = set()
        priority_seeds = {seed_index for seed_index, count in unique_coverage_by_seed.items() if count < 1150}

        phase_c_items.extend(
            self._select_phase_c_candidates(
                pool=positive_pool,
                total=5,
                per_seed_counts=per_seed_counts,
                selected_per_seed=selected_per_seed,
                used_viewports=used_viewports,
                selected_keys=selected_keys,
                priority_seeds=priority_seeds,
            )
        )
        phase_c_items.extend(
            self._select_phase_c_candidates(
                pool=hard_negative_pool,
                total=5,
                per_seed_counts=per_seed_counts,
                selected_per_seed=selected_per_seed,
                used_viewports=used_viewports,
                selected_keys=selected_keys,
                priority_seeds=priority_seeds,
            )
        )

        if len(phase_c_items) < PHASE_C_TOTAL:
            fallback_pool = sorted(
                [
                    *positive_pool,
                    *hard_negative_pool,
                ],
                key=lambda item: item.saliency_score,
                reverse=True,
            )
            phase_c_items.extend(
                self._select_phase_c_candidates(
                    pool=fallback_pool,
                    total=PHASE_C_TOTAL - len(phase_c_items),
                    per_seed_counts=per_seed_counts,
                    selected_per_seed=selected_per_seed,
                    used_viewports=used_viewports,
                    selected_keys=selected_keys,
                    priority_seeds=priority_seeds,
                    enforce_split=False,
                )
            )

        return phase_c_items

    def _select_phase_c_candidates(
        self,
        *,
        pool: list[ObservationPlanItem],
        total: int,
        per_seed_counts: Counter[int],
        selected_per_seed: dict[int, list[Viewport]],
        used_viewports: dict[int, list[Viewport]],
        selected_keys: set[tuple[int, int, int, int, int]],
        priority_seeds: set[int],
        enforce_split: bool = True,
    ) -> list[ObservationPlanItem]:
        selected: list[ObservationPlanItem] = []
        for enforce_priority in (True, False):
            for item in pool:
                if len(selected) == total:
                    return selected
                item_key = (item.seed_index, *_viewport_key(item.viewport))
                if item_key in selected_keys:
                    continue
                seed_index = item.seed_index
                if per_seed_counts[seed_index] >= 3:
                    continue
                if enforce_priority and priority_seeds and per_seed_counts[seed_index] >= 2:
                    if any(per_seed_counts[priority_seed] == 0 for priority_seed in priority_seeds):
                        continue
                overlap = _overlap_with_selected(item.viewport, used_viewports[seed_index] + selected_per_seed[seed_index])
                if overlap > 175:
                    continue
                selected.append(item)
                selected_keys.add(item_key)
                selected_per_seed[seed_index].append(item.viewport)
                per_seed_counts[seed_index] += 1
            if selected or not enforce_split:
                continue
        return selected

    def _plan_to_markdown(self, plan: ObservationPlan) -> str:
        lines = [
            f"# Observation Plan for Round {plan.round_id}",
            "",
            f"- Planner version: {plan.planner_version}",
            f"- Total budget: {plan.budget_total}",
            f"- Phase budgets: {plan.phase_budgets}",
            "",
        ]
        for phase in ("A", "B", "C"):
            lines.extend([f"## Phase {phase}", ""])
            for item in plan.items_for_phase(phase):
                lines.append(
                    f"- Seed {item.seed_index}: x={item.viewport.x}, y={item.viewport.y}, "
                    f"w={item.viewport.w}, h={item.viewport.h}, saliency={item.saliency_score:.2f}, rationale={item.rationale}"
                )
            lines.append("")
        lines.extend(["## Notes", ""])
        lines.extend(f"- {note}" for note in plan.notes)
        return "\n".join(lines)

    def _run_summary_markdown(
        self,
        plan: ObservationPlan,
        records: list[RecordedObservation],
        window_summaries,
    ) -> str:
        lines = [
            f"# Observation Run Summary for Round {plan.round_id}",
            "",
            f"- Executed queries: {len(records)}",
            f"- Phase A executed: {len([record for record in records if record.phase == 'A'])}",
            f"- Phase B executed: {len([record for record in records if record.phase == 'B'])}",
            f"- Phase C executed: {len([record for record in records if record.phase == 'C'])}",
            f"- Phase C positive-frontier windows: {sum(1 for item in plan.items_for_phase('C') if 'positive-frontier' in item.rationale)}",
            f"- Phase C hard-negative windows: {sum(1 for item in plan.items_for_phase('C') if 'hard-negative' in item.rationale)}",
            "",
            "## Seed Coverage",
            "",
        ]
        by_seed: Counter[int] = Counter(record.seed_index for record in records)
        for seed_index, count in sorted(by_seed.items()):
            unique_cells = len(
                {
                    (x, y)
                    for record in records
                    if record.seed_index == seed_index
                    for x, y in record.request.to_viewport().cell_coordinates()
                }
            )
            lines.append(f"- Seed {seed_index}: {count} observations, {unique_cells} unique covered cells")

        lines.extend(["", "## Window Dynamics", ""])
        ranked_summaries = sorted(
            window_summaries.values(),
            key=lambda summary: (summary.repeated_window_variance, summary.frontier_density, summary.population_max),
            reverse=True,
        )
        for summary in ranked_summaries[: min(10, len(ranked_summaries))]:
            lines.append(
                "- "
                f"Seed {summary.seed_index} {summary.window_key}: repeats={summary.repeat_count}, "
                f"variance={summary.repeated_window_variance:.3f}, frontier_density={summary.frontier_density:.3f}, "
                f"settlements={summary.settlement_count_mean:.2f}, ports={summary.port_count_mean:.2f}, "
                f"owner_diversity={summary.owner_diversity_mean:.2f}, pop_mean={summary.population_mean:.2f}, "
                f"food_mean={summary.food_mean:.2f}, wealth_mean={summary.wealth_mean:.3f}, defense_mean={summary.defense_mean:.2f}"
            )
        return "\n".join(lines)
