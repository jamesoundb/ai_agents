#include <stdio.h>

struct Buffer {
  int size;
  char* data;
};

static int buffer_len(struct Buffer* b) { return b->size; }

int main(void) {
  struct Buffer b;
  buffer_len(&b);
  printf("%d\n", b.size);
  return 0;
}
