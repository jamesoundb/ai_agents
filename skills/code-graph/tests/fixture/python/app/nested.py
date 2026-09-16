from .models import Repo, User


class Holder:
    """Two unrelated classes named Inner in one file: the linker must not merge their fields."""

    def make_a(self):
        class Inner:
            def __init__(self):
                self.x = Repo()

            def go(self, u):
                return self.x.save(u)      # typed -> Repo.save, whatever hash seed the process has
        return Inner()

    def make_b(self):
        class Inner:
            def __init__(self):
                self.x = User()

            def go(self, u):
                return self.x.save(u)      # User has no save: must not become typed via the other Inner
        return Inner()
