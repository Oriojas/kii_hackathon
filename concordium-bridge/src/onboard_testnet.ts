import { existsSync, readFileSync, realpathSync } from 'node:fs';
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
  EntrypointName,
  InitContractPayload,
  ModuleReference,
  Parameter,
  TransactionExpiry,
  calculateModuleReference,
  getEmbeddedModuleSchema,
  isInitContractSummary,
  isRejectTransaction,
  serializeInitContractParameters,
  serializeUpdateContractParameters,
  versionedModuleSourceFromBuffer,
} from '@concordium/web-sdk';
import { ConcordiumGRPCNodeClient } from '@concordium/web-sdk/nodejs';
import {
  calculateAgentCardHash,
  generateAgentCard,
  linkExternalKeyCIS8,
  registerAgentInCIS8004,
  validateAgentCard,
  type AgentCard,
} from './agentIdentity.js';
import { calculatePolicyHash, sha256Hex } from './canonical.js';
import {
  createConcordiumClient,
  getContractAddressFromEnv,
  getContractInstanceInfo,
  getNodeStatus,
  loadAccountSignerFromEnv,
  requireEnv,
  signAndSendTransaction,
} from './client.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const BRIDGE_ROOT = resolve(__dirname, '..');
const REPO_ROOT = resolve(BRIDGE_ROOT, '..');

dotenv.config({ path: resolve(BRIDGE_ROOT, '.env') });
dotenv.config({ path: resolve(REPO_ROOT, '.env') });

export interface EnvValidationReport {
  mode: 'simulate' | 'live';
  configuredVars: string[];
  missingRequiredForLive: string[];
  missingOptionalOrSimulated: string[];
  hasPrivateKey: boolean;
  warnings: string[];
}

export interface GrpcStatusReport {
  configured: boolean;
  connected: boolean;
  network: string;
  host: string | null;
  port: number | null;
  useTls: boolean;
  bestBlock: string | null;
  lastFinalizedBlock: string | null;
  blockHeight: string | null;
  genesisIndex: number | null;
  protocolVersion: string | null;
  skipped?: boolean;
  error?: string;
}

export interface WasmArtifactReport {
  contractName: string;
  wasmPath: string;
  exists: boolean;
  sizeBytes: number;
  fileSha256: string;
  moduleRef: string;
  moduleVersion: number;
  hasEmbeddedSchema: boolean;
  moduleAlreadyOnChain: boolean | null;
  serializationVerified: boolean;
}

export interface ContractOnboardingReport {
  contractName: string;
  role: 'evidence_registry' | 'reward_pool' | 'cis8004' | 'cis8';
  simulated: boolean;
  index: string;
  subindex: string;
  contractAddress: string;
  onChainVerified: boolean;
  onChainName?: string;
  methods?: string[];
  moduleRef?: string;
  moduleAlreadyOnChain?: boolean;
  deployTxHash?: string;
  initTxHash?: string;
  ccdscanUrl: string;
  error?: string;
}

export interface OnboardingSummary {
  ok: boolean;
  mode: 'simulate' | 'live';
  timestamp: string;
  network: string;
  policyHash: string;
  envValidation: EnvValidationReport;
  grpc: GrpcStatusReport;
  wasmArtifacts: {
    evidence_registry: WasmArtifactReport;
    reward_pool: WasmArtifactReport;
  };
  contracts: {
    evidence_registry: ContractOnboardingReport;
    reward_pool: ContractOnboardingReport;
    cis8004: ContractOnboardingReport;
    cis8: ContractOnboardingReport;
  };
  identity: {
    usedSimulationPlaceholders: boolean;
    agentId: string | number;
    agentUri: string;
    agentCardValid: boolean;
    agentCardHash: string;
    agentCard: AgentCard;
    cis8004: {
      mode: 'simulate' | 'submit';
      contract: string;
      receiveName: string;
      transactionHash?: string;
      dryRunInvoked: boolean;
      dryRunError?: string;
    };
    cis8: {
      mode: 'simulate' | 'submit';
      contract: string;
      receiveName: string;
      concordiumAccount: string;
      externalChain: string;
      keyType: string;
      externalPublicKey: string;
      domainTag: string;
      digestSha256: string;
      signatureVerified?: boolean;
      transactionHash?: string;
    };
  };
  ccdscanLinks: {
    explorerBase: string;
    contracts: Record<string, string>;
    transactions: Record<string, string>;
    account?: string;
  };
  suggestedEnvBlock: string;
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

function hexToByteArray32(hexStr: string): number[] {
  const clean = hexStr.startsWith('0x') ? hexStr.slice(2) : hexStr;
  if (!/^[0-9a-fA-F]{64}$/.test(clean)) {
    throw new Error(`Expected 32-byte hex string, got: ${hexStr}`);
  }
  return Array.from(Buffer.from(clean, 'hex'));
}

function hasPrivateKeyAvailable(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env.CONCORDIUM_AGENT_PRIVATE_KEY?.trim()) {
    return true;
  }
  const keyPath = env.CONCORDIUM_AGENT_PRIVATE_KEY_PATH?.trim();
  if (!keyPath) {
    return false;
  }
  return existsSync(resolveProjectPath(keyPath));
}

function isValidConcordiumAddress(address: string): boolean {
  try {
    AccountAddress.fromBase58(address);
    return true;
  } catch {
    return false;
  }
}

function resolvePolicyHashFromEnv(env: NodeJS.ProcessEnv = process.env): string {
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
      required_zones: zonesRaw
        .split(',')
        .map((z) => z.trim())
        .filter(Boolean),
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

function getExplorerBaseUrl(env: NodeJS.ProcessEnv = process.env): string {
  const configured = env.CONCORDIUM_CCDSCAN_BASE_URL?.trim();
  const base = configured || 'https://testnet.ccdscan.io';
  return base.replace(/\/+$/, '');
}

function buildContractExplorerUrl(
  explorerBase: string,
  index: string,
  subindex: string
): string {
  return `${explorerBase}/?dcount=1&dentity=contract&dindex=${encodeURIComponent(index)}&dsubindex=${encodeURIComponent(subindex)}`;
}

function buildTxExplorerUrl(explorerBase: string, txHash: string): string {
  return `${explorerBase}/?dcount=1&dentity=transaction&dhash=${encodeURIComponent(txHash)}`;
}

function buildAccountExplorerUrl(explorerBase: string, accountAddress: string): string {
  return `${explorerBase}/?dcount=1&dentity=account&daddress=${encodeURIComponent(accountAddress)}`;
}

function validateOnboardingEnv(
  mode: 'simulate' | 'live',
  env: NodeJS.ProcessEnv = process.env
): EnvValidationReport {
  const coreLiveVars = [
    'CONCORDIUM_GRPC_HOST',
    'CONCORDIUM_GRPC_PORT',
    'CONCORDIUM_NETWORK',
  ];
  const identityVars = [
    'CONCORDIUM_CIS8004_INDEX',
    'CONCORDIUM_CIS8_INDEX',
    'VERA_AGENT_ID',
    'VERA_AGENT_NAME',
    'VERA_AGENT_DESCRIPTION',
    'VERA_AGENT_VERSION',
    'VERA_PROVIDER_ORG',
    'VERA_PROVIDER_URL',
    'VERA_AGENT_ENDPOINT_URL',
  ];
  const optionalVars = [
    'CONCORDIUM_AGENT_ACCOUNT_ADDRESS',
    'CONCORDIUM_EVIDENCE_REGISTRY_INDEX',
    'CONCORDIUM_REWARD_POOL_INDEX',
    'CONCORDIUM_EVIDENCE_REGISTRY_WASM_PATH',
    'CONCORDIUM_REWARD_POOL_WASM_PATH',
    'VERA_AGENT_CARD_URI',
    'VERA_POLICY_HASH',
    'VERA_EXTERNAL_CHAIN',
    'VERA_EXTERNAL_KEY_TYPE',
  ];

  const allChecked = [...coreLiveVars, ...identityVars, ...optionalVars];
  const configuredVars = allChecked.filter((name) => Boolean(env[name]?.trim()));
  const hasPrivateKey = hasPrivateKeyAvailable(env);

  const missingRequiredForLive: string[] = [];
  const missingOptionalOrSimulated: string[] = [];
  const warnings: string[] = [];

  for (const name of coreLiveVars) {
    if (!env[name]?.trim()) {
      missingRequiredForLive.push(name);
    }
  }

  const portRaw = env.CONCORDIUM_GRPC_PORT?.trim();
  if (portRaw) {
    const port = Number(portRaw);
    if (!Number.isInteger(port) || port <= 0) {
      throw new Error(`Invalid CONCORDIUM_GRPC_PORT value: ${portRaw}`);
    }
  }

  if (!hasPrivateKey) {
    missingRequiredForLive.push(
      'CONCORDIUM_AGENT_PRIVATE_KEY (or CONCORDIUM_AGENT_PRIVATE_KEY_PATH)'
    );
  }

  const accountAddr = env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim();
  if (accountAddr && !isValidConcordiumAddress(accountAddr)) {
    throw new Error(
      `Invalid CONCORDIUM_AGENT_ACCOUNT_ADDRESS format: ${accountAddr}`
    );
  }

  for (const name of [...identityVars, ...optionalVars]) {
    if (!env[name]?.trim()) {
      missingOptionalOrSimulated.push(name);
    }
  }

  if (!env.CONCORDIUM_CIS8004_INDEX?.trim()) {
    missingRequiredForLive.push('CONCORDIUM_CIS8004_INDEX');
  }
  if (!env.CONCORDIUM_CIS8_INDEX?.trim()) {
    missingRequiredForLive.push('CONCORDIUM_CIS8_INDEX');
  }

  if (mode === 'live') {
    if (
      !env.VERA_EXTERNAL_PUBLIC_KEY?.trim() ||
      !env.VERA_EXTERNAL_SIGNATURE_HEX?.trim()
    ) {
      missingRequiredForLive.push(
        'VERA_EXTERNAL_PUBLIC_KEY & VERA_EXTERNAL_SIGNATURE_HEX'
      );
    }
    if (missingRequiredForLive.length > 0) {
      throw new Error(
        `Missing required environment variables for live onboarding: ${missingRequiredForLive.join(', ')}`
      );
    }
  } else if (missingRequiredForLive.length > 0) {
    warnings.push(
      `Simulate mode active: skipping live-only requirements (${missingRequiredForLive.join(', ')})`
    );
  }

  return {
    mode,
    configuredVars,
    missingRequiredForLive,
    missingOptionalOrSimulated,
    hasPrivateKey,
    warnings,
  };
}

async function testGrpcConnection(
  mode: 'simulate' | 'live',
  env: NodeJS.ProcessEnv = process.env
): Promise<{ report: GrpcStatusReport; client: ConcordiumGRPCNodeClient | null }> {
  const host = env.CONCORDIUM_GRPC_HOST?.trim() || '';
  const portRaw = env.CONCORDIUM_GRPC_PORT?.trim() || '';
  const port = portRaw ? Number(portRaw) : 0;
  const network = env.CONCORDIUM_NETWORK?.trim() || 'testnet';
  const configured = Boolean(host && Number.isInteger(port) && port > 0);

  const tlsEnv = env.CONCORDIUM_GRPC_TLS?.trim().toLowerCase();
  const useTls =
    tlsEnv !== undefined && tlsEnv !== ''
      ? tlsEnv === 'true' || tlsEnv === '1'
      : port === 20000 || network.toLowerCase() !== 'local';

  const timeoutRaw = env.CONCORDIUM_GRPC_TIMEOUT_MS?.trim();
  const timeoutMs =
    timeoutRaw && Number.isInteger(Number(timeoutRaw)) && Number(timeoutRaw) > 0
      ? Number(timeoutRaw)
      : 15000;

  if (!configured) {
    if (mode === 'live') {
      throw new Error('CONCORDIUM_GRPC_HOST and CONCORDIUM_GRPC_PORT must be configured');
    }
    return {
      client: null,
      report: {
        configured: false,
        connected: false,
        network,
        host: host || null,
        port: port > 0 ? port : null,
        useTls,
        bestBlock: null,
        lastFinalizedBlock: null,
        blockHeight: null,
        genesisIndex: null,
        protocolVersion: null,
        skipped: true,
        error: 'gRPC host/port not set in environment; skipped live node check in --simulate mode',
      },
    };
  }

  const client = createConcordiumClient({ host, port, useTls, timeoutMs });
  try {
    const { consensusStatus, blockInfo } = await getNodeStatus(client);
    return {
      client,
      report: {
        configured: true,
        connected: true,
        network,
        host,
        port,
        useTls,
        bestBlock: consensusStatus.bestBlock?.toString() ?? null,
        lastFinalizedBlock: consensusStatus.lastFinalizedBlock?.toString() ?? null,
        blockHeight: blockInfo.blockHeight?.toString() ?? null,
        genesisIndex: consensusStatus.genesisIndex ?? null,
        protocolVersion:
          consensusStatus.protocolVersion !== undefined
            ? String(consensusStatus.protocolVersion)
            : null,
      },
    };
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    if (mode === 'live') {
      throw new Error(`Failed gRPC connection to ${host}:${port} (${message})`);
    }
    return {
      client: null,
      report: {
        configured: true,
        connected: false,
        network,
        host,
        port,
        useTls,
        bestBlock: null,
        lastFinalizedBlock: null,
        blockHeight: null,
        genesisIndex: null,
        protocolVersion: null,
        error: message,
      },
    };
  }
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

async function inspectAndVerifyWasm(params: {
  contractName: 'evidence_registry' | 'reward_pool';
  wasmEnvVar: string;
  defaultRelativePath: string;
  policyHash: string;
  client: ConcordiumGRPCNodeClient | null;
}): Promise<{
  report: WasmArtifactReport;
  versionedSource: ReturnType<typeof versionedModuleSourceFromBuffer>;
  moduleRef: ModuleReference.Type;
}> {
  const wasmPath = resolveProjectPath(
    process.env[params.wasmEnvVar]?.trim() || params.defaultRelativePath
  );
  if (!existsSync(wasmPath)) {
    throw new Error(`Contract WASM artifact not found: ${wasmPath}`);
  }

  const buf = readFileSync(wasmPath);
  if (buf.byteLength === 0) {
    throw new Error(`Contract WASM artifact is empty: ${wasmPath}`);
  }

  const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  const versionedSource = versionedModuleSourceFromBuffer(ab);
  const moduleRef = calculateModuleReference(versionedSource);
  const schema = await getEmbeddedModuleSchema(versionedSource);

  let serializationVerified = false;
  if (schema) {
    const schemaVersion = schema.type === 'unversioned' ? schema.version : undefined;
    if (params.contractName === 'evidence_registry') {
      const dummyBytes32 = hexToByteArray32(sha256Hex('vera-onboard-check'));
      serializeUpdateContractParameters(
        ContractName.fromString('evidence_registry'),
        EntrypointName.fromString('register_evidence'),
        {
          evidence_id: dummyBytes32,
          evidence_root: dummyBytes32,
          source_type: { Physical: [] },
          calibration_expires_at: '2099-01-01T00:00:00Z',
          metadata_uri: 'vera://onboard/check',
        },
        schema.buffer,
        schemaVersion
      );
      serializationVerified = true;
    } else {
      serializeInitContractParameters(
        ContractName.fromString('reward_pool'),
        {
          policy_hash: hexToByteArray32(params.policyHash),
          evidence_registry: {
            Some: [{ index: 0, subindex: 0 }],
          },
        },
        schema.buffer,
        schemaVersion
      );
      serializationVerified = true;
    }
  }

  let moduleAlreadyOnChain: boolean | null = null;
  if (params.client) {
    try {
      moduleAlreadyOnChain = await isModuleDeployed(params.client, moduleRef);
    } catch {
      moduleAlreadyOnChain = null;
    }
  }

  return {
    versionedSource,
    moduleRef,
    report: {
      contractName: params.contractName,
      wasmPath,
      exists: true,
      sizeBytes: buf.byteLength,
      fileSha256: sha256Hex(buf),
      moduleRef: moduleRef.moduleRef,
      moduleVersion: versionedSource.version,
      hasEmbeddedSchema: Boolean(schema),
      moduleAlreadyOnChain,
      serializationVerified,
    },
  };
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

async function verifyContractOnChain(
  client: ConcordiumGRPCNodeClient | null,
  address: ContractAddress.Type
): Promise<{ verified: boolean; name?: string; methods?: string[]; error?: string }> {
  if (!client) {
    return { verified: false };
  }
  try {
    const info = await getContractInstanceInfo(client, address);
    return {
      verified: true,
      name: info.name.value,
      methods: info.methods.map((m) => m.value),
    };
  } catch (err: unknown) {
    return {
      verified: false,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

async function onboardCoreContracts(params: {
  mode: 'simulate' | 'live';
  client: ConcordiumGRPCNodeClient | null;
  policyHash: string;
  explorerBase: string;
  evidenceWasm: Awaited<ReturnType<typeof inspectAndVerifyWasm>>;
  rewardWasm: Awaited<ReturnType<typeof inspectAndVerifyWasm>>;
}): Promise<{
  evidenceRegistry: ContractOnboardingReport;
  rewardPool: ContractOnboardingReport;
  deployerAddress?: string;
}> {
  const configuredEr = getContractAddressFromEnv('CONCORDIUM_EVIDENCE_REGISTRY');
  const configuredRp = getContractAddressFromEnv('CONCORDIUM_REWARD_POOL');
  const forceRedeploy = parseBoolEnv(process.env.VERA_FORCE_REDEPLOY, false);

  if (params.mode === 'simulate') {
    const erIndex = configuredEr
      ? configuredEr.index.toString()
      : process.env.CONCORDIUM_SIM_EVIDENCE_REGISTRY_INDEX?.trim() || '1000';
    const erSub = configuredEr
      ? configuredEr.subindex.toString()
      : process.env.CONCORDIUM_SIM_EVIDENCE_REGISTRY_SUBINDEX?.trim() || '0';
    const rpIndex = configuredRp
      ? configuredRp.index.toString()
      : process.env.CONCORDIUM_SIM_REWARD_POOL_INDEX?.trim() || '1001';
    const rpSub = configuredRp
      ? configuredRp.subindex.toString()
      : process.env.CONCORDIUM_SIM_REWARD_POOL_SUBINDEX?.trim() || '0';

    const erCheck =
      configuredEr && params.client
        ? await verifyContractOnChain(params.client, configuredEr)
        : { verified: false };
    const rpCheck =
      configuredRp && params.client
        ? await verifyContractOnChain(params.client, configuredRp)
        : { verified: false };

    return {
      deployerAddress: process.env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim() || undefined,
      evidenceRegistry: {
        contractName: 'evidence_registry',
        role: 'evidence_registry',
        simulated: true,
        index: erIndex,
        subindex: erSub,
        contractAddress: `${erIndex}.${erSub}`,
        onChainVerified: erCheck.verified,
        onChainName: erCheck.name,
        methods: erCheck.methods,
        moduleRef: params.evidenceWasm.report.moduleRef,
        moduleAlreadyOnChain: params.evidenceWasm.report.moduleAlreadyOnChain ?? false,
        ccdscanUrl: buildContractExplorerUrl(params.explorerBase, erIndex, erSub),
        error: erCheck.error,
      },
      rewardPool: {
        contractName: 'reward_pool',
        role: 'reward_pool',
        simulated: true,
        index: rpIndex,
        subindex: rpSub,
        contractAddress: `${rpIndex}.${rpSub}`,
        onChainVerified: rpCheck.verified,
        onChainName: rpCheck.name,
        methods: rpCheck.methods,
        moduleRef: params.rewardWasm.report.moduleRef,
        moduleAlreadyOnChain: params.rewardWasm.report.moduleAlreadyOnChain ?? false,
        ccdscanUrl: buildContractExplorerUrl(params.explorerBase, rpIndex, rpSub),
        error: rpCheck.error,
      },
    };
  }

  if (!params.client) {
    throw new Error('Connected Concordium gRPC client is required in live mode');
  }

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

  let evidenceRegistryReport: ContractOnboardingReport;
  let erAddress: ContractAddress.Type;

  if (configuredEr && !forceRedeploy) {
    const check = await verifyContractOnChain(params.client, configuredEr);
    if (!check.verified) {
      throw new Error(
        `Configured evidence_registry (${configuredEr.index}.${configuredEr.subindex}) could not be verified on-chain: ${check.error ?? 'unknown error'}`
      );
    }
    erAddress = configuredEr;
    evidenceRegistryReport = {
      contractName: 'evidence_registry',
      role: 'evidence_registry',
      simulated: false,
      index: configuredEr.index.toString(),
      subindex: configuredEr.subindex.toString(),
      contractAddress: `${configuredEr.index}.${configuredEr.subindex}`,
      onChainVerified: true,
      onChainName: check.name,
      methods: check.methods,
      moduleRef: params.evidenceWasm.report.moduleRef,
      moduleAlreadyOnChain: params.evidenceWasm.report.moduleAlreadyOnChain ?? true,
      ccdscanUrl: buildContractExplorerUrl(
        params.explorerBase,
        configuredEr.index.toString(),
        configuredEr.subindex.toString()
      ),
    };
  } else {
    const erDeploy = await deployModuleIfNeeded(
      params.client,
      senderAddress,
      loadedSigner.signer,
      params.evidenceWasm.versionedSource,
      params.evidenceWasm.moduleRef,
      expiryMinutes
    );
    const erInit = await initContractInstance(
      params.client,
      senderAddress,
      loadedSigner.signer,
      {
        moduleRef: params.evidenceWasm.moduleRef,
        contractName: 'evidence_registry',
        parameter: Parameter.empty(),
        amountMicroCcd: 0n,
        maxEnergy,
        expiryMinutes,
      }
    );
    erAddress = erInit.address;
    const check = await verifyContractOnChain(params.client, erAddress);
    evidenceRegistryReport = {
      contractName: 'evidence_registry',
      role: 'evidence_registry',
      simulated: false,
      index: erAddress.index.toString(),
      subindex: erAddress.subindex.toString(),
      contractAddress: `${erAddress.index}.${erAddress.subindex}`,
      onChainVerified: check.verified,
      onChainName: check.name,
      methods: check.methods,
      moduleRef: params.evidenceWasm.report.moduleRef,
      moduleAlreadyOnChain: erDeploy.alreadyOnChain,
      deployTxHash: erDeploy.txHash,
      initTxHash: erInit.txHash,
      ccdscanUrl: buildContractExplorerUrl(
        params.explorerBase,
        erAddress.index.toString(),
        erAddress.subindex.toString()
      ),
    };
  }

  let rewardPoolReport: ContractOnboardingReport;
  if (configuredRp && !forceRedeploy) {
    const check = await verifyContractOnChain(params.client, configuredRp);
    if (!check.verified) {
      throw new Error(
        `Configured reward_pool (${configuredRp.index}.${configuredRp.subindex}) could not be verified on-chain: ${check.error ?? 'unknown error'}`
      );
    }
    rewardPoolReport = {
      contractName: 'reward_pool',
      role: 'reward_pool',
      simulated: false,
      index: configuredRp.index.toString(),
      subindex: configuredRp.subindex.toString(),
      contractAddress: `${configuredRp.index}.${configuredRp.subindex}`,
      onChainVerified: true,
      onChainName: check.name,
      methods: check.methods,
      moduleRef: params.rewardWasm.report.moduleRef,
      moduleAlreadyOnChain: params.rewardWasm.report.moduleAlreadyOnChain ?? true,
      ccdscanUrl: buildContractExplorerUrl(
        params.explorerBase,
        configuredRp.index.toString(),
        configuredRp.subindex.toString()
      ),
    };
  } else {
    const rpDeploy = await deployModuleIfNeeded(
      params.client,
      senderAddress,
      loadedSigner.signer,
      params.rewardWasm.versionedSource,
      params.rewardWasm.moduleRef,
      expiryMinutes
    );
    const rpSchema = await getEmbeddedModuleSchema(params.rewardWasm.versionedSource);
    if (!rpSchema) {
      throw new Error('Missing embedded schema in reward_pool.wasm.v1');
    }
    const schemaVersion = rpSchema.type === 'unversioned' ? rpSchema.version : undefined;
    const rpInitParam = serializeInitContractParameters(
      ContractName.fromString('reward_pool'),
      {
        policy_hash: hexToByteArray32(params.policyHash),
        evidence_registry: {
          Some: [
            {
              index: Number(erAddress.index),
              subindex: Number(erAddress.subindex),
            },
          ],
        },
      },
      rpSchema.buffer,
      schemaVersion
    );

    const rpInit = await initContractInstance(
      params.client,
      senderAddress,
      loadedSigner.signer,
      {
        moduleRef: params.rewardWasm.moduleRef,
        contractName: 'reward_pool',
        parameter: rpInitParam,
        amountMicroCcd: rewardPoolInitAmount,
        maxEnergy,
        expiryMinutes,
      }
    );
    const check = await verifyContractOnChain(params.client, rpInit.address);
    rewardPoolReport = {
      contractName: 'reward_pool',
      role: 'reward_pool',
      simulated: false,
      index: rpInit.address.index.toString(),
      subindex: rpInit.address.subindex.toString(),
      contractAddress: `${rpInit.address.index}.${rpInit.address.subindex}`,
      onChainVerified: check.verified,
      onChainName: check.name,
      methods: check.methods,
      moduleRef: params.rewardWasm.report.moduleRef,
      moduleAlreadyOnChain: rpDeploy.alreadyOnChain,
      deployTxHash: rpDeploy.txHash,
      initTxHash: rpInit.txHash,
      ccdscanUrl: buildContractExplorerUrl(
        params.explorerBase,
        rpInit.address.index.toString(),
        rpInit.address.subindex.toString()
      ),
    };
  }

  return {
    deployerAddress: senderAddress.address,
    evidenceRegistry: evidenceRegistryReport,
    rewardPool: rewardPoolReport,
  };
}

function buildAgentCardForOnboarding(
  mode: 'simulate' | 'live',
  network: string,
  env: NodeJS.ProcessEnv = process.env
): {
  card: AgentCard;
  cis8004Contract: ContractAddress.Type;
  cis8Contract: ContractAddress.Type;
  usedSimulationPlaceholders: boolean;
} {
  const cis8004Env = getContractAddressFromEnv('CONCORDIUM_CIS8004', env);
  const cis8Env = getContractAddressFromEnv('CONCORDIUM_CIS8', env);

  if (mode === 'live') {
    if (!cis8004Env) {
      throw new Error('CONCORDIUM_CIS8004_INDEX is required in live mode');
    }
    if (!cis8Env) {
      throw new Error('CONCORDIUM_CIS8_INDEX is required in live mode');
    }
    const card = generateAgentCard({ network }, env);
    return {
      card,
      cis8004Contract: cis8004Env,
      cis8Contract: cis8Env,
      usedSimulationPlaceholders: false,
    };
  }

  const simCis8004Index = env.CONCORDIUM_SIM_CIS8004_INDEX?.trim() || '1002';
  const simCis8004Sub = env.CONCORDIUM_SIM_CIS8004_SUBINDEX?.trim() || '0';
  const simCis8Index = env.CONCORDIUM_SIM_CIS8_INDEX?.trim() || '1003';
  const simCis8Sub = env.CONCORDIUM_SIM_CIS8_SUBINDEX?.trim() || '0';

  const cis8004Contract =
    cis8004Env ?? ContractAddress.create(BigInt(simCis8004Index), BigInt(simCis8004Sub));
  const cis8Contract =
    cis8Env ?? ContractAddress.create(BigInt(simCis8Index), BigInt(simCis8Sub));

  const port = env.PORT?.trim() || '8086';
  const defaultEndpointUrl = `http://localhost:${port}`;

  const usedSimulationPlaceholders =
    !cis8004Env ||
    !cis8Env ||
    !env.VERA_AGENT_NAME?.trim() ||
    !env.VERA_AGENT_DESCRIPTION?.trim() ||
    !env.VERA_AGENT_VERSION?.trim() ||
    !env.VERA_PROVIDER_ORG?.trim() ||
    !env.VERA_PROVIDER_URL?.trim() ||
    !env.VERA_AGENT_ENDPOINT_URL?.trim() ||
    !env.VERA_AGENT_ID?.trim();

  const endpointUrl = env.VERA_AGENT_ENDPOINT_URL?.trim() || defaultEndpointUrl;
  const providerUrl = env.VERA_PROVIDER_URL?.trim() || endpointUrl;
  const version = env.VERA_AGENT_VERSION?.trim() || '1.0.0';

  const card = generateAgentCard(
    {
      name:
        env.VERA_AGENT_NAME?.trim() ||
        env.VERA_AGENT_DISPLAY_NAME?.trim() ||
        'VERA Environmental Agent',
      description:
        env.VERA_AGENT_DESCRIPTION?.trim() ||
        'Verified Environmental Responsibility Agent for Concordium Trusted Agents',
      version,
      providerOrganization: env.VERA_PROVIDER_ORG?.trim() || 'VERA Protocol',
      providerUrl,
      endpoints: [
        {
          name: env.VERA_AGENT_ENDPOINT_NAME?.trim() || 'VERA-API',
          endpoint: endpointUrl,
          protocolBinding: env.VERA_AGENT_PROTOCOL_BINDING?.trim() || 'HTTP+JSON',
          version,
        },
      ],
      agentId: env.VERA_AGENT_ID?.trim() || 'vera-agent-demo-01',
      network,
      cis8004Index: cis8004Contract.index,
      cis8004Subindex: cis8004Contract.subindex,
      active: parseBoolEnv(env.VERA_AGENT_ACTIVE, true),
    },
    env
  );

  return {
    card,
    cis8004Contract,
    cis8Contract,
    usedSimulationPlaceholders,
  };
}

function formatSuggestedEnvBlock(summary: Omit<OnboardingSummary, 'suggestedEnvBlock'>): string {
  const lines = [
    `CONCORDIUM_NETWORK=${summary.network}`,
    `CONCORDIUM_EVIDENCE_REGISTRY_INDEX=${summary.contracts.evidence_registry.index}`,
    `CONCORDIUM_EVIDENCE_REGISTRY_SUBINDEX=${summary.contracts.evidence_registry.subindex}`,
    `CONCORDIUM_REWARD_POOL_INDEX=${summary.contracts.reward_pool.index}`,
    `CONCORDIUM_REWARD_POOL_SUBINDEX=${summary.contracts.reward_pool.subindex}`,
    `CONCORDIUM_CIS8004_INDEX=${summary.contracts.cis8004.index}`,
    `CONCORDIUM_CIS8004_SUBINDEX=${summary.contracts.cis8004.subindex}`,
    `CONCORDIUM_CIS8_INDEX=${summary.contracts.cis8.index}`,
    `CONCORDIUM_CIS8_SUBINDEX=${summary.contracts.cis8.subindex}`,
    `VERA_POLICY_HASH=${summary.policyHash}`,
    `VERA_AGENT_CARD_HASH=${summary.identity.agentCardHash}`,
  ];
  return lines.join('\n');
}

function formatTextReport(summary: OnboardingSummary): string {
  const lines: string[] = [];
  lines.push('============================================================');
  lines.push(`VERA Concordium Testnet Onboarding (${summary.mode.toUpperCase()})`);
  lines.push('============================================================');
  lines.push(`Network:         ${summary.network}`);
  lines.push(`Timestamp:       ${summary.timestamp}`);
  lines.push(`Policy Hash:     ${summary.policyHash}`);
  lines.push('');

  lines.push('1) Environment & gRPC Node Status');
  lines.push(
    `   - Configured env vars: ${summary.envValidation.configuredVars.length} | Missing (live): ${summary.envValidation.missingRequiredForLive.length} | Private key: ${summary.envValidation.hasPrivateKey ? 'present' : 'absent'}`
  );
  if (summary.envValidation.warnings.length > 0) {
    for (const warning of summary.envValidation.warnings) {
      lines.push(`   - Warning: ${warning}`);
    }
  }
  lines.push(
    `   - gRPC (${summary.grpc.host ?? 'unconfigured'}:${summary.grpc.port ?? '-'}): connected=${summary.grpc.connected}`
  );
  if (summary.grpc.connected) {
    lines.push(
      `     blockHeight=${summary.grpc.blockHeight ?? '-'} bestBlock=${summary.grpc.bestBlock ?? '-'} lastFinalized=${summary.grpc.lastFinalizedBlock ?? '-'}`
    );
  } else if (summary.grpc.error) {
    lines.push(`     note=${summary.grpc.error}`);
  }
  lines.push('');

  lines.push('2) WASM Artifacts');
  for (const artifact of [
    summary.wasmArtifacts.evidence_registry,
    summary.wasmArtifacts.reward_pool,
  ]) {
    lines.push(
      `   - ${artifact.contractName}: ${artifact.sizeBytes} bytes | moduleRef=${artifact.moduleRef}`
    );
    lines.push(
      `     schema=${artifact.hasEmbeddedSchema} | serializationVerified=${artifact.serializationVerified} | onChain=${artifact.moduleAlreadyOnChain ?? 'unknown'}`
    );
  }
  lines.push('');

  lines.push('3) Smart Contracts (Indices & Explorer Links)');
  for (const contract of [
    summary.contracts.evidence_registry,
    summary.contracts.reward_pool,
    summary.contracts.cis8004,
    summary.contracts.cis8,
  ]) {
    const statusTag = contract.simulated ? 'simulated' : 'live';
    lines.push(
      `   - ${contract.contractName} (${statusTag}): index=${contract.index}, subindex=${contract.subindex} | onChainVerified=${contract.onChainVerified}`
    );
    lines.push(`     CCDScan: ${contract.ccdscanUrl}`);
    if (contract.deployTxHash) {
      lines.push(
        `     Deploy Tx: ${buildTxExplorerUrl(summary.ccdscanLinks.explorerBase, contract.deployTxHash)}`
      );
    }
    if (contract.initTxHash) {
      lines.push(
        `     Init Tx:   ${buildTxExplorerUrl(summary.ccdscanLinks.explorerBase, contract.initTxHash)}`
      );
    }
  }
  lines.push('');

  lines.push('4) Agent Identity (CIS-8004 & CIS-8)');
  lines.push(`   - Agent ID:          ${summary.identity.agentId}`);
  lines.push(`   - Agent URI:         ${summary.identity.agentUri}`);
  lines.push(`   - agent-card.json:   valid=${summary.identity.agentCardValid}`);
  lines.push(`   - agent-card SHA256: ${summary.identity.agentCardHash}`);
  lines.push(
    `   - CIS-8004:          contract=${summary.identity.cis8004.contract} | mode=${summary.identity.cis8004.mode}${summary.identity.cis8004.transactionHash ? ` | tx=${summary.identity.cis8004.transactionHash}` : ''}`
  );
  lines.push(
    `   - CIS-8:             contract=${summary.identity.cis8.contract} | domainTag=${summary.identity.cis8.domainTag} | digest=${summary.identity.cis8.digestSha256} | verified=${summary.identity.cis8.signatureVerified ?? 'submitted'}`
  );
  lines.push('');

  lines.push('5) Suggested .env Block');
  lines.push('------------------------------------------------------------');
  lines.push(summary.suggestedEnvBlock);
  lines.push('------------------------------------------------------------');
  lines.push('');
  lines.push('JSON Summary:');
  lines.push(JSON.stringify(summary, null, 2));

  return lines.join('\n');
}

export async function runOnboardTestnet(argv = process.argv.slice(2)): Promise<{
  summary: OnboardingSummary;
  jsonOnly: boolean;
}> {
  const forceSimulate =
    argv.includes('--simulate') ||
    argv.includes('--dry-run') ||
    parseBoolEnv(process.env.VERA_DEPLOY_SIMULATE, false);
  const jsonOnly = argv.includes('--json');
  const mode: 'simulate' | 'live' = forceSimulate ? 'simulate' : 'live';

  const network = process.env.CONCORDIUM_NETWORK?.trim() || 'testnet';
  const explorerBase = getExplorerBaseUrl();

  // 1. Validate environment variables & test gRPC connection (getNodeStatus)
  const envValidation = validateOnboardingEnv(mode);
  const { report: grpc, client } = await testGrpcConnection(mode);

  // 2. Verify WASM artifacts (evidence_registry.wasm.v1 & reward_pool.wasm.v1)
  const policyHash = resolvePolicyHashFromEnv();
  const evidenceWasm = await inspectAndVerifyWasm({
    contractName: 'evidence_registry',
    wasmEnvVar: 'CONCORDIUM_EVIDENCE_REGISTRY_WASM_PATH',
    defaultRelativePath: '../contracts/target/evidence_registry.wasm.v1',
    policyHash,
    client,
  });
  const rewardWasm = await inspectAndVerifyWasm({
    contractName: 'reward_pool',
    wasmEnvVar: 'CONCORDIUM_REWARD_POOL_WASM_PATH',
    defaultRelativePath: '../contracts/target/reward_pool.wasm.v1',
    policyHash,
    client,
  });

  // 3. Simulate or deploy/verify evidence_registry & reward_pool
  const coreContracts = await onboardCoreContracts({
    mode,
    client,
    policyHash,
    explorerBase,
    evidenceWasm,
    rewardWasm,
  });

  // 4. Simulate or register agent-card.json in CIS-8004 and link external key in CIS-8
  const { card, cis8004Contract, cis8Contract, usedSimulationPlaceholders } =
    buildAgentCardForOnboarding(mode, network);
  const cardValidation = validateAgentCard(card);
  if (!cardValidation.valid) {
    throw new Error(`Invalid AgentCard: ${cardValidation.errors.join('; ')}`);
  }
  const agentCardHash = calculateAgentCardHash(card);
  const agentUri =
    process.env.VERA_AGENT_CARD_URI?.trim() ||
    `${card.endpoints[0].endpoint.replace(/\/+$/, '')}/agent-card.json`;

  const cis8004ConfiguredInEnv = Boolean(getContractAddressFromEnv('CONCORDIUM_CIS8004'));
  const cis8ConfiguredInEnv = Boolean(getContractAddressFromEnv('CONCORDIUM_CIS8'));

  const cis8004OnChainCheck =
    client && cis8004ConfiguredInEnv
      ? await verifyContractOnChain(client, cis8004Contract)
      : { verified: false };
  const cis8OnChainCheck =
    client && cis8ConfiguredInEnv
      ? await verifyContractOnChain(client, cis8Contract)
      : { verified: false };

  let cis8004Result;
  let dryRunInvoked = false;
  let dryRunError: string | undefined;

  if (mode === 'live') {
    if (!client) {
      throw new Error('Connected Concordium gRPC client is required for CIS-8004 live registration');
    }
    cis8004Result = await registerAgentInCIS8004({
      agentCard: card,
      agentUri,
      mode: 'submit',
      client,
      contractAddress: cis8004Contract,
      senderAddress: coreContracts.deployerAddress,
    });
  } else {
    try {
      cis8004Result = await registerAgentInCIS8004({
        agentCard: card,
        agentUri,
        mode: 'simulate',
        client: cis8004OnChainCheck.verified && client ? client : undefined,
        contractAddress: cis8004Contract,
        senderAddress: coreContracts.deployerAddress,
      });
      dryRunInvoked = Boolean(cis8004OnChainCheck.verified && client);
    } catch (err: unknown) {
      dryRunError = err instanceof Error ? err.message : String(err);
      cis8004Result = await registerAgentInCIS8004({
        agentCard: card,
        agentUri,
        mode: 'simulate',
        contractAddress: cis8004Contract,
        senderAddress: coreContracts.deployerAddress,
      });
    }
  }

  const accountForCis8 =
    coreContracts.deployerAddress ??
    (process.env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim() ||
      AccountAddress.fromBuffer(new Uint8Array(32).buffer).address);

  const externalChain = process.env.VERA_EXTERNAL_CHAIN?.trim() || 'ethereum';
  const externalKeyType = process.env.VERA_EXTERNAL_KEY_TYPE?.trim() || 'ed25519';

  const cis8Result = await linkExternalKeyCIS8({
    concordiumAccount: accountForCis8,
    externalChain,
    keyType: externalKeyType,
    externalPublicKey: process.env.VERA_EXTERNAL_PUBLIC_KEY?.trim() || undefined,
    externalSignatureHex: process.env.VERA_EXTERNAL_SIGNATURE_HEX?.trim() || undefined,
    agentId: card.registrations[0].agentId,
    network,
    cis8Contract,
    mode: mode === 'live' ? 'submit' : 'simulate',
    client: mode === 'live' && client ? client : undefined,
  });

  const cis8004Report: ContractOnboardingReport = {
    contractName: 'cis8004_registry',
    role: 'cis8004',
    simulated: mode === 'simulate',
    index: cis8004Contract.index.toString(),
    subindex: cis8004Contract.subindex.toString(),
    contractAddress: `${cis8004Contract.index}.${cis8004Contract.subindex}`,
    onChainVerified: cis8004OnChainCheck.verified,
    onChainName: cis8004OnChainCheck.name,
    methods: cis8004OnChainCheck.methods,
    ccdscanUrl: buildContractExplorerUrl(
      explorerBase,
      cis8004Contract.index.toString(),
      cis8004Contract.subindex.toString()
    ),
    error: cis8004OnChainCheck.error,
  };

  const cis8Report: ContractOnboardingReport = {
    contractName: 'cis8_registry',
    role: 'cis8',
    simulated: mode === 'simulate',
    index: cis8Contract.index.toString(),
    subindex: cis8Contract.subindex.toString(),
    contractAddress: `${cis8Contract.index}.${cis8Contract.subindex}`,
    onChainVerified: cis8OnChainCheck.verified,
    onChainName: cis8OnChainCheck.name,
    methods: cis8OnChainCheck.methods,
    ccdscanUrl: buildContractExplorerUrl(
      explorerBase,
      cis8Contract.index.toString(),
      cis8Contract.subindex.toString()
    ),
    error: cis8OnChainCheck.error,
  };

  const txLinks: Record<string, string> = {};
  if (coreContracts.evidenceRegistry.deployTxHash) {
    txLinks.evidence_registry_deploy = buildTxExplorerUrl(
      explorerBase,
      coreContracts.evidenceRegistry.deployTxHash
    );
  }
  if (coreContracts.evidenceRegistry.initTxHash) {
    txLinks.evidence_registry_init = buildTxExplorerUrl(
      explorerBase,
      coreContracts.evidenceRegistry.initTxHash
    );
  }
  if (coreContracts.rewardPool.deployTxHash) {
    txLinks.reward_pool_deploy = buildTxExplorerUrl(
      explorerBase,
      coreContracts.rewardPool.deployTxHash
    );
  }
  if (coreContracts.rewardPool.initTxHash) {
    txLinks.reward_pool_init = buildTxExplorerUrl(
      explorerBase,
      coreContracts.rewardPool.initTxHash
    );
  }
  if (cis8004Result.transactionHash) {
    txLinks.cis8004_register = buildTxExplorerUrl(explorerBase, cis8004Result.transactionHash);
  }
  if (cis8Result.transactionHash) {
    txLinks.cis8_link = buildTxExplorerUrl(explorerBase, cis8Result.transactionHash);
  }

  const baseSummary: Omit<OnboardingSummary, 'suggestedEnvBlock'> = {
    ok: true,
    mode,
    timestamp: new Date().toISOString(),
    network,
    policyHash,
    envValidation,
    grpc,
    wasmArtifacts: {
      evidence_registry: evidenceWasm.report,
      reward_pool: rewardWasm.report,
    },
    contracts: {
      evidence_registry: coreContracts.evidenceRegistry,
      reward_pool: coreContracts.rewardPool,
      cis8004: cis8004Report,
      cis8: cis8Report,
    },
    identity: {
      usedSimulationPlaceholders,
      agentId: card.registrations[0].agentId,
      agentUri,
      agentCardValid: cardValidation.valid,
      agentCardHash,
      agentCard: card,
      cis8004: {
        mode: cis8004Result.mode,
        contract: cis8004Result.contract,
        receiveName: cis8004Result.receiveName,
        transactionHash: cis8004Result.transactionHash,
        dryRunInvoked,
        dryRunError,
      },
      cis8: {
        mode: cis8Result.mode,
        contract: cis8Result.cis8Contract,
        receiveName: cis8Result.receiveName,
        concordiumAccount: accountForCis8,
        externalChain,
        keyType: cis8Result.metadataEntry.keyType,
        externalPublicKey: cis8Result.externalPublicKey,
        domainTag: cis8Result.canonicalMessage.domainTag,
        digestSha256: cis8Result.canonicalMessage.digestSha256,
        signatureVerified: cis8Result.signatureVerified,
        transactionHash: cis8Result.transactionHash,
      },
    },
    ccdscanLinks: {
      explorerBase,
      contracts: {
        evidence_registry: coreContracts.evidenceRegistry.ccdscanUrl,
        reward_pool: coreContracts.rewardPool.ccdscanUrl,
        cis8004: cis8004Report.ccdscanUrl,
        cis8: cis8Report.ccdscanUrl,
      },
      transactions: txLinks,
      account: coreContracts.deployerAddress
        ? buildAccountExplorerUrl(explorerBase, coreContracts.deployerAddress)
        : undefined,
    },
  };

  const summary: OnboardingSummary = {
    ...baseSummary,
    suggestedEnvBlock: formatSuggestedEnvBlock(baseSummary),
  };

  return { summary, jsonOnly };
}

const isEntrypoint = (() => {
  if (!process.argv[1]) {
    return false;
  }
  try {
    return realpathSync(resolve(process.argv[1])) === realpathSync(__filename);
  } catch {
    return resolve(process.argv[1]) === __filename;
  }
})();

if (isEntrypoint) {
  runOnboardTestnet()
    .then(({ summary, jsonOnly }) => {
      if (jsonOnly) {
        process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
      } else {
        process.stdout.write(`${formatTextReport(summary)}\n`);
      }
    })
    .catch((err: unknown) => {
      const message = err instanceof Error ? err.message : String(err);
      process.stderr.write(`${JSON.stringify({ ok: false, error: message }, null, 2)}\n`);
      process.exit(1);
    });
}
