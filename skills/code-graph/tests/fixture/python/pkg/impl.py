class Thing:
    def act(self):
        return 1


def merge(a, b):
    """Called from inside a method that also happens to be named merge (shadowing test)."""
    return a
