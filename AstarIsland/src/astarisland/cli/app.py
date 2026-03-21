from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from astarisland.application import (
    AnalysisWorkflow,
    ObservationWorkflow,
    PredictionWorkflow,
    RoundWorkflow,
    SubmissionWorkflow,
    append_experiment_entry,
)
from astarisland.domain.models import ExperimentEntry, RoundDetail
from astarisland.infrastructure import AstarIslandApiClient, ArtifactStore, MarkdownExperimentLog, Settings

app = typer.Typer(help="Astar Island CLI dynamic frontier client")
round_app = typer.Typer(help="Round discovery and metadata")
observe_app = typer.Typer(help="Observation planning and execution")
app.add_typer(round_app, name="round")
app.add_typer(observe_app, name="observe")

console = Console()


def _settings() -> Settings:
    return Settings()


def _artifact_store(settings: Settings) -> ArtifactStore:
    return ArtifactStore(settings.data_dir)


def _experiment_log(settings: Settings) -> MarkdownExperimentLog:
    return MarkdownExperimentLog(settings.documentation_path)


def _load_round_detail(settings: Settings, round_id: str, *, fetch_if_missing: bool = True) -> RoundDetail:
    store = _artifact_store(settings)
    try:
        return store.load_round_detail(round_id)
    except FileNotFoundError:
        if not fetch_if_missing:
            raise
        with AstarIslandApiClient(settings) as api_client:
            workflow = RoundWorkflow(api_client, store)
            return workflow.get_round_detail(round_id)


def _require_token(settings: Settings) -> None:
    if not settings.access_token:
        raise typer.BadParameter("ASTAR_ACCESS_TOKEN is required for this command.")


@round_app.command("active")
def round_active() -> None:
    """Fetch and save the active round detail."""

    settings = _settings()
    store = _artifact_store(settings)
    with AstarIslandApiClient(settings) as api_client:
        workflow = RoundWorkflow(api_client, store)
        summary, detail = workflow.get_active_round()

    table = Table(title="Active Round")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Round", str(summary.round_number))
    table.add_row("Round ID", summary.id)
    table.add_row("Status", summary.status)
    table.add_row("Map", f"{detail.map_width}x{detail.map_height}")
    table.add_row("Seeds", str(detail.seeds_count))
    table.add_row("Closes", detail.closes_at.isoformat() if detail.closes_at else "unknown")
    console.print(table)


@observe_app.command("plan")
def observe_plan(round_id: str = typer.Option(..., "--round-id")) -> None:
    """Create and save the initial observation plan."""

    settings = _settings()
    round_detail = _load_round_detail(settings, round_id)
    workflow = ObservationWorkflow(AstarIslandApiClient(settings), _artifact_store(settings))
    try:
        plan = workflow.create_plan(round_detail)
    finally:
        workflow.api_client.close()
    console.print(f"Saved observation plan for round {plan.round_id} with {len(plan.items)} Phase A queries.")


@observe_app.command("run")
def observe_run(round_id: str = typer.Option(..., "--round-id")) -> None:
    """Run the planned observation campaign against the API."""

    settings = _settings()
    _require_token(settings)
    store = _artifact_store(settings)
    experiment_log = _experiment_log(settings)
    round_detail = _load_round_detail(settings, round_id)

    with AstarIslandApiClient(settings) as api_client:
        workflow = ObservationWorkflow(api_client, store)
        try:
            plan = store.load_observation_plan(round_id)
        except FileNotFoundError:
            plan = workflow.create_plan(round_detail)
        final_plan = workflow.run_plan(round_detail, existing_plan=plan)

    append_experiment_entry(
        experiment_log,
        ExperimentEntry(
            event_type="observe",
            round_id=round_id,
            hypothesis="Active-frontier coverage with exact repeats, positive follow-ups, and hard-negative checks should reduce false dynamic predictions while preserving dynamic-cell recall.",
            query_allocation="Phase A 20 / Phase B 20 / Phase C 10",
            model_version=final_plan.planner_version,
            outcome=f"Executed {final_plan.executed_count()} observations and saved raw payloads under data/rounds/{round_id}/observations/raw.",
            do_not_repeat="Do not spend all adaptive windows on frontier-positive guesses; the fitted model also needs hard-negative evidence to suppress false dynamic mass.",
            notes=[
                "Observation summary saved under summaries/observation-run.md",
                "Window dynamics summaries saved under observations/window-dynamics.json",
            ],
        ),
    )
    console.print(f"Completed observation run for round {round_id}.")


@app.command("predict")
def predict(round_id: str = typer.Option(..., "--round-id")) -> None:
    """Build prediction tensors from saved observations."""

    settings = _settings()
    store = _artifact_store(settings)
    experiment_log = _experiment_log(settings)
    round_detail = _load_round_detail(settings, round_id)

    workflow = PredictionWorkflow(store)
    predictions = workflow.build_predictions(round_detail)

    append_experiment_entry(
        experiment_log,
        ExperimentEntry(
            event_type="predict",
            round_id=round_id,
            hypothesis="A regime-based heuristic should shrink total dynamic mass toward observed and learned targets while suppressing unsupported Port and Ruin mass.",
            query_allocation="Prediction uses all saved observations for the round.",
            model_version="v4-regime-calibrated-heuristic",
            outcome=f"Built {len(predictions)} calibrated prediction tensors.",
            do_not_repeat="Do not let Port and Ruin absorb frontier mass without direct trade or collapse evidence.",
            notes=["Prediction summary saved under summaries/prediction-summary.md"],
        ),
    )
    console.print(f"Saved {len(predictions)} prediction tensors for round {round_id}.")


@app.command("submit")
def submit(
    round_id: str = typer.Option(..., "--round-id"),
    all_seeds: bool = typer.Option(False, "--all-seeds", help="Submit all saved seed predictions."),
    seed_index: int | None = typer.Option(None, "--seed-index", help="Submit a single seed."),
) -> None:
    """Submit previously generated prediction tensors."""

    settings = _settings()
    _require_token(settings)
    store = _artifact_store(settings)
    experiment_log = _experiment_log(settings)

    with AstarIslandApiClient(settings) as api_client:
        workflow = SubmissionWorkflow(api_client, store)
        submitted = workflow.submit_predictions(round_id, all_seeds=all_seeds, seed_index=seed_index)

    append_experiment_entry(
        experiment_log,
        ExperimentEntry(
            event_type="submit",
            round_id=round_id,
            hypothesis="Manual submission after validation is safer than unattended writes to the competition API.",
            query_allocation="Submission only; no additional queries consumed.",
            model_version="v4-regime-calibrated-heuristic",
            outcome=f"Submitted seeds {submitted}.",
            do_not_repeat="Do not call submit before confirming the intended seed set.",
            notes=["Submission summary saved under summaries/submission-summary.md"],
        ),
    )
    console.print(f"Submitted seeds {submitted} for round {round_id}.")


@app.command("analyze")
def analyze(round_id: str = typer.Option(..., "--round-id")) -> None:
    """Fetch post-round analysis payloads and learn reusable priors."""

    settings = _settings()
    _require_token(settings)
    store = _artifact_store(settings)
    experiment_log = _experiment_log(settings)
    round_detail = _load_round_detail(settings, round_id)

    with AstarIslandApiClient(settings) as api_client:
        workflow = AnalysisWorkflow(api_client, store)
        summary = workflow.analyze_completed_round(round_detail)

    append_experiment_entry(
        experiment_log,
        ExperimentEntry(
            event_type="analyze",
            round_id=round_id,
            hypothesis="Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.",
            query_allocation="Analysis endpoint fetches only; no simulation budget consumed.",
            model_version="v4-regime-calibrated-heuristic",
            outcome=f"Analyzed {len(summary['seed_scores'])} seeds with average local score {summary['average_score']:.3f}.",
            score=summary["average_score"],
            do_not_repeat="Do not let global dynamic mass drift far above observed plus learned round priors.",
            notes=[
                "Analysis summary saved under summaries/analysis-summary.md",
                "Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json",
                "Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json",
                "Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json",
            ],
        ),
    )
    console.print(f"Analysis complete for round {round_id}. Average local score: {summary['average_score']:.3f}")


def main() -> None:
    app()
