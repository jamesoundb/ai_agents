from __future__ import annotations

from enum import Enum
from typing import Optional


class Color(Enum):
    RED = 1
    BLUE = 2

    def label(self) -> str:
        return self.name


class Palette:
    primary: Optional["Color"] = None

    def __init__(self, **options: object) -> None:
        self.options = options

    def name(self) -> str:
        return self.primary.label() if self.primary else ""     # Optional["Color"] field -> Color.label typed

    def pick(self) -> str:
        return Color.RED.label()                               # enum member -> Color.label typed

    def opts(self) -> int:
        self.options.pop("x", None)                            # **kwargs is a dict: no lead into repo `pop` methods
        return 0
