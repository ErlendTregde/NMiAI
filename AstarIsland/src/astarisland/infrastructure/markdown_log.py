from __future__ import annotations

from pathlib import Path

from astarisland.domain.models import ExperimentEntry


DOCUMENTATION_TEMPLATE = """# Astar Island Experiment Ledger

## Stable Assumptions

- API docs are treated as the source of truth for tensor ordering and class mapping.
- Manual submission is the default safety gate.

## Attempt Log

<!-- append experiment entries below -->

## Mistakes To Avoid Repeating

- Do not submit tensors with zeros or invalid row sums.

## Validated Heuristics

- Reserve query budget for repeated windows before spending all 50 on coverage.
"""


class MarkdownExperimentLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def ensure_exists(self) -> None:
        if not self.path.exists() or not self.path.read_text(encoding="utf-8").strip():
            self.path.write_text(DOCUMENTATION_TEMPLATE, encoding="utf-8")

    def append_entry(self, entry: ExperimentEntry) -> None:
        self.ensure_exists()
        content = self.path.read_text(encoding="utf-8")
        marker = "## Mistakes To Avoid Repeating"
        addition = f"{entry.to_markdown()}\n\n"
        if marker in content:
            updated = content.replace(marker, addition + marker, 1)
        else:
            updated = content.rstrip() + "\n\n" + addition
        self.path.write_text(updated, encoding="utf-8")

