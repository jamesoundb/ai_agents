from core.util import format_money, parse_amount


def refund(text):
    return format_money(-parse_amount(text))
