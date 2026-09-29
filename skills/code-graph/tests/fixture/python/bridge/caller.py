"""Python that calls straight into a C++ extension module."""
from . import _pywrap_demo


def run_it(x):
    return _pywrap_demo.DemoExecute(x)


def not_a_binding(x):
    """`DemoOther` is exported too, but this name also exists in Python, so the bridge must
    not claim it -- see the shadowing test."""
    return locally_defined(x)


def locally_defined(x):
    return x
