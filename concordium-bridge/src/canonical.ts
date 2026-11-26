import { createHash } from 'node:crypto';

/**
 * Normalizes any JSON-compatible value into a deterministic canonical form
 * matching Python's `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`.
 */
export function canonicalize(value: unknown): unknown {
  if (value === null) {
    return null;
  }

  if (typeof value === 'boolean' || typeof value === 'string') {
    return value;
  }

  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw new Error(`Cannot canonicalize non-finite number: ${String(value)}`);
    }
    // Normalize -0 to 0 per RFC 8785 / canonical JSON
    return Object.is(value, -0) ? 0 : value;
  }

  if (typeof value === 'bigint') {
    return value.toString();
  }

  if (value instanceof Date) {
    return value.toISOString();
  }

  if (value instanceof Uint8Array) {
    return Buffer.from(value).toString('hex');
  }

  if (Array.isArray(value)) {
    return value.map((item) => (item === undefined ? null : canonicalize(item)));
  }

  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const sortedKeys = Object.keys(record).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    const result: Record<string, unknown> = {};

    for (const key of sortedKeys) {
      const prop = record[key];
      if (prop !== undefined && typeof prop !== 'function' && typeof prop !== 'symbol') {
        result[key] = canonicalize(prop);
      }
    }

    return result;
  }

  throw new Error(`Unsupported type for canonical JSON serialization: ${typeof value}`);
}

/**
 * Produces a deterministic, compact UTF-8 JSON string with sorted keys and no whitespace.
 */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

/**
 * Computes raw SHA-256 digest bytes from a UTF-8 string or byte array.
 */
export function sha256Bytes(input: string | Uint8Array): Buffer {
  const hash = createHash('sha256');
  if (typeof input === 'string') {
    hash.update(input, 'utf8');
  } else {
    hash.update(input);
  }
  return hash.digest();
}

/**
 * Computes lowercase 64-character hexadecimal SHA-256 digest.
 */
export function sha256Hex(input: string | Uint8Array): string {
  return sha256Bytes(input).toString('hex');
}

/**
 * Computes the canonical SHA-256 hash of an individual evidence record.
 */
export function calculateRecordHash(record: unknown): string {
  return sha256Hex(canonicalJson(record));
}

/**
 * Hashes two 64-char lowercase hex Merkle nodes (`leftHex + rightHex`) as UTF-8 into their parent digest.
 * Matches `vera_core.attestations.hash_merkle_pair` (`sha256_hex(f"{left_hex}{right_hex}")`).
 */
function hashMerklePair(leftHex: string, rightHex: string): string {
  return sha256Hex(`${leftHex}${rightHex}`);
}

/**
 * Calculates a deterministic SHA-256 binary Merkle root over an ordered list of evidence records.
 *
 * Parity specification with `vera_core/attestations.py`:
 * 1. Each leaf is `sha256(utf8(canonical_json(record)))` as 64-char lowercase hex.
 * 2. Empty `records` (`[]`) returns `sha256("")` (`e3b0c442...`).
 * 3. Single-element `records` returns its leaf hash directly.
 * 4. At each tree level with an odd number of nodes, the last node is duplicated,
 *    and each pair is combined as `sha256(utf8(leftHex + rightHex))`.
 */
export function calculateEvidenceRoot(records: any[]): string {
  if (!Array.isArray(records)) {
    throw new TypeError('records must be an array');
  }

  if (records.length === 0) {
    return sha256Hex('');
  }

  let level: string[] = records.map((record) => calculateRecordHash(record));

  while (level.length > 1) {
    const nextLevel: string[] = [];
    for (let i = 0; i < level.length; i += 2) {
      const left = level[i];
      const right = i + 1 < level.length ? level[i + 1] : left;
      nextLevel.push(hashMerklePair(left, right));
    }
    level = nextLevel;
  }

  return level[0];
}

export interface MerkleProofStep {
  position: 'left' | 'right';
  hash: string;
}

/**
 * Builds a Merkle inclusion proof for the record at `targetIndex`.
 */
export function buildMerkleProof(records: any[], targetIndex: number): MerkleProofStep[] {
  if (!Array.isArray(records) || records.length === 0) {
    throw new Error('Cannot build Merkle proof for empty records');
  }
  if (!Number.isInteger(targetIndex) || targetIndex < 0 || targetIndex >= records.length) {
    throw new RangeError(`targetIndex out of bounds: ${targetIndex}`);
  }

  let level: string[] = records.map((record) => calculateRecordHash(record));
  let index = targetIndex;
  const proof: MerkleProofStep[] = [];

  while (level.length > 1) {
    const nextLevel: string[] = [];
    for (let i = 0; i < level.length; i += 2) {
      const left = level[i];
      const right = i + 1 < level.length ? level[i + 1] : left;

      if (i === index) {
        proof.push({ position: 'right', hash: right });
      } else if (i + 1 === index) {
        proof.push({ position: 'left', hash: left });
      }

      nextLevel.push(hashMerklePair(left, right));
    }
    index = Math.floor(index / 2);
    level = nextLevel;
  }

  return proof;
}

/**
 * Verifies a Merkle inclusion proof against an expected root hash.
 */
export function verifyMerkleProof(
  record: unknown,
  proof: MerkleProofStep[],
  expectedRoot: string
): boolean {
  let current = calculateRecordHash(record);

  for (const step of proof) {
    if (!/^[0-9a-f]{64}$/i.test(step.hash)) {
      return false;
    }
    const sibling = step.hash.toLowerCase();
    current =
      step.position === 'left'
        ? hashMerklePair(sibling, current)
        : hashMerklePair(current, sibling);
  }

  return current === expectedRoot.toLowerCase();
}

/**
 * Calculates a deterministic SHA-256 hash over a canonicalized policy object.
 *
 * Parity specification with Python backend:
 * `hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()`
 */
export function calculatePolicyHash(policy: object): string {
  if (policy === null || typeof policy !== 'object' || Array.isArray(policy)) {
    throw new TypeError('policy must be a non-null plain object');
  }

  return sha256Hex(canonicalJson(policy));
}
