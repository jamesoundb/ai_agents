def test_pick(palette):
    assert palette.pick() == "RED"        # untyped fixture parameter -> Palette.pick typed via the fixture's return type
