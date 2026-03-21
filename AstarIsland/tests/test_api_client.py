from __future__ import annotations

import httpx
import pytest
import respx

from astarisland.application.rounds import RoundWorkflow
from astarisland.application.submissions import SubmissionWorkflow
from astarisland.domain.tensors import PredictionTensor
from astarisland.infrastructure.api_client import ApiError, AstarIslandApiClient
from astarisland.infrastructure.artifact_store import ArtifactStore
from astarisland.infrastructure.settings import Settings


def _settings(tmp_path):
    return Settings(
        base_url="https://api.ainm.no",
        access_token="test-token",
        data_dir=tmp_path / "data",
    )


@respx.mock
def test_api_client_sets_auth_header_and_parses_rounds(tmp_path) -> None:
    route = respx.get("https://api.ainm.no/astar-island/rounds").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "round-1",
                    "round_number": 1,
                    "status": "active",
                    "map_width": 15,
                    "map_height": 15,
                }
            ],
        )
    )

    with AstarIslandApiClient(_settings(tmp_path)) as client:
        rounds = client.list_rounds()

    assert len(rounds) == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"


@respx.mock
def test_round_workflow_saves_round_detail(tmp_path, fixture_round_detail) -> None:
    respx.get("https://api.ainm.no/astar-island/rounds").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": fixture_round_detail.id,
                    "round_number": fixture_round_detail.round_number,
                    "status": "active",
                    "map_width": fixture_round_detail.map_width,
                    "map_height": fixture_round_detail.map_height,
                }
            ],
        )
    )
    respx.get(f"https://api.ainm.no/astar-island/rounds/{fixture_round_detail.id}").mock(
        return_value=httpx.Response(200, json=fixture_round_detail.model_dump(mode="json"))
    )

    store = ArtifactStore(tmp_path / "data")
    with AstarIslandApiClient(_settings(tmp_path)) as client:
        workflow = RoundWorkflow(client, store)
        _, detail = workflow.get_active_round()

    saved = store.load_round_detail(detail.id)
    assert saved.id == fixture_round_detail.id


@respx.mock
def test_submit_prediction_retries_after_429(tmp_path) -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, json={"detail": "slow down"})
        return httpx.Response(200, json={"status": "accepted", "round_id": "round-1", "seed_index": 0})

    respx.post("https://api.ainm.no/astar-island/submit").mock(side_effect=handler)

    with AstarIslandApiClient(_settings(tmp_path)) as client:
        result = client.submit_prediction("round-1", 0, [[[1 / 6.0] * 6]])

    assert result.status == "accepted"
    assert attempts["count"] == 2


@respx.mock
def test_submit_prediction_raises_on_validation_error(tmp_path) -> None:
    respx.post("https://api.ainm.no/astar-island/submit").mock(
        return_value=httpx.Response(400, text="Cell (0,0): probs sum to 0.5, expected 1.0")
    )

    with AstarIslandApiClient(_settings(tmp_path)) as client:
        with pytest.raises(ApiError):
            client.submit_prediction("round-1", 0, [[[1.0, 0.0, 0.0, 0.0, 0.0, 0.0]]])


@respx.mock
def test_submission_workflow_allows_overwrite_on_resubmit(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "data")
    tensor = PredictionTensor.uniform(seed_index=0, height=2, width=2)
    store.save_prediction_tensor("round-1", tensor)
    respx.post("https://api.ainm.no/astar-island/submit").mock(
        return_value=httpx.Response(200, json={"status": "accepted", "round_id": "round-1", "seed_index": 0})
    )

    with AstarIslandApiClient(_settings(tmp_path)) as client:
        workflow = SubmissionWorkflow(client, store)
        workflow.submit_predictions("round-1", seed_index=0)
        workflow.submit_predictions("round-1", seed_index=0)

    assert (tmp_path / "data" / "rounds" / "round-1" / "submissions" / "seed-0.json").exists()

