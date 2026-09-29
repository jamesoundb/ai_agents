#ifndef FIXTURE_KERNEL_H_
#define FIXTURE_KERNEL_H_
#include <vector>
#include "fixture/base.h"

namespace demo {
namespace ops {

class MatMulOp : public OpKernel {
 public:
  explicit MatMulOp(OpKernelConstruction* ctx);
  ~MatMulOp();
  void Compute(OpKernelContext* ctx) override;
  int rank() const { return rank_; }

 private:
  int rank_ = 0;
  OpKernelContext* ctx_;
};

struct Point {
  float x;
  float y;
};

enum class Mode { FAST, SLOW };

}  // namespace ops
}  // namespace demo
#endif
