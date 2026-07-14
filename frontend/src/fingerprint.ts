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

export function fnv1a(bytes: Uint8Array): string {
  let hash = FNV_OFFSET_BASIS;
  for (let i = 0; i < bytes.length; i++) {
    hash ^= bytes[i];
    hash = Math.imul(hash, FNV_PRIME);
  }
  return (hash >>> 0).toString(16);
}
