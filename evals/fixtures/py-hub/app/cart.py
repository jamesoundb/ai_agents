from core.util import parse_amount


def add(cart, price_text):
    cart.append(parse_amount(price_text))
    return cart
