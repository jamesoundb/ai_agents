from __future__ import annotations

from functools import cached_property

from .enums import Color, Palette


class Swatch:
    def __init__(self, color: Color) -> None:
        self.color = color

    @property
    def tone(self) -> Color:
        return self.color


class Board:
    def __init__(self, ok: bool) -> None:
        self.swatches: list[Swatch] = []
        self.by_name: dict[str, Swatch] = {}
        if ok:
            self.palette = Palette()            # assigned inside a branch of __init__: still a typed field
        else:
            self.palette = Palette()

    def labels(self) -> list[str]:
        out = []
        for s in self.swatches:                 # for-loop variable typed from list[Swatch]
            out.append(s.color.label())         # -> Color.label typed
        return out

    def comp(self) -> list[str]:
        return [s.tone.label() for s in self.swatches]      # comprehension variable + @property segment -> Color.label

    def named(self) -> str:
        for name, sw in self.by_name.items():   # dict[str, Swatch].items() -> Swatch
            return sw.color.label()
        if (found := self.by_name.get("x")) is not None:    # walrus + dict.get -> Swatch
            return found.color.label()
        return "".join(["a"])                   # literal receiver: no lead

    def first(self) -> str:
        return self.palette.pick()              # -> Palette.pick typed

    def first_label(self) -> str:
        s = self.swatches[0]                    # indexing a list[Swatch] field -> Swatch
        return s.color.label()

    def named_swatch(self) -> str:
        sw = self.by_name["x"]                  # indexing a dict[str, Swatch] field -> Swatch
        return sw.color.label()


def use_with() -> str:
    with Board(True) as b:                      # `with X() as b` types b
        return b.first()
