// Host test for the byte order in firmware/rpa.h, using the Bluetooth Core Spec ah() sample data.
// The AES step itself is checked on the chip by rpa::selfTest() at boot (reported by `info`).
#include <stdio.h>
#include "../firmware/rpa.h"

static int failures = 0;
#define CHECK(cond)                                                \
  do {                                                             \
    if (!(cond)) {                                                 \
      printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);       \
      failures++;                                                  \
    }                                                              \
  } while (0)

int main() {
  // Spec values, most significant byte first.
  const uint8_t specKey[16] = {0xec, 0x02, 0x34, 0xa3, 0x57, 0xc8, 0xad, 0x05,
                               0x34, 0x10, 0x10, 0xa6, 0x0a, 0x39, 0x7d, 0x9b};
  const uint8_t specPlain[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x70, 0x81, 0x94};
  const uint8_t specCipher[16] = {0x15, 0x9d, 0x5f, 0xb7, 0x2e, 0xbe, 0x23, 0x11,
                                  0xa4, 0x8c, 0x1b, 0xdc, 0xc4, 0x0d, 0xfb, 0xaa};

  uint8_t key[16], block[16];
  rpa::keyFromIrk(rpa::kSpecIrkLe, key);
  CHECK(memcmp(key, specKey, 16) == 0);

  rpa::plaintextFor(rpa::kSpecAddrLe, block);
  CHECK(memcmp(block, specPlain, 16) == 0);

  CHECK(rpa::isResolvable(rpa::kSpecAddrLe));
  CHECK(rpa::hashMatches(specCipher, rpa::kSpecAddrLe));

  uint8_t wrong[6];
  memcpy(wrong, rpa::kSpecAddrLe, 6);
  wrong[1] ^= 0x10;  // corrupt the hash
  CHECK(!rpa::hashMatches(specCipher, wrong));

  uint8_t notRpa[6];
  memcpy(notRpa, rpa::kSpecAddrLe, 6);
  notRpa[5] = 0xC0 | (notRpa[5] & 0x3F);  // static random address
  CHECK(!rpa::isResolvable(notRpa));
  notRpa[5] = 0x00 | (notRpa[5] & 0x3F);  // non-resolvable private address
  CHECK(!rpa::isResolvable(notRpa));

  if (failures) {
    printf("%d check(s) FAILED\n", failures);
    return 1;
  }
  printf("all rpa tests passed\n");
  return 0;
}
