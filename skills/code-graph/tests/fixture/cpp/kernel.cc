#include "kernel.h"

namespace demo {
namespace ops {

MatMulOp::MatMulOp(OpKernelConstruction* ctx) : OpKernel(ctx) { rank_ = 2; }

MatMulOp::~MatMulOp() {}

void MatMulOp::Compute(OpKernelContext* ctx) {
  const Tensor& a = ctx->input(0);
  Validate(a);
  errors::InvalidArgument("bad shape");
  Point* w = new Point();
  helper_.Run(a, w);
}

static int Validate(const Tensor& t) { return t.dims(); }

}  // namespace ops
}  // namespace demo
