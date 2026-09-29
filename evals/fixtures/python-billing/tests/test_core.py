from decimal import Decimal

from billing.core import Ledger


def test_post_records_an_entry():
    ledger = Ledger("b1")
    assert ledger.post(Decimal("1.00"), "t")["book"] == "b1"
