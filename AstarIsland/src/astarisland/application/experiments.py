from __future__ import annotations

from astarisland.domain.models import ExperimentEntry
from astarisland.infrastructure.markdown_log import MarkdownExperimentLog


def append_experiment_entry(experiment_log: MarkdownExperimentLog, entry: ExperimentEntry) -> None:
    experiment_log.append_entry(entry)

