from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

from astarisland.domain.enums import BUILDABLE_TERRAINS, LAND_TERRAINS, TerrainCode, terrain_to_prediction_class
from astarisland.domain.models import InitialState


def bucket_distance(distance: int | None) -> str:
    if distance is None:
        return "none"
    if distance == 0:
        return "0"
    if distance <= 2:
        return "1-2"
    if distance <= 5:
        return "3-5"
    if distance <= 9:
        return "6-9"
    return "10+"


def bucket_landmass_size(size: int | None) -> str:
    if size is None or size <= 0:
        return "water"
    if size <= 16:
        return "tiny"
    if size <= 48:
        return "small"
    if size <= 120:
        return "medium"
    return "large"


@dataclass(frozen=True, slots=True)
class CellFeatures:
    x: int
    y: int
    initial_terrain: int
    mapped_class: int
    coastal: bool
    initial_port: bool
    settlement_distance_bucket: str
    forest_distance_bucket: str
    ruin_distance_bucket: str
    coast_distance_bucket: str
    landmass_membership: str
    landmass_size_bucket: str
    buildable: bool

    def bucket_key(self) -> str:
        return "|".join(
            [
                f"terrain:{self.initial_terrain}",
                f"class:{self.mapped_class}",
                f"coastal:{int(self.coastal)}",
                f"port:{int(self.initial_port)}",
                f"settle_dist:{self.settlement_distance_bucket}",
                f"forest_dist:{self.forest_distance_bucket}",
                f"ruin_dist:{self.ruin_distance_bucket}",
                f"coast_dist:{self.coast_distance_bucket}",
                f"landmass_size:{self.landmass_size_bucket}",
                f"buildable:{int(self.buildable)}",
            ]
        )


@dataclass(frozen=True, slots=True)
class FeatureCatalog:
    per_cell: dict[tuple[int, int], CellFeatures]
    landmass_sizes: dict[str, int]

    def feature_for(self, x: int, y: int) -> CellFeatures:
        return self.per_cell[(x, y)]


def neighbors(x: int, y: int, width: int, height: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for delta_x, delta_y in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        next_x = x + delta_x
        next_y = y + delta_y
        if 0 <= next_x < width and 0 <= next_y < height:
            out.append((next_x, next_y))
    return out


def compute_distance_map(
    sources: list[tuple[int, int]],
    width: int,
    height: int,
) -> dict[tuple[int, int], int | None]:
    distance_map = {(x, y): None for y in range(height) for x in range(width)}
    if not sources:
        return distance_map

    queue: deque[tuple[int, int]] = deque()
    for source in sources:
        distance_map[source] = 0
        queue.append(source)

    while queue:
        current_x, current_y = queue.popleft()
        current_distance = distance_map[(current_x, current_y)]
        assert current_distance is not None
        for next_x, next_y in neighbors(current_x, current_y, width, height):
            if distance_map[(next_x, next_y)] is None:
                distance_map[(next_x, next_y)] = current_distance + 1
                queue.append((next_x, next_y))

    return distance_map


def compute_landmass_membership(grid: list[list[int]]) -> tuple[dict[tuple[int, int], str], dict[str, int]]:
    height = len(grid)
    width = len(grid[0])
    membership: dict[tuple[int, int], str] = {}
    sizes: Counter[str] = Counter()
    component_index = 0

    for y in range(height):
        for x in range(width):
            terrain = TerrainCode(int(grid[y][x]))
            if terrain not in LAND_TERRAINS or (x, y) in membership:
                continue
            component_index += 1
            label = f"lm{component_index}"
            queue = deque([(x, y)])
            membership[(x, y)] = label
            while queue:
                current_x, current_y = queue.popleft()
                sizes[label] += 1
                for next_x, next_y in neighbors(current_x, current_y, width, height):
                    next_terrain = TerrainCode(int(grid[next_y][next_x]))
                    if next_terrain in LAND_TERRAINS and (next_x, next_y) not in membership:
                        membership[(next_x, next_y)] = label
                        queue.append((next_x, next_y))

    for y in range(height):
        for x in range(width):
            membership.setdefault((x, y), "water")

    return membership, dict(sizes)


def build_feature_catalog(initial_state: InitialState) -> FeatureCatalog:
    grid = initial_state.overlay_grid()
    height = len(grid)
    width = len(grid[0])
    settlement_sources = [(settlement.x, settlement.y) for settlement in initial_state.settlements]
    ruin_sources = [
        (x, y)
        for y, row in enumerate(grid)
        for x, value in enumerate(row)
        if int(value) == TerrainCode.RUIN
    ]
    forest_sources = [
        (x, y)
        for y, row in enumerate(grid)
        for x, value in enumerate(row)
        if int(value) == TerrainCode.FOREST
    ]

    settlement_distances = compute_distance_map(settlement_sources, width, height)
    forest_distances = compute_distance_map(forest_sources, width, height)
    membership, landmass_sizes = compute_landmass_membership(grid)
    ruin_distances = compute_distance_map(ruin_sources, width, height)

    per_cell: dict[tuple[int, int], CellFeatures] = {}
    initial_port_cells = {(settlement.x, settlement.y) for settlement in initial_state.settlements if settlement.has_port}
    coastal_land_cells = []
    for y, row in enumerate(grid):
        for x, _value in enumerate(row):
            terrain = TerrainCode(int(grid[y][x]))
            if terrain in LAND_TERRAINS and any(
                grid[next_y][next_x] == TerrainCode.OCEAN for next_x, next_y in neighbors(x, y, width, height)
            ):
                coastal_land_cells.append((x, y))
    coast_distances = compute_distance_map(coastal_land_cells, width, height)

    for y, row in enumerate(grid):
        for x, value in enumerate(row):
            coastal = any(grid[next_y][next_x] == TerrainCode.OCEAN for next_x, next_y in neighbors(x, y, width, height))
            landmass_label = membership[(x, y)]
            features = CellFeatures(
                x=x,
                y=y,
                initial_terrain=int(value),
                mapped_class=int(terrain_to_prediction_class(value)),
                coastal=coastal,
                initial_port=(x, y) in initial_port_cells,
                settlement_distance_bucket=bucket_distance(settlement_distances[(x, y)]),
                forest_distance_bucket=bucket_distance(forest_distances[(x, y)]),
                ruin_distance_bucket=bucket_distance(ruin_distances[(x, y)]),
                coast_distance_bucket=bucket_distance(coast_distances[(x, y)]),
                landmass_membership=landmass_label,
                landmass_size_bucket=bucket_landmass_size(landmass_sizes.get(landmass_label)),
                buildable=TerrainCode(int(value)) in BUILDABLE_TERRAINS,
            )
            per_cell[(x, y)] = features

    return FeatureCatalog(per_cell=per_cell, landmass_sizes=landmass_sizes)
