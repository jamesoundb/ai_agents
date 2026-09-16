class User:
    """A user record."""


class Repo:
    def save(self, u: User) -> None:
        pass


def dumps(x):
    """Same name as json.dumps; must not be linked from `js.dumps()` in service.py."""
    return str(x)
