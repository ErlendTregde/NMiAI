from __future__ import annotations

from enum import IntEnum


class TerrainCode(IntEnum):
    EMPTY = 0
    SETTLEMENT = 1
    PORT = 2
    RUIN = 3
    FOREST = 4
    MOUNTAIN = 5
    OCEAN = 10
    PLAINS = 11


class PredictionClass(IntEnum):
    EMPTY = 0
    SETTLEMENT = 1
    PORT = 2
    RUIN = 3
    FOREST = 4
    MOUNTAIN = 5


PREDICTION_CLASSES = tuple(PredictionClass)
LAND_TERRAINS = {
    TerrainCode.EMPTY,
    TerrainCode.PLAINS,
    TerrainCode.SETTLEMENT,
    TerrainCode.PORT,
    TerrainCode.RUIN,
    TerrainCode.FOREST,
}
BUILDABLE_TERRAINS = {
    TerrainCode.EMPTY,
    TerrainCode.PLAINS,
    TerrainCode.RUIN,
    TerrainCode.FOREST,
}
STATIC_TERRAINS = {
    TerrainCode.OCEAN,
    TerrainCode.MOUNTAIN,
}
DYNAMIC_CLASSES = {
    PredictionClass.SETTLEMENT,
    PredictionClass.PORT,
    PredictionClass.RUIN,
}


def terrain_to_prediction_class(terrain: int | TerrainCode) -> PredictionClass:
    terrain_code = TerrainCode(int(terrain))
    if terrain_code in {TerrainCode.EMPTY, TerrainCode.OCEAN, TerrainCode.PLAINS}:
        return PredictionClass.EMPTY
    if terrain_code == TerrainCode.SETTLEMENT:
        return PredictionClass.SETTLEMENT
    if terrain_code == TerrainCode.PORT:
        return PredictionClass.PORT
    if terrain_code == TerrainCode.RUIN:
        return PredictionClass.RUIN
    if terrain_code == TerrainCode.FOREST:
        return PredictionClass.FOREST
    if terrain_code == TerrainCode.MOUNTAIN:
        return PredictionClass.MOUNTAIN
    raise ValueError(f"Unsupported terrain code: {terrain_code}")

