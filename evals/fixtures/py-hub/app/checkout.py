from core.util import format_money, parse_amount


def total(lines):
    cents = sum(parse_amount(x) for x in lines)
    return format_money(cents)
