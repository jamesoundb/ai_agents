#include "pybind11/pybind11.h"
#include "kernel.h"
namespace py = pybind11;

int RealCompute(int a, int b) { return a + b; }

// A typed local, and a call through it. Exercises two things the fixture missed until a
// fresh-install test caught them: C++ local-variable typing, and call edges landing on the
// .cc definition rather than the .h prototype.
int UsesPoint(int a) {
  demo::ops::Point p;
  MatMulOp op(nullptr);
  op.Compute(nullptr);
  return a + static_cast<int>(p.x);
}

PYBIND11_MODULE(_pywrap_demo, m) {
  m.def("DemoExecute", [](int a) { return RealCompute(a, 1); });
  m.def("DemoOther", &RealCompute);
}
