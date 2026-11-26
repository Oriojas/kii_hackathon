import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import {
  AccountAddress,
  ContractAddress,
  ContractName,
  EntrypointName,
  getEmbeddedModuleSchema,
  isRejectTransaction,
  serializeUpdateContractParameters,
  versionedModuleSourceFromBuffer,
} from '@concordium/web-sdk';
import { ConcordiumGRPCNodeClient } from '@concordium/web-sdk/nodejs';
import {
  calculateAgentCardHash,
  generateAgentCard,
  linkExternalKeyCIS8,
  validateAgentCard,
} from './agentIdentity.js';
import { sha256Hex } from './canonical.js';
import {
  createConcordiumClient,
  getContractAddressFromEnv,
  getContractInstanceInfo,
  getNodeStatus,
  loadAccountSignerFromEnv,
  submitContractUpdate,
} from './client.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const BRIDGE_ROOT = resolve(__dirname, '..');
const REPO_ROOT = resolve(BRIDGE_ROOT, '..');

dotenv.config({ path: resolve(BRIDGE_ROOT, '.env') });
dotenv.config({ path: resolve(REPO_ROOT, '.env') });

type CliCommand = 'status' | 'verify-badge' | 'register-evidence' | 'settle-reward';

interface ContractStatusEntry {
  configured: boolean;
  index?: string;
  subindex?: string;
  on_chain?: boolean;
  contract_name?: string;
  methods?: string[];
  error?: string;
}

function resolveProjectPath(rawPath: string): string {
  const trimmed = rawPath.trim();
  if (existsSync(trimmed)) {
    return resolve(trimmed);
  }
  const fromCwd = resolve(process.cwd(), trimmed);
  if (existsSync(fromCwd)) {
    return fromCwd;
  }
  const fromBridge = resolve(BRIDGE_ROOT, trimmed);
  if (existsSync(fromBridge)) {
    return fromBridge;
  }
  return resolve(REPO_ROOT, trimmed);
}

function hasPrivateKeyConfigured(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env.CONCORDIUM_AGENT_PRIVATE_KEY?.trim()) {
    return true;
  }
  const keyPath = env.CONCORDIUM_AGENT_PRIVATE_KEY_PATH?.trim();
  if (!keyPath) {
    return false;
  }
  return existsSync(resolveProjectPath(keyPath));
}

function parseGrpcConfig(env: NodeJS.ProcessEnv = process.env) {
  const host = env.CONCORDIUM_GRPC_HOST?.trim() || '';
  const portRaw = env.CONCORDIUM_GRPC_PORT?.trim() || '';
  const port = portRaw ? Number(portRaw) : 0;
  const network = env.CONCORDIUM_NETWORK?.trim() || 'testnet';
  const configured = Boolean(host && Number.isInteger(port) && port > 0);
  const timeoutMsRaw = env.CONCORDIUM_GRPC_TIMEOUT_MS?.trim();
  const timeoutMs =
    timeoutMsRaw && Number.isInteger(Number(timeoutMsRaw)) && Number(timeoutMsRaw) > 0
      ? Number(timeoutMsRaw)
      : 6000;
  const tlsEnv = env.CONCORDIUM_GRPC_TLS?.trim().toLowerCase();
  const useTls =
    tlsEnv !== undefined && tlsEnv !== ''
      ? tlsEnv === 'true' || tlsEnv === '1'
      : port === 20000 || network.toLowerCase() !== 'local';

  return { host, port, network, configured, timeoutMs, useTls };
}

function parseFlags(argv: string[]) {
  let command: string | undefined;
  let inputJson: string | undefined;
  let inputFile: string | undefined;
  let forceSimulate = false;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--simulate' || arg === '--dry-run') {
      forceSimulate = true;
    } else if (arg === '--input' || arg === '--json') {
      inputJson = argv[i + 1];
      i += 1;
    } else if (arg === '--file') {
      inputFile = argv[i + 1];
      i += 1;
    } else if (!arg.startsWith('-') && !command) {
      command = arg;
    }
  }

  return { command, inputJson, inputFile, forceSimulate };
}

function readJsonInput(inputJson?: string, inputFile?: string): Record<string, unknown> {
  if (inputJson) {
    return JSON.parse(inputJson) as Record<string, unknown>;
  }
  if (inputFile) {
    return JSON.parse(readFileSync(resolveProjectPath(inputFile), 'utf8')) as Record<string, unknown>;
  }
  if (!process.stdin.isTTY) {
    const stdinText = readFileSync(0, 'utf8').trim();
    if (stdinText) {
      return JSON.parse(stdinText) as Record<string, unknown>;
    }
  }
  return {};
}

function normalizeBytes32Hex(value: unknown, fallbackSeed: string): string {
  if (typeof value === 'string' && value.trim()) {
    const clean = value.trim().startsWith('0x') ? value.trim().slice(2) : value.trim();
    if (/^[0-9a-fA-F]{64}$/.test(clean)) {
      return clean.toLowerCase();
    }
    return sha256Hex(value.trim());
  }
  return sha256Hex(fallbackSeed);
}

function hex32ToByteArray(hex: string): number[] {
  return Array.from(Buffer.from(hex, 'hex'));
}

function isValidConcordiumAddress(address: string): boolean {
  try {
    AccountAddress.fromBase58(address);
    return true;
  } catch {
    return false;
  }
}

async function serializeContractParamsFromWasm(params: {
  wasmEnvVar: string;
  defaultWasmPath: string;
  contractName: string;
  entrypoint: string;
  payload: Record<string, unknown>;
}): Promise<string> {
  const wasmPath = resolveProjectPath(
    process.env[params.wasmEnvVar]?.trim() || params.defaultWasmPath
  );
  if (!existsSync(wasmPath)) {
    return Buffer.from(JSON.stringify(params.payload), 'utf8').toString('hex');
  }
  const buf = readFileSync(wasmPath);
  const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  const versioned = versionedModuleSourceFromBuffer(ab);
  const schema = await getEmbeddedModuleSchema(versioned);
  if (!schema) {
    return Buffer.from(JSON.stringify(params.payload), 'utf8').toString('hex');
  }
  const schemaVersion = schema.type === 'unversioned' ? schema.version : undefined;
  const serialized = serializeUpdateContractParameters(
    ContractName.fromString(params.contractName),
    EntrypointName.fromString(params.entrypoint),
    params.payload,
    schema.buffer,
    schemaVersion
  );
  return Buffer.from(serialized.buffer).toString('hex');
}

async function inspectContract(
  client: ConcordiumGRPCNodeClient | null,
  prefix: string
): Promise<ContractStatusEntry> {
  const addr = getContractAddressFromEnv(prefix);
  if (!addr) {
    return { configured: false };
  }
  const base: ContractStatusEntry = {
    configured: true,
    index: addr.index.toString(),
    subindex: addr.subindex.toString(),
  };
  if (!client) {
    return { ...base, on_chain: false };
  }
  try {
    const info = await getContractInstanceInfo(client, addr);
    return {
      ...base,
      on_chain: true,
      contract_name: info.name.value,
      methods: info.methods.map((m) => m.value),
    };
  } catch (err: unknown) {
    return {
      ...base,
      on_chain: false,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

function buildAgentCardFromEnv() {
  const env = process.env;
  const cis8004Addr = getContractAddressFromEnv('CONCORDIUM_CIS8004');
  const network = env.CONCORDIUM_NETWORK?.trim() || 'testnet';
  const port = env.PORT?.trim() || '8086';
  const endpointUrl =
    env.VERA_AGENT_ENDPOINT_URL?.trim() || `http://localhost:${port}`;
  const providerUrl = env.VERA_PROVIDER_URL?.trim() || endpointUrl;

  return generateAgentCard({
    name: env.VERA_AGENT_NAME?.trim() || env.VERA_AGENT_DISPLAY_NAME?.trim() || 'VERA Environmental Agent',
    description:
      env.VERA_AGENT_DESCRIPTION?.trim() ||
      'Verified Environmental Responsibility Agent for Concordium Trusted Agents',
    version: env.VERA_AGENT_VERSION?.trim() || '1.0.0',
    providerOrganization: env.VERA_PROVIDER_ORG?.trim() || 'VERA Protocol',
    providerUrl,
    endpoints: [
      {
        name: env.VERA_AGENT_ENDPOINT_NAME?.trim() || 'VERA-API',
        endpoint: endpointUrl,
        protocolBinding: env.VERA_AGENT_PROTOCOL_BINDING?.trim() || 'HTTP+JSON',
        version: env.VERA_AGENT_VERSION?.trim() || '1.0.0',
      },
    ],
    agentId: env.VERA_AGENT_ID?.trim() || 'vera-agent-demo-01',
    network,
    cis8004Index: cis8004Addr ? cis8004Addr.index : 1002,
    cis8004Subindex: cis8004Addr ? cis8004Addr.subindex : 0,
    active: env.VERA_AGENT_ACTIVE?.trim().toLowerCase() === 'true',
  });
}

async function handleStatus() {
  const grpc = parseGrpcConfig();
  const hasKey = hasPrivateKeyConfigured();
  let client: ConcordiumGRPCNodeClient | null = null;
  let connected = false;
  let nodeError: string | undefined;
  let bestBlock: string | undefined;
  let lastFinalizedBlock: string | undefined;
  let blockHeight: string | undefined;
  let genesisIndex: number | undefined;
  let protocolVersion: string | undefined;

  if (grpc.configured) {
    try {
      client = createConcordiumClient({
        host: grpc.host,
        port: grpc.port,
        useTls: grpc.useTls,
        timeoutMs: grpc.timeoutMs,
      });
      const { consensusStatus, blockInfo } = await getNodeStatus(client);
      connected = true;
      bestBlock = consensusStatus.bestBlock?.toString();
      lastFinalizedBlock = consensusStatus.lastFinalizedBlock?.toString();
      blockHeight = blockInfo.blockHeight?.toString();
      genesisIndex = consensusStatus.genesisIndex;
      protocolVersion = String(consensusStatus.protocolVersion);
    } catch (err: unknown) {
      nodeError = err instanceof Error ? err.message : String(err);
      client = null;
    }
  } else {
    nodeError = 'CONCORDIUM_GRPC_HOST / CONCORDIUM_GRPC_PORT not configured in environment';
  }

  const [evidenceRegistry, rewardPool, cis8004, cis8] = await Promise.all([
    inspectContract(client, 'CONCORDIUM_EVIDENCE_REGISTRY'),
    inspectContract(client, 'CONCORDIUM_REWARD_POOL'),
    inspectContract(client, 'CONCORDIUM_CIS8004'),
    inspectContract(client, 'CONCORDIUM_CIS8'),
  ]);

  let agentCardHash: string | undefined;
  let agentCardValid = false;
  try {
    const card = buildAgentCardFromEnv();
    agentCardValid = validateAgentCard(card).valid;
    if (agentCardValid) {
      agentCardHash = calculateAgentCardHash(card);
    }
  } catch {
    agentCardValid = false;
  }

  return {
    ok: true,
    network: grpc.network,
    mode: hasKey && connected ? 'live' : 'simulate',
    has_private_key: hasKey,
    grpc: {
      configured: grpc.configured,
      connected,
      host: grpc.host || null,
      port: grpc.port || null,
      use_tls: grpc.useTls,
      best_block: bestBlock ?? null,
      last_finalized_block: lastFinalizedBlock ?? null,
      block_height: blockHeight ?? null,
      genesis_index: genesisIndex ?? null,
      protocol_version: protocolVersion ?? null,
      error: nodeError ?? null,
    },
    contracts: {
      evidence_registry: evidenceRegistry,
      reward_pool: rewardPool,
      cis8004,
      cis8,
    },
    cis8004_badge: {
      standard: 'CIS-8004',
      verified: Boolean(cis8004.on_chain) || agentCardValid,
      on_chain_verified: Boolean(cis8004.on_chain),
      agent_card_valid: agentCardValid,
      agent_card_hash: agentCardHash ?? null,
      contract: cis8004.configured ? `${cis8004.index}.${cis8004.subindex}` : null,
      agent_account: process.env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim() || null,
    },
  };
}

async function handleVerifyBadge() {
  const status = await handleStatus();
  const card = buildAgentCardFromEnv();
  const validation = validateAgentCard(card);
  const cardHash = validation.valid ? calculateAgentCardHash(card) : null;

  let cis8Simulation: Record<string, unknown> | null = null;
  const cis8Contract = getContractAddressFromEnv('CONCORDIUM_CIS8');
  const agentAccount = process.env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim();
  if (cis8Contract && agentAccount && isValidConcordiumAddress(agentAccount)) {
    const res = await linkExternalKeyCIS8({
      concordiumAccount: agentAccount,
      externalChain: process.env.VERA_EXTERNAL_CHAIN?.trim() || 'ethereum',
      keyType: 'ed25519',
      agentId: card.registrations[0].agentId,
      cis8Contract,
      mode: 'simulate',
    });
    cis8Simulation = {
      cis8_contract: res.cis8Contract,
      domain_tag: res.canonicalMessage.domainTag,
      digest_sha256: res.canonicalMessage.digestSha256,
      signature_verified: res.signatureVerified ?? false,
    };
  }

  return {
    ok: true,
    mode: status.mode,
    network: status.network,
    verified: validation.valid,
    on_chain_verified: status.cis8004_badge.on_chain_verified,
    errors: validation.errors,
    agent_card_hash: cardHash,
    cis8004: status.contracts.cis8004,
    cis8: cis8Simulation ?? status.contracts.cis8,
    agent_card: card,
  };
}

async function handleRegisterEvidence(input: Record<string, unknown>, forceSimulate: boolean) {
  const grpc = parseGrpcConfig();
  const hasKey = hasPrivateKeyConfigured();
  const contractAddr: ContractAddress.Type =
    getContractAddressFromEnv('CONCORDIUM_EVIDENCE_REGISTRY') ?? ContractAddress.create(1000n, 0n);

  const evidenceRootHex = normalizeBytes32Hex(
    input.evidence_root,
    JSON.stringify(input)
  );
  const evidenceIdHex = normalizeBytes32Hex(
    input.evidence_id,
    `evidence:${evidenceRootHex}`
  );
  const sourceTypeRaw = String(input.source_type ?? 'physical').toLowerCase();
  const sourceTypeVariant =
    sourceTypeRaw === 'synthetic' || sourceTypeRaw === 'synthetic_declared'
      ? { Synthetic: [] }
      : { Physical: [] };

  const defaultExpiry = new Date(Date.now() + 30 * 24 * 3600 * 1000)
    .toISOString()
    .replace(/\.\d{3}Z$/, 'Z');
  const calibrationExpiresAt = String(input.calibration_expires_at || defaultExpiry);
  const metadataUri = String(
    input.metadata_uri || `vera://evidence/${evidenceIdHex.slice(0, 16)}`
  );

  const contractPayload = {
    evidence_id: hex32ToByteArray(evidenceIdHex),
    evidence_root: hex32ToByteArray(evidenceRootHex),
    source_type: sourceTypeVariant,
    calibration_expires_at: calibrationExpiresAt,
    metadata_uri: metadataUri,
  };

  const parameterHex = await serializeContractParamsFromWasm({
    wasmEnvVar: 'CONCORDIUM_EVIDENCE_REGISTRY_WASM_PATH',
    defaultWasmPath: '../contracts/target/evidence_registry.wasm.v1',
    contractName: 'evidence_registry',
    entrypoint: 'register_evidence',
    payload: contractPayload,
  });

  if (forceSimulate || !hasKey || !grpc.configured) {
    const simTxHash = `sim_${sha256Hex(`register:${evidenceIdHex}:${evidenceRootHex}`).slice(0, 56)}`;
    return {
      ok: true,
      mode: 'simulate',
      simulated: true,
      network: grpc.network,
      contract: `${contractAddr.index.toString()}.${contractAddr.subindex.toString()}`,
      receive_name: 'evidence_registry.register_evidence',
      evidence_id: evidenceIdHex,
      evidence_root: evidenceRootHex,
      source_type: sourceTypeRaw,
      calibration_expires_at: calibrationExpiresAt,
      metadata_uri: metadataUri,
      parameter_hex: parameterHex,
      tx_hash: simTxHash,
      reason: !hasKey
        ? 'No private key configured; executed deterministic local simulation'
        : 'Simulation requested or gRPC unconfigured',
    };
  }

  const client = createConcordiumClient({
    host: grpc.host,
    port: grpc.port,
    useTls: grpc.useTls,
    timeoutMs: grpc.timeoutMs,
  });
  const envForSigner: NodeJS.ProcessEnv = { ...process.env };
  if (envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH) {
    envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH = resolveProjectPath(
      envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH
    );
  }
  const signerLoaded = loadAccountSignerFromEnv(envForSigner);
  const submitted = await submitContractUpdate(client, {
    contract: contractAddr,
    receiveName: 'evidence_registry.register_evidence',
    parameterHex,
    sender: signerLoaded.address,
    signer: signerLoaded.signer,
    waitForFinalization: true,
  });

  if (submitted.summary && isRejectTransaction(submitted.summary.summary)) {
    return {
      ok: false,
      mode: 'submit',
      simulated: false,
      network: grpc.network,
      contract: `${contractAddr.index.toString()}.${contractAddr.subindex.toString()}`,
      tx_hash: submitted.transactionHash.toString(),
      error: JSON.stringify(submitted.summary.summary.rejectReason),
    };
  }

  return {
    ok: true,
    mode: 'submit',
    simulated: false,
    network: grpc.network,
    contract: `${contractAddr.index.toString()}.${contractAddr.subindex.toString()}`,
    receive_name: 'evidence_registry.register_evidence',
    evidence_id: evidenceIdHex,
    evidence_root: evidenceRootHex,
    parameter_hex: parameterHex,
    tx_hash: submitted.transactionHash.toString(),
  };
}

async function handleSettleReward(input: Record<string, unknown>, forceSimulate: boolean) {
  const grpc = parseGrpcConfig();
  const hasKey = hasPrivateKeyConfigured();
  const contractAddr: ContractAddress.Type =
    getContractAddressFromEnv('CONCORDIUM_REWARD_POOL') ?? ContractAddress.create(1001n, 0n);

  const evidenceIdHex = normalizeBytes32Hex(
    input.evidence_id ?? input.evidence_root,
    `settle:${JSON.stringify(input)}`
  );
  const policyHashHex = normalizeBytes32Hex(
    input.policy_hash,
    'vera-policy-default'
  );
  const amountMicroCcd = String(
    input.amount_micro_ccd ??
      process.env.VERA_REWARD_AMOUNT_MICRO_CCD?.trim() ??
      '1000000'
  );
  const recipientRequested = String(
    input.recipient ??
      process.env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim() ??
      ''
  );
  const defaultExpiry = new Date(Date.now() + 30 * 24 * 3600 * 1000)
    .toISOString()
    .replace(/\.\d{3}Z$/, 'Z');
  const calibrationExpiresAt = String(input.calibration_expires_at || defaultExpiry);

  const zeroFallbackAddress = AccountAddress.fromBuffer(new Uint8Array(32).buffer).address;
  const recipientForSerialization = isValidConcordiumAddress(recipientRequested)
    ? recipientRequested
    : zeroFallbackAddress;

  const contractPayload = {
    recipient: recipientForSerialization,
    evidence_id: hex32ToByteArray(evidenceIdHex),
    policy_hash: hex32ToByteArray(policyHashHex),
    amount: amountMicroCcd,
    calibration_expires_at: calibrationExpiresAt,
  };

  const parameterHex = await serializeContractParamsFromWasm({
    wasmEnvVar: 'CONCORDIUM_REWARD_POOL_WASM_PATH',
    defaultWasmPath: '../contracts/target/reward_pool.wasm.v1',
    contractName: 'reward_pool',
    entrypoint: 'settle_reward',
    payload: contractPayload,
  });

  if (forceSimulate || !hasKey || !grpc.configured) {
    const simTxHash = `sim_${sha256Hex(`settle:${evidenceIdHex}:${policyHashHex}:${amountMicroCcd}`).slice(0, 56)}`;
    return {
      ok: true,
      mode: 'simulate',
      simulated: true,
      network: grpc.network,
      contract: `${contractAddr.index.toString()}.${contractAddr.subindex.toString()}`,
      receive_name: 'reward_pool.settle_reward',
      evidence_id: evidenceIdHex,
      policy_hash: policyHashHex,
      recipient: recipientRequested || recipientForSerialization,
      amount_micro_ccd: Number(amountMicroCcd),
      calibration_expires_at: calibrationExpiresAt,
      parameter_hex: parameterHex,
      tx_hash: simTxHash,
      reason: !hasKey
        ? 'No private key configured; executed deterministic local simulation'
        : 'Simulation requested or gRPC unconfigured',
    };
  }

  if (!isValidConcordiumAddress(recipientRequested)) {
    throw new Error(`Invalid Concordium recipient address for live settlement: ${recipientRequested}`);
  }

  const client = createConcordiumClient({
    host: grpc.host,
    port: grpc.port,
    useTls: grpc.useTls,
    timeoutMs: grpc.timeoutMs,
  });
  const envForSigner: NodeJS.ProcessEnv = { ...process.env };
  if (envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH) {
    envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH = resolveProjectPath(
      envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH
    );
  }
  const signerLoaded = loadAccountSignerFromEnv(envForSigner);
  const submitted = await submitContractUpdate(client, {
    contract: contractAddr,
    receiveName: 'reward_pool.settle_reward',
    parameterHex,
    sender: signerLoaded.address,
    signer: signerLoaded.signer,
    waitForFinalization: true,
  });

  if (submitted.summary && isRejectTransaction(submitted.summary.summary)) {
    return {
      ok: false,
      mode: 'submit',
      simulated: false,
      network: grpc.network,
      contract: `${contractAddr.index.toString()}.${contractAddr.subindex.toString()}`,
      tx_hash: submitted.transactionHash.toString(),
      error: JSON.stringify(submitted.summary.summary.rejectReason),
    };
  }

  return {
    ok: true,
    mode: 'submit',
    simulated: false,
    network: grpc.network,
    contract: `${contractAddr.index.toString()}.${contractAddr.subindex.toString()}`,
    receive_name: 'reward_pool.settle_reward',
    evidence_id: evidenceIdHex,
    policy_hash: policyHashHex,
    recipient: recipientRequested,
    amount_micro_ccd: Number(amountMicroCcd),
    calibration_expires_at: calibrationExpiresAt,
    parameter_hex: parameterHex,
    tx_hash: submitted.transactionHash.toString(),
  };
}

async function main() {
  const { command, inputJson, inputFile, forceSimulate } = parseFlags(process.argv.slice(2));
  const validCommands: CliCommand[] = [
    'status',
    'verify-badge',
    'register-evidence',
    'settle-reward',
  ];

  if (!command || !validCommands.includes(command as CliCommand)) {
    process.stdout.write(
      `${JSON.stringify({
        ok: false,
        error: `Unknown or missing command. Expected one of: ${validCommands.join(', ')}`,
      })}\n`
    );
    process.exit(1);
  }

  const cmd = command as CliCommand;
  let result: unknown;

  if (cmd === 'status') {
    result = await handleStatus();
  } else if (cmd === 'verify-badge') {
    result = await handleVerifyBadge();
  } else if (cmd === 'register-evidence') {
    const input = readJsonInput(inputJson, inputFile);
    result = await handleRegisterEvidence(input, forceSimulate);
  } else {
    const input = readJsonInput(inputJson, inputFile);
    result = await handleSettleReward(input, forceSimulate);
  }

  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch((err: unknown) => {
  const message = err instanceof Error ? err.message : String(err);
  process.stdout.write(`${JSON.stringify({ ok: false, error: message })}\n`);
  process.exit(1);
});
