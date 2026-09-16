from . import enums as en
from .enums import Palette as BasePalette                      # aliased from-import


class ThemedPalette(BasePalette):                              # extends through the alias
    pass


def use() -> str:
    p = en.Palette()                                           # module-qualified constructor keeps its qualifier
    return p.name()                                            # -> Palette.name typed
