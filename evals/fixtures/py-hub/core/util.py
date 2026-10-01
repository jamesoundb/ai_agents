"""Money helpers shared by the app and the batch scripts."""


def _cents(value):
    return int(round(value * 100))


def parse_amount(text):
    """'12.50' -> 1250 cents."""
    return _cents(float(text.strip().lstrip("$")))


def format_money(cents):
    return f"${cents / 100:.2f}"


def legacy_round(value):
    """Kept for the old test suite only."""
    return round(value, 2)


class Ledger:
    def __init__(self):
        self.entries = []

    def post(self, cents, memo=""):
        self.entries.append((cents, memo))

    def balance(self):
        return sum(c for c, _ in self.entries)
