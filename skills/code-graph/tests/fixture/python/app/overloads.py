from typing import overload


@overload
def read(x: int) -> int: ...
@overload
def read(x: str) -> str: ...
def read(x):
    """The implementation; callers must attach here, not to the @overload stubs."""
    return x


def caller():
    return read(1)
