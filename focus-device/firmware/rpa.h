// rpa.h: recognise a phone's rotating Bluetooth address using its identity key (IRK).
//
// Phones change their Bluetooth address about every 15 minutes ("resolvable private address",
// RPA). The phone hands its IRK to the device when it pairs. An address belongs to the phone when
//   AES-128(IRK, 13 zero bytes || prand) ends in the address's 3-byte hash        (Core Spec ah()).
//
// Byte order (checked against the Bluetooth Core Spec sample data in tests/test_rpa.cpp):
//   - NimBLE stores the IRK little-endian (ble_store_value_sec.irk[0] = least significant byte).
//   - NimBLEAddress::getVal() is little-endian: val[0..2] = hash, val[3..5] = prand, val[5] = MSB.
//   - AES (mbedtls) works on big-endian 16-byte blocks.
// The byte shuffling lives in the functions below with no crypto, so a PC test can check it.
#pragma once
#include <stdint.h>
#include <string.h>

namespace rpa {

// Top two bits 01 = resolvable private address.
inline bool isResolvable(const uint8_t addrLe[6]) { return (addrLe[5] & 0xC0) == 0x40; }

// NimBLE's little-endian IRK -> the big-endian AES key.
inline void keyFromIrk(const uint8_t irkLe[16], uint8_t keyBe[16]) {
  for (int i = 0; i < 16; i++) keyBe[i] = irkLe[15 - i];
}

// The AES input block: 13 zero bytes, then prand most-significant byte first.
inline void plaintextFor(const uint8_t addrLe[6], uint8_t blockBe[16]) {
  memset(blockBe, 0, 16);
  blockBe[13] = addrLe[5];
  blockBe[14] = addrLe[4];
  blockBe[15] = addrLe[3];
}

// Does the AES output match the hash in the address?
inline bool hashMatches(const uint8_t cipherBe[16], const uint8_t addrLe[6]) {
  return cipherBe[13] == addrLe[2] && cipherBe[14] == addrLe[1] && cipherBe[15] == addrLe[0];
}

// Spec sample data (Core Spec Vol 3 Part H, Appendix D): IRK ec0234a357c8ad05341010a60a397d9b,
// prand 708194, AES output 159d5fb72ebe2311a48c1bdcc40dfbaa, hash 0dfbaa. In NimBLE byte order:
static const uint8_t kSpecIrkLe[16] = {0x9b, 0x7d, 0x39, 0x0a, 0xa6, 0x10, 0x10, 0x34,
                                       0x05, 0xad, 0xc8, 0x57, 0xa3, 0x34, 0x02, 0xec};
static const uint8_t kSpecAddrLe[6] = {0xaa, 0xfb, 0x0d, 0x94, 0x81, 0x70};

}  // namespace rpa

#ifdef ARDUINO
#include "mbedtls/aes.h"

namespace rpa {

// One per phone. setIrk() once, then matches() for every advertisement.
class Resolver {
 public:
  Resolver() { mbedtls_aes_init(&ctx_); }
  ~Resolver() { mbedtls_aes_free(&ctx_); }
  Resolver(const Resolver &) = delete;
  Resolver &operator=(const Resolver &) = delete;

  void setIrk(const uint8_t irkLe[16]) {
    uint8_t key[16];
    keyFromIrk(irkLe, key);
    ready_ = mbedtls_aes_setkey_enc(&ctx_, key, 128) == 0;
  }
  void clear() { ready_ = false; }
  bool ready() const { return ready_; }

  bool matches(const uint8_t addrLe[6]) {
    if (!ready_ || !isResolvable(addrLe)) return false;
    uint8_t in[16], out[16];
    plaintextFor(addrLe, in);
    if (mbedtls_aes_crypt_ecb(&ctx_, MBEDTLS_AES_ENCRYPT, in, out) != 0) return false;
    return hashMatches(out, addrLe);
  }

 private:
  mbedtls_aes_context ctx_;
  bool ready_ = false;
};

// Run once at boot: proves the AES path and byte order on the real chip.
inline bool selfTest() {
  Resolver r;
  r.setIrk(kSpecIrkLe);
  uint8_t wrong[6];
  memcpy(wrong, kSpecAddrLe, 6);
  wrong[0] ^= 1;
  return r.matches(kSpecAddrLe) && !r.matches(wrong);
}

}  // namespace rpa
#endif
