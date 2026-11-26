import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import dotenv from 'dotenv';
import {
  AccountAddress,
  AccountTransaction,
  AccountTransactionHeader,
  AccountTransactionType,
  BlockHash,
  BlockItemSummaryInBlock,
  CcdAmount,
  ContractAddress,
  ContractContext,
  Energy,
  InstanceInfo,
  InvokeContractResult,
  Parameter,
  ReceiveName,
  SequenceNumber,
  TransactionExpiry,
  TransactionHash,
  UpdateContractPayload,
  buildAccountSigner,
  buildBasicAccountSigner,
  parseWallet,
  signTransaction,
  type AccountSigner,
} from '@concordium/web-sdk';
import { ConcordiumGRPCNodeClient, credentials } from '@concordium/web-sdk/nodejs';

// Load root .env if present (does not override already defined environment variables)
dotenv.config({ path: resolve(process.cwd(), '.env') });
dotenv.config({ path: resolve(process.cwd(), '..', '.env') });

export interface ConcordiumBridgeConfig {
  grpcHost: string;
  grpcPort: number;
  useTls: boolean;
  timeoutMs: number;
  network: string;
  agentAccountAddress?: string;
  agentPrivateKeyPath?: string;
  agentPrivateKeyHex?: string;
  contracts: {
    evidenceRegistry?: ContractAddress.Type;
    rewardPool?: ContractAddress.Type;
    cis8004?: ContractAddress.Type;
    cis8?: ContractAddress.Type;
  };
}

export interface ConcordiumClientOptions {
  host?: string;
  port?: number;
  useTls?: boolean;
  timeoutMs?: number;
}

/**
 * Parses a required non-empty environment variable.
 */
export function requireEnv(name: string, env: NodeJS.ProcessEnv = process.env): string {
  const value = env[name]?.trim();
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

/**
 * Parses an integer environment variable with validation.
 */
function parseIntegerEnv(name: string, raw: string | undefined, fallback?: number): number {
  const value = raw?.trim();
  if (!value) {
    if (fallback !== undefined) {
      return fallback;
    }
    throw new Error(`Missing required integer environment variable: ${name}`);
  }
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`Invalid integer value for ${name}: ${value}`);
  }
  return parsed;
}

/**
 * Parses a ContractAddress from index and subindex values.
 */
export function parseContractAddress(
  index: string | number | bigint,
  subindex: string | number | bigint = 0
): ContractAddress.Type {
  const idx = typeof index === 'bigint' ? index : BigInt(index);
  const sub = typeof subindex === 'bigint' ? subindex : BigInt(subindex);
  return ContractAddress.create(idx, sub);
}

/**
 * Reads a ContractAddress from `<PREFIX>_INDEX` and `<PREFIX>_SUBINDEX` environment variables.
 */
export function getContractAddressFromEnv(
  prefix: string,
  env: NodeJS.ProcessEnv = process.env
): ContractAddress.Type | undefined {
  const indexRaw = env[`${prefix}_INDEX`]?.trim();
  if (!indexRaw) {
    return undefined;
  }
  const subindexRaw = env[`${prefix}_SUBINDEX`]?.trim() ?? '0';
  return parseContractAddress(indexRaw, subindexRaw);
}

/**
 * Loads Concordium bridge configuration exclusively from environment variables.
 */
export function loadConcordiumConfig(
  env: NodeJS.ProcessEnv = process.env
): ConcordiumBridgeConfig {
  const grpcHost = requireEnv('CONCORDIUM_GRPC_HOST', env);
  const grpcPort = parseIntegerEnv('CONCORDIUM_GRPC_PORT', env.CONCORDIUM_GRPC_PORT);
  const network = requireEnv('CONCORDIUM_NETWORK', env);

  const tlsEnv = env.CONCORDIUM_GRPC_TLS?.trim().toLowerCase();
  const useTls =
    tlsEnv !== undefined && tlsEnv !== ''
      ? tlsEnv === 'true' || tlsEnv === '1'
      : grpcPort === 20000 || network.toLowerCase() !== 'local';

  const timeoutMs = parseIntegerEnv(
    'CONCORDIUM_GRPC_TIMEOUT_MS',
    env.CONCORDIUM_GRPC_TIMEOUT_MS,
    15000
  );

  return {
    grpcHost,
    grpcPort,
    useTls,
    timeoutMs,
    network,
    agentAccountAddress: env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim() || undefined,
    agentPrivateKeyPath: env.CONCORDIUM_AGENT_PRIVATE_KEY_PATH?.trim() || undefined,
    agentPrivateKeyHex: env.CONCORDIUM_AGENT_PRIVATE_KEY?.trim() || undefined,
    contracts: {
      evidenceRegistry: getContractAddressFromEnv('CONCORDIUM_EVIDENCE_REGISTRY', env),
      rewardPool: getContractAddressFromEnv('CONCORDIUM_REWARD_POOL', env),
      cis8004: getContractAddressFromEnv('CONCORDIUM_CIS8004', env),
      cis8: getContractAddressFromEnv('CONCORDIUM_CIS8', env),
    },
  };
}

/**
 * Initializes a ConcordiumGRPCNodeClient pointing to CONCORDIUM_GRPC_HOST and CONCORDIUM_GRPC_PORT.
 */
export function createConcordiumClient(
  options: ConcordiumClientOptions = {}
): ConcordiumGRPCNodeClient {
  const host = options.host ?? requireEnv('CONCORDIUM_GRPC_HOST');
  const port =
    options.port ??
    parseIntegerEnv('CONCORDIUM_GRPC_PORT', process.env.CONCORDIUM_GRPC_PORT);

  const tlsEnv = process.env.CONCORDIUM_GRPC_TLS?.trim().toLowerCase();
  const useTls =
    options.useTls ??
    (tlsEnv !== undefined && tlsEnv !== ''
      ? tlsEnv === 'true' || tlsEnv === '1'
      : port === 20000);

  const timeout =
    options.timeoutMs ??
    parseIntegerEnv('CONCORDIUM_GRPC_TIMEOUT_MS', process.env.CONCORDIUM_GRPC_TIMEOUT_MS, 15000);

  const channelCredentials = useTls
    ? credentials.createSsl()
    : credentials.createInsecure();

  return new ConcordiumGRPCNodeClient(host, port, channelCredentials, { timeout });
}

/**
 * Queries node consensus status and latest finalized block info.
 */
export async function getNodeStatus(client: ConcordiumGRPCNodeClient) {
  const [consensusStatus, blockInfo] = await Promise.all([
    client.getConsensusStatus(),
    client.getBlockInfo(),
  ]);
  return { consensusStatus, blockInfo };
}

/**
 * Queries metadata and exposed entrypoints of a deployed smart contract instance.
 */
export async function getContractInstanceInfo(
  client: ConcordiumGRPCNodeClient,
  contractAddress: ContractAddress.Type,
  blockHash?: BlockHash.Type
): Promise<InstanceInfo> {
  return client.getInstanceInfo(contractAddress, blockHash);
}

export interface InvokeContractViewParams {
  contract: ContractAddress.Type;
  receiveName: string;
  parameterHex?: string;
  parameterBytes?: Uint8Array;
  invoker?: AccountAddress.Type;
  amountMicroCcd?: bigint;
  energy?: bigint;
  blockHash?: BlockHash.Type;
}

/**
 * Converts a Uint8Array/Buffer or hex string into a Concordium Parameter.Type.
 */
export function buildContractParameter(
  parameterHex?: string,
  parameterBytes?: Uint8Array
): Parameter.Type {
  if (parameterBytes !== undefined) {
    const copy = new Uint8Array(parameterBytes.byteLength);
    copy.set(parameterBytes);
    return Parameter.fromBuffer(copy.buffer);
  }
  if (parameterHex !== undefined && parameterHex.length > 0) {
    const cleanHex = parameterHex.startsWith('0x') ? parameterHex.slice(2) : parameterHex;
    return Parameter.fromHexString(cleanHex);
  }
  return Parameter.empty();
}

/**
 * Performs a read-only / dry-run smart contract invocation against the node state.
 */
export async function invokeContractView(
  client: ConcordiumGRPCNodeClient,
  params: InvokeContractViewParams
): Promise<InvokeContractResult> {
  const context: ContractContext = {
    contract: params.contract,
    method: ReceiveName.fromString(params.receiveName),
    parameter: buildContractParameter(params.parameterHex, params.parameterBytes),
    amount: CcdAmount.fromMicroCcd(params.amountMicroCcd ?? 0n),
    invoker: params.invoker,
    energy: params.energy !== undefined ? Energy.create(params.energy) : undefined,
  };

  return client.invokeContract(context, params.blockHash);
}

export interface LoadedSigner {
  signer: AccountSigner;
  address?: AccountAddress.Type;
}

/**
 * Loads an AccountSigner from CONCORDIUM_AGENT_PRIVATE_KEY_PATH (wallet export JSON)
 * or CONCORDIUM_AGENT_PRIVATE_KEY (hex Ed25519 key).
 */
export function loadAccountSignerFromEnv(
  env: NodeJS.ProcessEnv = process.env
): LoadedSigner {
  const walletPath = env.CONCORDIUM_AGENT_PRIVATE_KEY_PATH?.trim();
  const addressEnv = env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim();

  if (walletPath) {
    const resolvedPath = resolve(process.cwd(), walletPath);
    const rawWallet = readFileSync(resolvedPath, 'utf8');
    const parsed = parseWallet(rawWallet);
    return {
      signer: buildAccountSigner(parsed),
      address: AccountAddress.fromBase58(addressEnv || parsed.value.address),
    };
  }

  const privateKeyHex = env.CONCORDIUM_AGENT_PRIVATE_KEY?.trim();
  if (privateKeyHex) {
    return {
      signer: buildBasicAccountSigner(privateKeyHex),
      address: addressEnv ? AccountAddress.fromBase58(addressEnv) : undefined,
    };
  }

  throw new Error(
    'Missing agent credentials in environment: set CONCORDIUM_AGENT_PRIVATE_KEY_PATH or CONCORDIUM_AGENT_PRIVATE_KEY'
  );
}

export interface BuildUpdateContractTxParams {
  sender: AccountAddress.Type;
  nonce: SequenceNumber.Type | bigint | number;
  contract: ContractAddress.Type;
  receiveName: string;
  parameterHex?: string;
  parameterBytes?: Uint8Array;
  amountMicroCcd?: bigint;
  maxContractExecutionEnergy?: bigint;
  expiryMinutes?: number;
}

/**
 * Builds an unsigned Concordium AccountTransaction for updating a smart contract.
 */
export function buildUpdateContractTransaction(
  params: BuildUpdateContractTxParams
): AccountTransaction {
  const header: AccountTransactionHeader = {
    sender: params.sender,
    nonce:
      typeof params.nonce === 'bigint' || typeof params.nonce === 'number'
        ? SequenceNumber.create(params.nonce)
        : params.nonce,
    expiry: TransactionExpiry.futureMinutes(params.expiryMinutes ?? 5),
  };

  const payload: UpdateContractPayload = {
    amount: CcdAmount.fromMicroCcd(params.amountMicroCcd ?? 0n),
    address: params.contract,
    receiveName: ReceiveName.fromString(params.receiveName),
    message: buildContractParameter(params.parameterHex, params.parameterBytes),
    maxContractExecutionEnergy: Energy.create(params.maxContractExecutionEnergy ?? 30000n),
  };

  return {
    type: AccountTransactionType.Update,
    header,
    payload,
  };
}

export interface SubmittedTransactionResult {
  transactionHash: TransactionHash.Type;
  summary?: BlockItemSummaryInBlock;
}

/**
 * Signs and submits an AccountTransaction to the Concordium node, optionally waiting for finalization.
 */
export async function signAndSendTransaction(
  client: ConcordiumGRPCNodeClient,
  transaction: AccountTransaction,
  signer: AccountSigner,
  waitForFinalization = true,
  finalizationTimeoutMs?: number
): Promise<SubmittedTransactionResult> {
  const signature = await signTransaction(transaction, signer);
  const transactionHash = await client.sendAccountTransaction(transaction, signature);

  if (!waitForFinalization) {
    return { transactionHash };
  }

  const summary = await client.waitForTransactionFinalization(
    transactionHash,
    finalizationTimeoutMs
  );
  return { transactionHash, summary };
}

export interface SubmitContractUpdateParams {
  contract: ContractAddress.Type;
  receiveName: string;
  parameterHex?: string;
  parameterBytes?: Uint8Array;
  amountMicroCcd?: bigint;
  maxContractExecutionEnergy?: bigint;
  expiryMinutes?: number;
  sender?: AccountAddress.Type;
  signer?: AccountSigner;
  waitForFinalization?: boolean;
}

/**
 * Convenience helper that fetches the next nonce, builds, signs, and submits a contract update.
 */
export async function submitContractUpdate(
  client: ConcordiumGRPCNodeClient,
  params: SubmitContractUpdateParams
): Promise<SubmittedTransactionResult> {
  const loaded = params.signer ? { signer: params.signer, address: params.sender } : loadAccountSignerFromEnv();
  const senderAddress =
    params.sender ??
    loaded.address ??
    AccountAddress.fromBase58(requireEnv('CONCORDIUM_AGENT_ACCOUNT_ADDRESS'));

  const { nonce } = await client.getNextAccountNonce(senderAddress);
  const tx = buildUpdateContractTransaction({
    sender: senderAddress,
    nonce,
    contract: params.contract,
    receiveName: params.receiveName,
    parameterHex: params.parameterHex,
    parameterBytes: params.parameterBytes,
    amountMicroCcd: params.amountMicroCcd,
    maxContractExecutionEnergy: params.maxContractExecutionEnergy,
    expiryMinutes: params.expiryMinutes,
  });

  return signAndSendTransaction(
    client,
    tx,
    loaded.signer,
    params.waitForFinalization ?? true
  );
}
