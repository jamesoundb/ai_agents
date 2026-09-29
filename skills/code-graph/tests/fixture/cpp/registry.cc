#include "framework/op.h"
#include "kernel.h"

REGISTER_OP("DemoMatMul").Input("a: T").Output("p: T");

namespace demo {
namespace ops {
class DemoMatMulOp : public OpKernel {
 public:
  void Compute(OpKernelContext* ctx) override {}
};
}  // namespace ops
}  // namespace demo

REGISTER_KERNEL_BUILDER(Name("DemoMatMul").Device(DEVICE_CPU), demo::ops::DemoMatMulOp);
