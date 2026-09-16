def convert(x):
    return -x


def lonely():
    """Defined once, never imported by consumer.py: a bare `lonely()` there must NOT link here."""
    return 0


class Sized:
    def len(self):
        """Same name as the builtin; `len(x)` must never link here."""
        return 0


class Render1:
    def render(self): pass


class Render2:
    def render(self): pass


class Render3:
    def render(self): pass


class Render4:
    def render(self): pass
