import os
import json as js

from .models import Repo, User


class Service:
    def __init__(self):
        self.repo = Repo()

    def run(self, u: User):
        self.repo.save(u)          # typed: inferred field type Repo
        os.path.join("a", "b")     # external namespace
        js.dumps(u)                # external alias -> must not hit models.dumps


def helper(x, u):
    x.save(u)                      # unknown receiver -> ambiguous lead at most


def make_repo() -> Repo:
    return Repo()


class Factory:
    def __init__(self):
        self.repo2 = make_repo()   # field typed from make_repo's return annotation

    def repo(self) -> Repo:
        return Repo()

    def run(self, u):
        r = make_repo()
        r.save(u)                  # typed via make_repo's return annotation
        self.repo().save(u)        # typed via the method's return annotation (call segment in the chain)
        self.repo2.save(u)         # typed via the field's call result
