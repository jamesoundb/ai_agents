from core.util import Ledger, format_money


def statement(rows):
    ledger = Ledger()
    for cents, memo in rows:
        ledger.post(cents, memo)
    return format_money(ledger.balance())
