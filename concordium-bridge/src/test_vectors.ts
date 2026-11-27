import { resolve } from 'node:path';
import dotenv from 'dotenv';
import {
  buildMerkleProof,
  calculateEvidenceRoot,
  calculatePolicyHash,
  calculateRecordHash,
  canonicalJson,
  verifyMerkleProof,
} from './canonical.js';
import {
  calculateAgentCardHash,
  generateAgentCard,
  linkExternalKeyCIS8,
  registerAgentInCIS8004,
  validateAgentCard,
} from './agentIdentity.js';

// Load environment variables from root .env if present (never overrides existing env vars)
dotenv.config({ path: resolve(process.cwd(), '.env') });
dotenv.config({ path: resolve(process.cwd(), '..', '.env') });

const homeAirRecords = [
  {
    batch_id: 'home-air-batch-001',
    seq: 1,
    timestamp: '2026-11-25T18:00:00Z',
    source_type: 'physical_sensor',
    device_id: 'mq135-node-01',
    raw_adc: 312,
    baseline_adc: 300,
    anomaly_detected: false,
    calibration_valid: true,
  },
  {
    batch_id: 'home-air-batch-001',
    seq: 2,
    timestamp: '2026-11-25T18:05:00Z',
    source_type: 'physical_sensor',
    device_id: 'mq135-node-01',
    raw_adc: 645,
    baseline_adc: 300,
    anomaly_detected: true,
    calibration_valid: true,
  },
  {
    batch_id: 'home-air-batch-001',
    seq: 3,
    timestamp: '2026-11-25T18:10:00Z',
    source_type: 'physical_sensor',
    device_id: 'mq135-node-01',
    raw_adc: 340,
    baseline_adc: 300,
    anomaly_detected: false,
    calibration_valid: true,
  },
];

const businessPolicy = {
  policy_id: 'vera-business-scope2-v1',
  version: '1.0.0',
  rules: {
    require_valid_calibration: true,
    min_coverage_ratio: 0.95,
    max_intensity_kg_co2e_per_unit: 1.25,
    reject_replay_batch: true,
  },
  allowed_sources: ['physical_sensor', 'synthetic_declared', 'imported'],
  settlement: {
    idempotent: true,
    currency: 'CCD',
  },
};

async function main() {
  console.log('=== VERA Concordium Canonical Test Vectors ===\n');

  // 1. Canonical JSON & Record Hashes
  console.log('1) Canonical Record Hashes:');
  homeAirRecords.forEach((record, index) => {
    console.log(`   - Record[${index}] canonical: ${canonicalJson(record)}`);
    console.log(`     SHA-256: ${calculateRecordHash(record)}`);
  });

  // 2. Evidence Root & Merkle Proof
  const emptyRoot = calculateEvidenceRoot([]);
  const singleRoot = calculateEvidenceRoot([homeAirRecords[0]]);
  const evidenceRoot = calculateEvidenceRoot(homeAirRecords);
  const proofIdx1 = buildMerkleProof(homeAirRecords, 1);
  const proofValid = verifyMerkleProof(homeAirRecords[1], proofIdx1, evidenceRoot);

  console.log('\n2) Evidence Roots (Merkle SHA-256):');
  console.log(`   - Empty root ([]):          ${emptyRoot}`);
  console.log(`   - Single record root ([0]): ${singleRoot}`);
  console.log(`   - Batch root ([0..2]):      ${evidenceRoot}`);
  console.log(`   - Merkle proof [1] valid:   ${proofValid}`);

  // 3. Policy Hash
  const policyCanonical = canonicalJson(businessPolicy);
  const policyHash = calculatePolicyHash(businessPolicy);

  console.log('\n3) Policy Hash:');
  console.log(`   - Canonical policy: ${policyCanonical}`);
  console.log(`   - Policy SHA-256:   ${policyHash}`);

  // 4. Optional CIS-8004 & CIS-8 simulation when environment variables are configured
  const hasIdentityEnv =
    Boolean(process.env.CONCORDIUM_CIS8004_INDEX) &&
    Boolean(process.env.CONCORDIUM_CIS8_INDEX) &&
    Boolean(process.env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS) &&
    Boolean(process.env.CONCORDIUM_NETWORK);

  console.log('\n4) CIS-8004 / CIS-8 Identity Simulation:');
  if (!hasIdentityEnv) {
    console.log(
      '   - Skipped (set CONCORDIUM_NETWORK, CONCORDIUM_CIS8004_INDEX, CONCORDIUM_CIS8_INDEX, CONCORDIUM_AGENT_ACCOUNT_ADDRESS and VERA_AGENT_* env vars to run identity simulation).'
    );
  } else {
    const hasAgentProfileEnv =
      Boolean(process.env.VERA_AGENT_NAME) &&
      Boolean(process.env.VERA_AGENT_DESCRIPTION) &&
      Boolean(process.env.VERA_AGENT_VERSION) &&
      Boolean(process.env.VERA_PROVIDER_ORG) &&
      Boolean(process.env.VERA_PROVIDER_URL) &&
      Boolean(process.env.VERA_AGENT_ENDPOINT_URL) &&
      Boolean(process.env.VERA_AGENT_ID);

    if (!hasAgentProfileEnv) {
      console.log(
        '   - Skipped AgentCard generation (VERA_AGENT_* / VERA_PROVIDER_* env vars not fully configured).'
      );
    } else {
      const card = generateAgentCard();
      const validation = validateAgentCard(card);
      const cardHash = calculateAgentCardHash(card);
      const cis8004Sim = await registerAgentInCIS8004({
        agentCard: card,
        agentUri: process.env.VERA_AGENT_CARD_URI?.trim() || `${card.endpoints[0].endpoint}/agent-card.json`,
        mode: 'simulate',
      });
      const cis8Sim = await linkExternalKeyCIS8({
        externalChain: process.env.VERA_EXTERNAL_CHAIN?.trim() || 'ethereum',
        keyType: 'ed25519',
        agentId: card.registrations[0].agentId,
        mode: 'simulate',
      });

      console.log(`   - AgentCard valid:          ${validation.valid}`);
      console.log(`   - AgentCard SHA-256:        ${cardHash}`);
      console.log(`   - CIS-8004 contract:        ${cis8004Sim.contract}`);
      console.log(`   - CIS-8 domain tag:         ${cis8Sim.canonicalMessage.domainTag}`);
      console.log(`   - CIS-8 message SHA-256:    ${cis8Sim.canonicalMessage.digestSha256}`);
      console.log(`   - CIS-8 ephemeral verified: ${cis8Sim.signatureVerified}`);
    }
  }
}

main().catch((err) => {
  console.error('Error running test vectors:', err);
  process.exit(1);
});
