from __future__ import annotations

from ..schemas import ResolvedBlueprint


def build_instruction(blueprint: ResolvedBlueprint) -> str:
    return blueprint.detail.instruction.strip()