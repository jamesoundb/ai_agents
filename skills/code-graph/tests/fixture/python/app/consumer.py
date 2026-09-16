import io                              # stdlib; must NOT resolve to python/other/io/
import pkg
from pkg import ops, Thing
from collections import abc as collections_abc
from other.np import Sized
from tests_support import base_a


class Frame:
    def merge(self, other):
        from pkg.impl import merge      # local import shadows the method name
        return merge(self, other)       # -> pkg/impl.py::merge, not Frame.merge

    def use(self, x):
        ops.convert(1)                  # namespace bound to pkg/ops.py -> import, not ambiguous with other/np.py
        pkg.Thing().act()               # re-export through pkg/__init__.py -> typed Thing.act
        t = Thing()
        t.act()                         # from-import name re-exported -> typed
        len(x)                          # builtin: no edge to Sized.len
        lonely()                        # never imported: no edge to other/np.py::lonely
        x.render()                      # unknown receiver, 4 candidates: no lead edges
        x.charge()                      # unknown receiver, only candidate is TypeScript: no cross-language lead


class Iterable:
    """Same name as collections.abc.Iterable; must not be picked as a base class."""


class Container(collections_abc.Iterable):
    pass


class MyTest(base_a.TestCase):
    def test_it(self):
        self.helper()                   # typed -> tests_support/base_a.py::TestCase.helper only


class OtherTest(unknown_mod.TestCase):
    def test_it(self):
        self.helper()                   # base unresolved: at most an ambiguous lead, never typed
