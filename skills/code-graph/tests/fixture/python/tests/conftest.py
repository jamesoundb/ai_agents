import pytest

from app.enums import Palette


@pytest.fixture
def palette() -> Palette:
    return Palette()
