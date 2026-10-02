#include "cstore.h"

/* calls a function defined in another .c file, visible only through the header */
int cstore_use(void) { return cstore_put(1); }
