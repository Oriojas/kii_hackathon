import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import {
  AccountAddress,
  AccountTransaction,
  AccountTransactionType,
  CcdAmount,
  ContractAddress,
  ContractName,
  DeployModulePayload,
  Energy,
  InitContractPayload,
  ModuleReference,
  Parameter,
  SequenceNumber,
  TransactionExpiry,
  calculateModuleReference,
  getEmbeddedModuleSchema,
  isInitContractSummary,
  isRejectTransaction,
  serializeInitContractParameters,
  versionedModuleSourceFromBuffer,
} from '@concordium/web-sdk';
import { ConcordiumGRPCNodeClient } from '@concordium/web-sdk/nodejs';
import { calculatePolicyHash } from './canonical.js';
import {
  createConcordiumClient,
  loadAccountSignerFromEnv,
  loadConcordiumConfig,
  requireEnv,
  signAndSendTransaction,
} from './client.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const BRIDGE_ROOT = resolve(__dirname, '..');
const REPO_ROOT = resolve(BRIDGE_ROOT, '..');

dotenv.config({ path: resolve(BRIDGE_ROOT, '.env') });
dotenv.config({ path: resolve(REPO_ROOT, '.env') });

export interface DeployedContractOutput {
  contractName: string;
  wasmPath: string;
  moduleRef: string;
  moduleAlreadyOnChain: boolean;
  deployTxHash?: string;
  initTxHash?: string;
  contractAddress?: {
    index: string;
    subindex: string;
  };
}

export interface DeployTestnetResult {
  ok: boolean;
  mode: 'submit' | 'simulate';
  network: string;
  grpcEndpoint: string;
  deployerAccount?: string;
  policyHash: string;
  evidenceRegistry: DeployedContractOutput;
  rewardPool: DeployedContractOutput;
  envUpdates: Record<string, string>;
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

function parseBoolEnv(value: string | undefined, defaultValue: boolean): boolean {
  if (!value || value.trim() === '') {
    return defaultValue;
  }
  const normalized = value.trim().toLowerCase();
  return normalized === '1' || normalized === 'true' || normalized === 'yes' || normalized === 'on';
}

function parseFloatEnv(name: string, defaultValue: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) {
    return defaultValue;
  }
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) {
    throw new Error(`Invalid numeric value for ${name}: ${raw}`);
  }
  return parsed;
}

function parseIntEnv(name: string, defaultValue: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) {
    return defaultValue;
  }
  const parsed = Number(raw);
  if (!Number.isInteger(parsed)) {
    throw new Error(`Invalid integer value for ${name}: ${raw}`);
  }
  return parsed;
}

function parseBigIntEnv(name: string, defaultValue: bigint): bigint {
  const raw = process.env[name]?.trim();
  if (!raw) {
    return defaultValue;
  }
  return BigInt(raw);
}

/**
 * Computes the deterministic policy_hash matching `vera_core.policy_engine.PolicyEngine`
 * unless `VERA_POLICY_HASH` (64 hex chars) is explicitly provided in environment.
 */
export function resolvePolicyHashFromEnv(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = env.VERA_POLICY_HASH?.trim();
  if (explicit) {
    const clean = explicit.startsWith('0x') ? explicit.slice(2) : explicit;
    if (!/^[0-9a-fA-F]{64}$/.test(clean)) {
      throw new Error('VERA_POLICY_HASH must be a 64-character hex string');
    }
    return clean.toLowerCase();
  }

  const scenario = env.VERA_DEFAULT_SCENARIO?.trim() || 'home_air';
  const policyVersion = env.VERA_POLICY_VERSION?.trim() || 'vera-policy-v1';
  const minCoverageRatio = parseFloatEnv('VERA_MIN_COVERAGE_RATIO', 0.85);
  const maxCalibrationAgeHours = parseIntEnv('VERA_MAX_CALIBRATION_AGE_HOURS', 720);

  if (scenario === 'business') {
    const zonesRaw = env.VERA_BUSINESS_ZONES?.trim() || 'Oficinas,Producción,Bodega,Logística';
    return calculatePolicyHash({
      policy_version: policyVersion,
      scenario_type: 'business',
      min_coverage_ratio: minCoverageRatio,
      max_calibration_age_hours: maxCalibrationAgeHours,
      required_zones: zonesRaw.split(',').map((z) => z.trim()).filter(Boolean),
      grid_emission_factor_kg_per_kwh: parseFloatEnv(
        'VERA_BUSINESS_GRID_EMISSION_FACTOR_KG_PER_KWH',
        0.164
      ),
      max_intensity_kg_per_unit: parseFloatEnv('VERA_BUSINESS_MAX_INTENSITY_KG_PER_UNIT', 0.45),
      min_reduction_pct: parseFloatEnv('VERA_BUSINESS_MIN_REDUCTION_PCT', 5.0),
    });
  }

  return calculatePolicyHash({
    policy_version: policyVersion,
    scenario_type: 'home_air',
    min_coverage_ratio: minCoverageRatio,
    max_calibration_age_hours: maxCalibrationAgeHours,
    expected_batch_readings: parseIntEnv('VERA_EXPECTED_BATCH_READINGS', 48),
    require_recovery_for_anomaly: parseBoolEnv(env.VERA_REQUIRE_RECOVERY_FOR_ANOMALY, true),
    baseline_max_raw: parseFloatEnv('VERA_HOME_AIR_BASELINE_MAX_RAW', 380.0),
    anomaly_min_raw: parseFloatEnv('VERA_HOME_AIR_ANOMALY_MIN_RAW', 520.0),
    recovery_max_raw: parseFloatEnv('VERA_HOME_AIR_RECOVERY_MAX_RAW', 390.0),
  });
}

function hexToByteArray32(hexStr: string): number[] {
  const clean = hexStr.startsWith('0x') ? hexStr.slice(2) : hexStr;
  if (!/^[0-9a-fA-F]{64}$/.test(clean)) {
    throw new Error(`Expected 32-byte hex string, got: ${hexStr}`);
  }
  return Array.from(Buffer.from(clean, 'hex'));
}

function hasPrivateKeyAvailable(env: NodeJS.ProcessEnv = process.env): boolean {
  const keyHex = env.CONCORDIUM_AGENT_PRIVATE_KEY?.trim();
  if (keyHex) {
    return true;
  }
  const keyPath = env.CONCORDIUM_AGENT_PRIVATE_KEY_PATH?.trim();
  if (!keyPath) {
    return false;
  }
  return existsSync(resolveProjectPath(keyPath));
}

function loadWasmModule(wasmPath: string) {
  if (!existsSync(wasmPath)) {
    throw new Error(`Contract WASM file not found at: ${wasmPath}`);
  }
  const buf = readFileSync(wasmPath);
  const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  const versionedSource = versionedModuleSourceFromBuffer(ab);
  const moduleRef = calculateModuleReference(versionedSource);
  return { versionedSource, moduleRef };
}

async function isModuleDeployed(
  client: ConcordiumGRPCNodeClient,
  moduleRef: ModuleReference.Type
): Promise<boolean> {
  try {
    await client.getModuleSource(moduleRef);
    return true;
  } catch {
    return false;
  }
}

async function deployModuleIfNeeded(
  client: ConcordiumGRPCNodeClient,
  sender: AccountAddress.Type,
  signer: ReturnType<typeof loadAccountSignerFromEnv>['signer'],
  versionedSource: ReturnType<typeof versionedModuleSourceFromBuffer>,
  moduleRef: ModuleReference.Type,
  expiryMinutes: number
): Promise<{ alreadyOnChain: boolean; txHash?: string }> {
  if (await isModuleDeployed(client, moduleRef)) {
    return { alreadyOnChain: true };
  }

  const { nonce } = await client.getNextAccountNonce(sender);
  const payload: DeployModulePayload = {
    version: versionedSource.version,
    source: versionedSource.source,
  };
  const tx: AccountTransaction = {
    type: AccountTransactionType.DeployModule,
    header: {
      sender,
      nonce,
      expiry: TransactionExpiry.futureMinutes(expiryMinutes),
    },
    payload,
  };

  const result = await signAndSendTransaction(client, tx, signer, true);
  if (result.summary && isRejectTransaction(result.summary.summary)) {
    throw new Error(
      `DeployModule rejected for ${moduleRef.moduleRef}: ${JSON.stringify(
        result.summary.summary.rejectReason
      )}`
    );
  }
  return {
    alreadyOnChain: false,
    txHash: result.transactionHash.toString(),
  };
}

async function initContractInstance(
  client: ConcordiumGRPCNodeClient,
  sender: AccountAddress.Type,
  signer: ReturnType<typeof loadAccountSignerFromEnv>['signer'],
  params: {
    moduleRef: ModuleReference.Type;
    contractName: string;
    parameter: Parameter.Type;
    amountMicroCcd: bigint;
    maxEnergy: bigint;
    expiryMinutes: number;
  }
): Promise<{ txHash: string; address: ContractAddress.Type }> {
  const { nonce } = await client.getNextAccountNonce(sender);
  const payload: InitContractPayload = {
    amount: CcdAmount.fromMicroCcd(params.amountMicroCcd),
    moduleRef: params.moduleRef,
    initName: ContractName.fromString(params.contractName),
    param: params.parameter,
    maxContractExecutionEnergy: Energy.create(params.maxEnergy),
  };

  const tx: AccountTransaction = {
    type: AccountTransactionType.InitContract,
    header: {
      sender,
      nonce,
      expiry: TransactionExpiry.futureMinutes(params.expiryMinutes),
    },
    payload,
  };

  const result = await signAndSendTransaction(client, tx, signer, true);
  if (!result.summary) {
    throw new Error(`Missing finalization summary for ${params.contractName} init`);
  }
  if (isRejectTransaction(result.summary.summary)) {
    throw new Error(
      `InitContract rejected for ${params.contractName}: ${JSON.stringify(
        result.summary.summary.rejectReason
      )}`
    );
  }
  if (!isInitContractSummary(result.summary.summary)) {
    throw new Error(`Unexpected transaction summary type for ${params.contractName} init`);
  }

  return {
    txHash: result.transactionHash.toString(),
    address: result.summary.summary.contractInitialized.address,
  };
}

async function runDeploy(): Promise<DeployTestnetResult> {
  const args = process.argv.slice(2);
  const forceSimulate =
    args.includes('--simulate') ||
    args.includes('--dry-run') ||
    parseBoolEnv(process.env.VERA_DEPLOY_SIMULATE, false);

  const evidenceWasmPath = resolveProjectPath(
    process.env.CONCORDIUM_EVIDENCE_REGISTRY_WASM_PATH?.trim() ||
      '../contracts/target/evidence_registry.wasm.v1'
  );
  const rewardPoolWasmPath = resolveProjectPath(
    process.env.CONCORDIUM_REWARD_POOL_WASM_PATH?.trim() ||
      '../contracts/target/reward_pool.wasm.v1'
  );

  const evidenceLoaded = loadWasmModule(evidenceWasmPath);
  const rewardLoaded = loadWasmModule(rewardPoolWasmPath);
  const policyHash = resolvePolicyHashFromEnv();

  const network = process.env.CONCORDIUM_NETWORK?.trim() || 'testnet';
  const grpcHost = process.env.CONCORDIUM_GRPC_HOST?.trim() || '';
  const grpcPort = process.env.CONCORDIUM_GRPC_PORT?.trim() || '';
  const grpcEndpoint = grpcHost && grpcPort ? `${grpcHost}:${grpcPort}` : 'unconfigured';

  if (forceSimulate || !hasPrivateKeyAvailable()) {
    const simulatedErIndex = process.env.CONCORDIUM_EVIDENCE_REGISTRY_INDEX?.trim() || '1000';
    const simulatedErSub = process.env.CONCORDIUM_EVIDENCE_REGISTRY_SUBINDEX?.trim() || '0';
    const simulatedRpIndex = process.env.CONCORDIUM_REWARD_POOL_INDEX?.trim() || '1001';
    const simulatedRpSub = process.env.CONCORDIUM_REWARD_POOL_SUBINDEX?.trim() || '0';

    // Verify embedded schema parameter serialization even in simulate mode
    const rpSchema = await getEmbeddedModuleSchema(rewardLoaded.versionedSource);
    if (rpSchema) {
      const schemaVersion = rpSchema.type === 'unversioned' ? rpSchema.version : undefined;
      serializeInitContractParameters(
        ContractName.fromString('reward_pool'),
        {
          policy_hash: hexToByteArray32(policyHash),
          evidence_registry: {
            Some: [{ index: Number(simulatedErIndex), subindex: Number(simulatedErSub) }],
          },
        },
        rpSchema.buffer,
        schemaVersion
      );
    }

    return {
      ok: true,
      mode: 'simulate',
      network,
      grpcEndpoint,
      deployerAccount: process.env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim() || undefined,
      policyHash,
      evidenceRegistry: {
        contractName: 'evidence_registry',
        wasmPath: evidenceWasmPath,
        moduleRef: evidenceLoaded.moduleRef.moduleRef,
        moduleAlreadyOnChain: false,
        contractAddress: {
          index: simulatedErIndex,
          subindex: simulatedErSub,
        },
      },
      rewardPool: {
        contractName: 'reward_pool',
        wasmPath: rewardPoolWasmPath,
        moduleRef: rewardLoaded.moduleRef.moduleRef,
        moduleAlreadyOnChain: false,
        contractAddress: {
          index: simulatedRpIndex,
          subindex: simulatedRpSub,
        },
      },
      envUpdates: {
        CONCORDIUM_EVIDENCE_REGISTRY_INDEX: simulatedErIndex,
        CONCORDIUM_EVIDENCE_REGISTRY_SUBINDEX: simulatedErSub,
        CONCORDIUM_REWARD_POOL_INDEX: simulatedRpIndex,
        CONCORDIUM_REWARD_POOL_SUBINDEX: simulatedRpSub,
      },
    };
  }

  const config = loadConcordiumConfig();
  const client = createConcordiumClient({
    host: config.grpcHost,
    port: config.grpcPort,
    useTls: config.useTls,
    timeoutMs: config.timeoutMs,
  });

  // Resolve private key path cleanly if relative to repo root
  const envForSigner: NodeJS.ProcessEnv = { ...process.env };
  if (envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH) {
    envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH = resolveProjectPath(
      envForSigner.CONCORDIUM_AGENT_PRIVATE_KEY_PATH
    );
  }
  const loadedSigner = loadAccountSignerFromEnv(envForSigner);
  const senderAddress =
    loadedSigner.address ??
    AccountAddress.fromBase58(requireEnv('CONCORDIUM_AGENT_ACCOUNT_ADDRESS'));

  const maxEnergy = parseBigIntEnv('CONCORDIUM_INIT_MAX_ENERGY', 30000n);
  const expiryMinutes = parseIntEnv('CONCORDIUM_TX_EXPIRY_MINUTES', 10);
  const rewardPoolInitAmount = parseBigIntEnv('VERA_REWARD_POOL_INIT_MICRO_CCD', 0n);

  // 1. Deploy and initialize evidence_registry
  const erDeploy = await deployModuleIfNeeded(
    client,
    senderAddress,
    loadedSigner.signer,
    evidenceLoaded.versionedSource,
    evidenceLoaded.moduleRef,
    expiryMinutes
  );

  const erInit = await initContractInstance(client, senderAddress, loadedSigner.signer, {
    moduleRef: evidenceLoaded.moduleRef,
    contractName: 'evidence_registry',
    parameter: Parameter.empty(),
    amountMicroCcd: 0n,
    maxEnergy,
    expiryMinutes,
  });

  // 2. Deploy and initialize reward_pool linked to evidence_registry
  const rpDeploy = await deployModuleIfNeeded(
    client,
    senderAddress,
    loadedSigner.signer,
    rewardLoaded.versionedSource,
    rewardLoaded.moduleRef,
    expiryMinutes
  );

  const rpSchema = await getEmbeddedModuleSchema(rewardLoaded.versionedSource);
  if (!rpSchema) {
    throw new Error('Missing embedded schema in reward_pool.wasm.v1');
  }
  const schemaVersion = rpSchema.type === 'unversioned' ? rpSchema.version : undefined;
  const rpInitParam = serializeInitContractParameters(
    ContractName.fromString('reward_pool'),
    {
      policy_hash: hexToByteArray32(policyHash),
      evidence_registry: {
        Some: [
          {
            index: Number(erInit.address.index),
            subindex: Number(erInit.address.subindex),
          },
        ],
      },
    },
    rpSchema.buffer,
    schemaVersion
  );

  const rpInit = await initContractInstance(client, senderAddress, loadedSigner.signer, {
    moduleRef: rewardLoaded.moduleRef,
    contractName: 'reward_pool',
    parameter: rpInitParam,
    amountMicroCcd: rewardPoolInitAmount,
    maxEnergy,
    expiryMinutes,
  });

  const erIndexStr = erInit.address.index.toString();
  const erSubStr = erInit.address.subindex.toString();
  const rpIndexStr = rpInit.address.index.toString();
  const rpSubStr = rpInit.address.subindex.toString();

  return {
    ok: true,
    mode: 'submit',
    network: config.network,
    grpcEndpoint: `${config.grpcHost}:${config.grpcPort}`,
    deployerAccount: senderAddress.address,
    policyHash,
    evidenceRegistry: {
      contractName: 'evidence_registry',
      wasmPath: evidenceWasmPath,
      moduleRef: evidenceLoaded.moduleRef.moduleRef,
      moduleAlreadyOnChain: erDeploy.alreadyOnChain,
      deployTxHash: erDeploy.txHash,
      initTxHash: erInit.txHash,
      contractAddress: {
        index: erIndexStr,
        subindex: erSubStr,
      },
    },
    rewardPool: {
      contractName: 'reward_pool',
      wasmPath: rewardPoolWasmPath,
      moduleRef: rewardLoaded.moduleRef.moduleRef,
      moduleAlreadyOnChain: rpDeploy.alreadyOnChain,
      deployTxHash: rpDeploy.txHash,
      initTxHash: rpInit.txHash,
      contractAddress: {
        index: rpIndexStr,
        subindex: rpSubStr,
      },
    },
    envUpdates: {
      CONCORDIUM_EVIDENCE_REGISTRY_INDEX: erIndexStr,
      CONCORDIUM_EVIDENCE_REGISTRY_SUBINDEX: erSubStr,
      CONCORDIUM_REWARD_POOL_INDEX: rpIndexStr,
      CONCORDIUM_REWARD_POOL_SUBINDEX: rpSubStr,
    },
  };
}

runDeploy()
  .then((output) => {
    process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
  })
  .catch((err: unknown) => {
    const message = err instanceof Error ? err.message : String(err);
    process.stdout.write(`${JSON.stringify({ ok: false, error: message }, null, 2)}\n`);
    process.exit(1);
  });
