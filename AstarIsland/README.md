# Astar Island

CLI-first baseline client for the Astar Island simulator challenge.

## What This Project Does

- Fetches round metadata and initial states
- Plans and runs a 50-query observation campaign
- Builds calibrated `H x W x 6` prediction tensors for every seed
- Saves raw and derived artifacts under `data/rounds/<round_id>/`
- Supports manual submission and post-round retrospective analysis

## Setup

1. Install dependencies with `uv sync --extra dev`
2. Export `ASTAR_ACCESS_TOKEN=<your jwt>`
3. Run commands with `uv run astar-island ...`

## Main Commands

- `uv run astar-island round active`
- `uv run astar-island observe plan --round-id <id>`
- `uv run astar-island observe run --round-id <id>`
- `uv run astar-island predict --round-id <id>`
- `uv run astar-island submit --round-id <id> --all-seeds`
- `uv run astar-island analyze --round-id <id>`

## Architecture

- `astarisland.domain`: pure rules, scoring, tensors, and feature extraction
- `astarisland.application`: planning, prediction, submission, and analysis workflows
- `astarisland.infrastructure`: API client, settings, storage, and markdown logging
- `astarisland.cli`: command-line interface

