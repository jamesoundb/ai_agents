from core.util import parse_amount


def line_total(qty, price_text):
    return qty * parse_amount(price_text)
