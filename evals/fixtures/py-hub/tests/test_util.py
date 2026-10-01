from core.util import legacy_round, parse_amount


def test_legacy_round():
    assert legacy_round(1.234) == 1.23


def test_parse():
    assert parse_amount("$1.50") == 150
