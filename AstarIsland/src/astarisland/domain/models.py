from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from astarisland.domain.enums import TerrainCode


class AstarBaseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class Viewport(AstarBaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)

    def cell_coordinates(self) -> list[tuple[int, int]]:
        return [
            (self.x + offset_x, self.y + offset_y)
            for offset_y in range(self.h)
            for offset_x in range(self.w)
        ]


class Settlement(AstarBaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    population: float | None = None
    food: float | None = None
    wealth: float | None = None
    defense: float | None = None
    has_port: bool = False
    alive: bool = True
    owner_id: int | None = None


class InitialState(AstarBaseModel):
    grid: list[list[int]]
    settlements: list[Settlement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rectangular_grid(self) -> "InitialState":
        if not self.grid:
            raise ValueError("initial state grid cannot be empty")
        width = len(self.grid[0])
        if width == 0:
            raise ValueError("initial state grid cannot have empty rows")
        for row in self.grid:
            if len(row) != width:
                raise ValueError("initial state grid must be rectangular")
        return self

    @computed_field
    @property
    def height(self) -> int:
        return len(self.grid)

    @computed_field
    @property
    def width(self) -> int:
        return len(self.grid[0])

    def overlay_grid(self) -> list[list[int]]:
        grid = [row[:] for row in self.grid]
        for settlement in self.settlements:
            grid[settlement.y][settlement.x] = (
                TerrainCode.PORT if settlement.has_port else TerrainCode.SETTLEMENT
            )
        return grid


class RoundSummary(AstarBaseModel):
    id: str
    round_number: int
    status: str
    map_width: int = Field(ge=1)
    map_height: int = Field(ge=1)
    prediction_window_minutes: int | None = None
    started_at: datetime | None = None
    closes_at: datetime | None = None
    event_date: str | None = None
    round_weight: float | None = None
    created_at: datetime | None = None


class RoundDetail(RoundSummary):
    seeds_count: int = Field(ge=1)
    initial_states: list[InitialState]


class BudgetStatus(AstarBaseModel):
    round_id: str
    queries_used: int = Field(ge=0)
    queries_max: int = Field(ge=0)
    active: bool

    @computed_field
    @property
    def queries_remaining(self) -> int:
        return max(self.queries_max - self.queries_used, 0)


class ObservationRequest(AstarBaseModel):
    round_id: str
    seed_index: int = Field(ge=0)
    viewport_x: int = Field(ge=0)
    viewport_y: int = Field(ge=0)
    viewport_w: int = Field(ge=5, le=15)
    viewport_h: int = Field(ge=5, le=15)

    def to_viewport(self) -> Viewport:
        return Viewport(x=self.viewport_x, y=self.viewport_y, w=self.viewport_w, h=self.viewport_h)


class ObservationResult(AstarBaseModel):
    grid: list[list[int]]
    settlements: list[Settlement] = Field(default_factory=list)
    viewport: Viewport
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    queries_used: int = Field(ge=0)
    queries_max: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_grid_shape(self) -> "ObservationResult":
        if len(self.grid) != self.viewport.h:
            raise ValueError("observation grid height must match viewport height")
        for row in self.grid:
            if len(row) != self.viewport.w:
                raise ValueError("observation grid width must match viewport width")
        return self


class RecordedObservation(AstarBaseModel):
    round_id: str
    seed_index: int = Field(ge=0)
    phase: Literal["A", "B", "C"]
    request: ObservationRequest
    result: ObservationResult
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    observation_file: str | None = None

    @computed_field
    @property
    def window_key(self) -> str:
        viewport = self.request.to_viewport()
        return f"{self.seed_index}:{viewport.x}:{viewport.y}:{viewport.w}:{viewport.h}"


class ObservationPlanItem(AstarBaseModel):
    phase: Literal["A", "B", "C"]
    seed_index: int = Field(ge=0)
    viewport: Viewport
    saliency_score: float = Field(default=0.0, ge=0.0)
    rationale: str
    executed: bool = False
    observation_file: str | None = None

    @computed_field
    @property
    def request(self) -> ObservationRequest:
        return ObservationRequest(
            round_id="",
            seed_index=self.seed_index,
            viewport_x=self.viewport.x,
            viewport_y=self.viewport.y,
            viewport_w=self.viewport.w,
            viewport_h=self.viewport.h,
        )


class ObservationPlan(AstarBaseModel):
    round_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    planner_version: str = "v2-dynamic-frontier"
    adaptive_phase_c: bool = True
    budget_total: int = 50
    phase_budgets: dict[str, int] = Field(default_factory=lambda: {"A": 20, "B": 20, "C": 10})
    items: list[ObservationPlanItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def items_for_phase(self, phase: Literal["A", "B", "C"]) -> list[ObservationPlanItem]:
        return [item for item in self.items if item.phase == phase]

    def executed_count(self) -> int:
        return sum(1 for item in self.items if item.executed)


class SubmissionResult(AstarBaseModel):
    status: str
    round_id: str
    seed_index: int = Field(ge=0)


class AnalysisResult(AstarBaseModel):
    prediction: list[list[list[float]]] | None = None
    ground_truth: list[list[list[float]]]
    score: float | None = None
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    initial_grid: list[list[int]] | None = None


class MyPredictionSummary(AstarBaseModel):
    seed_index: int = Field(ge=0)
    argmax_grid: list[list[int]]
    confidence_grid: list[list[float]]
    score: float | None = None
    submitted_at: datetime | None = None


class WindowDynamicsSummary(AstarBaseModel):
    round_id: str
    seed_index: int = Field(ge=0)
    window_key: str
    viewport: Viewport
    repeat_count: int = Field(ge=1)
    unique_covered_cells: int = Field(ge=1)
    repeated_window_variance: float = Field(ge=0.0)
    frontier_density: float = Field(ge=0.0)
    settlement_count_mean: float = Field(ge=0.0)
    port_count_mean: float = Field(ge=0.0)
    ruin_count_mean: float = Field(ge=0.0)
    owner_diversity_mean: float = Field(ge=0.0)
    population_mean: float = Field(ge=0.0)
    population_max: float = Field(ge=0.0)
    food_mean: float = Field(ge=0.0)
    wealth_mean: float = Field(ge=0.0)
    defense_mean: float = Field(ge=0.0)


class CalibrationBin(AstarBaseModel):
    lower: float
    upper: float
    accuracy: float = Field(ge=0.0)
    avg_confidence: float = Field(ge=0.0)
    count: int = Field(ge=0)
    recommended_temperature: float = Field(ge=1.0)


class CalibrationProfile(AstarBaseModel):
    name: str
    bins: list[CalibrationBin] = Field(default_factory=list)


class FrontierPriorBucket(AstarBaseModel):
    counts: list[float]
    evidence: float = Field(ge=0.0)
    entropy_total: float = Field(ge=0.0)
    dynamic_mass_total: float = Field(ge=0.0)
    hotspot_total: float = Field(ge=0.0)
    active_frontier_total: float = Field(default=0.0, ge=0.0)
    settlement_mass_total: float = Field(ge=0.0)
    port_mass_total: float = Field(ge=0.0)
    ruin_mass_total: float = Field(ge=0.0)

    @computed_field
    @property
    def mean_distribution(self) -> list[float]:
        total = sum(self.counts)
        if total <= 0.0:
            return [1.0 / 6.0] * 6
        return [value / total for value in self.counts]

    @computed_field
    @property
    def mean_entropy(self) -> float:
        if self.evidence <= 0.0:
            return 0.0
        return self.entropy_total / self.evidence

    @computed_field
    @property
    def mean_dynamic_mass(self) -> float:
        if self.evidence <= 0.0:
            return 0.0
        return self.dynamic_mass_total / self.evidence

    @computed_field
    @property
    def mean_hotspot_score(self) -> float:
        if self.evidence <= 0.0:
            return 0.0
        return self.hotspot_total / self.evidence

    @computed_field
    @property
    def mean_active_frontier_rate(self) -> float:
        if self.evidence <= 0.0:
            return 0.0
        return self.active_frontier_total / self.evidence


class FrontierPriorTable(AstarBaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rounds_included: list[str] = Field(default_factory=list)
    buckets: dict[str, FrontierPriorBucket] = Field(default_factory=dict)
    calibration_profiles: dict[str, CalibrationProfile] = Field(default_factory=dict)


class RoundReplayDiagnostics(AstarBaseModel):
    round_id: str
    model_version: str
    average_score: float = Field(ge=0.0)
    seed_scores: list[float] = Field(default_factory=list)
    mean_dynamic_mass_gap: float = 0.0
    mean_high_entropy_port_gap: float = 0.0
    max_seed_dynamic_mass_error: float = Field(ge=0.0)


class ModelBundleMetadata(AstarBaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_version: str
    rounds_included: list[str] = Field(default_factory=list)
    feature_names: list[str] = Field(default_factory=list)
    training_examples: int = Field(default=0, ge=0)
    gate_examples: int = Field(default=0, ge=0)
    replay_diagnostics: list[RoundReplayDiagnostics] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TrainingExample:
    round_id: str
    seed_index: int
    x: int
    y: int
    features: tuple[float, ...]
    active_frontier_target: int
    dynamic_mass_target: float
    dynamic_class_target: int
    static_class_target: int
    hard_negative_candidate: bool


@dataclass(frozen=True, slots=True)
class ModelBundle:
    model_version: str
    trained_rounds: tuple[str, ...]
    feature_names: tuple[str, ...]
    global_dynamic_mass_mean: float
    global_active_frontier_rate: float
    gate_model: Any
    dynamic_mass_model: Any
    dynamic_split_model: Any
    static_split_model: Any


class ExperimentEntry(AstarBaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: Literal["observe", "predict", "submit", "analyze"]
    round_id: str
    hypothesis: str
    query_allocation: str
    model_version: str
    outcome: str
    score: float | None = None
    do_not_repeat: str
    notes: list[str] = Field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"### {self.timestamp.isoformat()} | {self.event_type} | round {self.round_id}",
            f"- Hypothesis: {self.hypothesis}",
            f"- Query allocation: {self.query_allocation}",
            f"- Model version: {self.model_version}",
            f"- Outcome: {self.outcome}",
            f"- Score: {self.score if self.score is not None else 'pending'}",
            f"- Do not repeat: {self.do_not_repeat}",
        ]
        for note in self.notes:
            lines.append(f"- Note: {note}")
        return "\n".join(lines)


def coerce_prediction_rows(prediction: Sequence[Sequence[Sequence[float]]]) -> list[list[list[float]]]:
    return [[list(cell) for cell in row] for row in prediction]
