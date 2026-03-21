from __future__ import annotations

from astarisland.domain.models import RoundDetail, RoundSummary
from astarisland.infrastructure.api_client import AstarIslandApiClient
from astarisland.infrastructure.artifact_store import ArtifactStore


class RoundWorkflow:
    def __init__(self, api_client: AstarIslandApiClient, artifact_store: ArtifactStore) -> None:
        self.api_client = api_client
        self.artifact_store = artifact_store

    def get_active_round(self) -> tuple[RoundSummary, RoundDetail]:
        rounds = self.api_client.list_rounds()
        active_round = next((round_summary for round_summary in rounds if round_summary.status == "active"), None)
        if active_round is None:
            raise RuntimeError("No active round found")
        detail = self.api_client.get_round_detail(active_round.id)
        self.artifact_store.save_round_detail(detail)
        return active_round, detail

    def get_round_detail(self, round_id: str) -> RoundDetail:
        detail = self.api_client.get_round_detail(round_id)
        self.artifact_store.save_round_detail(detail)
        return detail

