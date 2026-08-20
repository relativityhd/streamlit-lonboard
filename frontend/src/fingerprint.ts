/**
 * Fast (GB/s-class), dependency-free content fingerprint for cache-hit checks.
 *
 * Not cryptographic - collisions are astronomically unlikely for our use case
 * (comparing a handful of per-layer byte slices across reruns of the same
 * app) but not adversarially hardened, which is fine since this only decides
 * "reuse this deck.gl layer or rebuild it," never anything security-relevant.
 */

const FNV_OFFSET_BASIS = 0x811c9dc5;
const FNV_PRIME = 0x01000193;

// Second basis/prime pair for the 64-bit-wide variant below. FNV is only defined for
// specific widths, so rather than emulate 64-bit multiplication in JS, the wide hash
// runs two independent 32-bit lanes and concatenates them.
const FNV_OFFSET_BASIS_2 = 0x84222325;
const FNV_PRIME_2 = 0x000001b3;

export function fnv1a(bytes: Uint8Array): string {
  let hash = FNV_OFFSET_BASIS;
  for (let i = 0; i < bytes.length; i++) {
    hash ^= bytes[i];
    hash = Math.imul(hash, FNV_PRIME);
  }
  return (hash >>> 0).toString(16);
}

/**
 * Chainable, 64-bit-wide FNV-1a: folds `bytes` into a running pair of lane states so a
 * fingerprint can span many buffers without concatenating them first.
 *
 * Pass `0, 0` to start (the offset bases are applied then, so a zero-length hash still
 * differs from a hash of a zero byte), and `toHex` to finish. Wider than the 32-bit
 * `fnv1a` used for whole-payload cache checks because these fingerprints decide whether
 * to *reuse geometry*: a 32-bit collision would silently leave stale shapes on screen,
 * whereas a collision in the payload-level check only costs a redundant rebuild.
 */
export function fnv1aInto(hash1: number, hash2: number, bytes: Uint8Array): [number, number] {
  let h1 = hash1 === 0 && hash2 === 0 ? FNV_OFFSET_BASIS : hash1;
  let h2 = hash1 === 0 && hash2 === 0 ? FNV_OFFSET_BASIS_2 : hash2;
  for (let i = 0; i < bytes.length; i++) {
    const byte = bytes[i];
    h1 ^= byte;
    h1 = Math.imul(h1, FNV_PRIME);
    h2 ^= byte;
    h2 = Math.imul(h2, FNV_PRIME_2);
  }
  return [h1, h2];
}

/** Render a `fnv1aInto` lane pair as a stable hex string. */
export function toHex(hash1: number, hash2: number): string {
  return `${(hash1 >>> 0).toString(16)}${(hash2 >>> 0).toString(16)}`;
}

/**
 * Same algorithm as `fnv1a`, but over a JS string directly (hashing each
 * UTF-16 code unit as two bytes) instead of a `Uint8Array` - avoids a
 * `TextEncoder` allocation on every rerun for callers hashing JSON-stringified
 * props rather than binary payloads.
 */
export function fnv1aString(str: string): string {
  let hash = FNV_OFFSET_BASIS;
  for (let i = 0; i < str.length; i++) {
    const code = str.charCodeAt(i);
    hash ^= code & 0xff;
    hash = Math.imul(hash, FNV_PRIME);
    hash ^= code >> 8;
    hash = Math.imul(hash, FNV_PRIME);
  }
  return (hash >>> 0).toString(16);
}
