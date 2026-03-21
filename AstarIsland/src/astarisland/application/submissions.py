from __future__ import annotations

from astarisland.infrastructure.api_client import AstarIslandApiClient
from astarisland.infrastructure.artifact_store import ArtifactStore


class SubmissionWorkflow:
    def __init__(self, api_client: AstarIslandApiClient, artifact_store: ArtifactStore) -> None:
        self.api_client = api_client
        self.artifact_store = artifact_store

    def submit_predictions(
        self,
        round_id: str,
        *,
        all_seeds: bool = False,
        seed_index: int | None = None,
    ) -> list[int]:
        if not all_seeds and seed_index is None:
            raise ValueError("manual submission requires --all-seeds or --seed-index")

        seed_indices = [seed_index] if seed_index is not None else []
        if all_seeds:
            tensors = self.artifact_store.load_all_prediction_tensors(round_id)
            seed_indices = [tensor.seed_index for tensor in tensors]
        submitted: list[int] = []
        for current_seed_index in seed_indices:
            tensor = self.artifact_store.load_prediction_tensor(round_id, int(current_seed_index))
            result = self.api_client.submit_prediction(round_id, int(current_seed_index), tensor.tolist())
            self.artifact_store.save_submission_result(round_id, result)
            submitted.append(int(current_seed_index))
        self.artifact_store.save_markdown_summary(
            round_id,
            "submission-summary",
            "\n".join(
                [
                    f"# Submission Summary for Round {round_id}",
                    "",
                    *[f"- Submitted seed {submitted_seed}" for submitted_seed in submitted],
                ]
            ),
        )
        return submitted

