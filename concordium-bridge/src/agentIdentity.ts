import { generateKeyPairSync, sign as cryptoSign, verify as cryptoVerify } from 'node:crypto';
import { AccountAddress, ContractAddress } from '@concordium/web-sdk';
import { ConcordiumGRPCNodeClient } from '@concordium/web-sdk/nodejs';
import { canonicalJson, sha256Hex } from './canonical.js';
import {
  buildUpdateContractTransaction,
  getContractAddressFromEnv,
  invokeContractView,
  requireEnv,
  submitContractUpdate,
} from './client.js';

export const CIS8004_REGISTRATION_TYPE =
  'https://eips.ethereum.org/EIPS/eip-8004#registration-v1' as const;
export const CIS8004_PROTOCOL_VERSION = '1.0' as const;
export const CIS8_CANONICAL_DOMAIN_TAG = 'CIS-8/v1/canonical' as const;

export interface AgentProviderInfo {
  organization: string;
  url: string;
}

export interface AgentEndpoint {
  name: string;
  endpoint: string;
  protocolBinding?: string;
  version?: string;
}

export interface AgentCapabilities {
  streaming: boolean;
  pushNotifications: boolean;
  supportedTrust: string[];
  [key: string]: unknown;
}

export interface AgentRegistration {
  agentId: number | string;
  agentRegistry: string;
  network: string;
  standard: 'CIS-8004';
}

export interface ExternalKeyLinkMetadata {
  chain: string;
  keyType: string;
  publicKeyOrAddress: string;
  cis8Contract: string;
  canonicalDomainTag: typeof CIS8_CANONICAL_DOMAIN_TAG;
  digestSha256?: string;
}

export interface AgentCard {
  type: typeof CIS8004_REGISTRATION_TYPE;
  protocolVersion: string;
  name: string;
  description: string;
  version: string;
  provider: AgentProviderInfo;
  endpoints: AgentEndpoint[];
  capabilities: AgentCapabilities;
  registrations: AgentRegistration[];
  active: boolean;
  externalKeys?: ExternalKeyLinkMetadata[];
  metadata?: Record<string, unknown>;
}

export interface GenerateAgentCardInput {
  name?: string;
  description?: string;
  version?: string;
  providerOrganization?: string;
  providerUrl?: string;
  endpoints?: AgentEndpoint[];
  capabilities?: Partial<AgentCapabilities>;
  agentId?: number | string;
  network?: string;
  genesisHash?: string;
  cis8004Index?: string | number | bigint;
  cis8004Subindex?: string | number | bigint;
  active?: boolean;
  externalKeys?: ExternalKeyLinkMetadata[];
  metadata?: Record<string, unknown>;
}

function isValidHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:';
  } catch {
    return false;
  }
}

/**
 * Formats a Concordium CIS-8004 registry identifier in CAIP-like format:
 * `ccd:<genesisHashOrNetwork>:<index>.<subindex>`.
 */
export function formatAgentRegistryId(
  contract: ContractAddress.Type,
  genesisHashOrNetwork: string
): string {
  return `ccd:${genesisHashOrNetwork}:${contract.index.toString()}.${contract.subindex.toString()}`;
}

/**
 * Generates a CIS-8004 / ERC-8004 compliant `agent-card.json` object.
 * Reads unset fields from environment variables (never hardcoded credentials or endpoints).
 */
export function generateAgentCard(
  input: GenerateAgentCardInput = {},
  env: NodeJS.ProcessEnv = process.env
): AgentCard {
  const name = input.name ?? requireEnv('VERA_AGENT_NAME', env);
  const description = input.description ?? requireEnv('VERA_AGENT_DESCRIPTION', env);
  const version = input.version ?? requireEnv('VERA_AGENT_VERSION', env);
  const providerOrganization =
    input.providerOrganization ?? requireEnv('VERA_PROVIDER_ORG', env);
  const providerUrl = input.providerUrl ?? requireEnv('VERA_PROVIDER_URL', env);

  const endpoints: AgentEndpoint[] =
    input.endpoints ??
    [
      {
        name: env.VERA_AGENT_ENDPOINT_NAME?.trim() || 'VERA-API',
        endpoint: requireEnv('VERA_AGENT_ENDPOINT_URL', env),
        protocolBinding: env.VERA_AGENT_PROTOCOL_BINDING?.trim() || 'HTTP+JSON',
        version,
      },
    ];

  const network = input.network ?? requireEnv('CONCORDIUM_NETWORK', env);
  const genesisOrNet =
    input.genesisHash ?? (env.CONCORDIUM_GENESIS_HASH?.trim() || network);

  const cis8004Contract =
    input.cis8004Index !== undefined
      ? ContractAddress.create(BigInt(input.cis8004Index), BigInt(input.cis8004Subindex ?? 0))
      : getContractAddressFromEnv('CONCORDIUM_CIS8004', env);

  if (!cis8004Contract) {
    throw new Error(
      'Missing CIS-8004 contract address: provide cis8004Index or set CONCORDIUM_CIS8004_INDEX'
    );
  }

  const rawAgentId = input.agentId ?? requireEnv('VERA_AGENT_ID', env);
  const parsedNumericId = Number(rawAgentId);
  const agentId =
    typeof rawAgentId === 'number'
      ? rawAgentId
      : Number.isInteger(parsedNumericId) && String(parsedNumericId) === rawAgentId
        ? parsedNumericId
        : rawAgentId;

  const activeEnv = env.VERA_AGENT_ACTIVE?.trim().toLowerCase();
  const active =
    input.active ?? (activeEnv !== undefined && activeEnv !== '' ? activeEnv === 'true' : false);

  const card: AgentCard = {
    type: CIS8004_REGISTRATION_TYPE,
    protocolVersion: CIS8004_PROTOCOL_VERSION,
    name,
    description,
    version,
    provider: {
      organization: providerOrganization,
      url: providerUrl,
    },
    endpoints,
    capabilities: {
      streaming: input.capabilities?.streaming ?? false,
      pushNotifications: input.capabilities?.pushNotifications ?? false,
      supportedTrust: input.capabilities?.supportedTrust ?? ['attestation', 'crypto-economic'],
      ...input.capabilities,
    },
    registrations: [
      {
        agentId,
        agentRegistry: formatAgentRegistryId(cis8004Contract, genesisOrNet),
        network,
        standard: 'CIS-8004',
      },
    ],
    active,
  };

  if (input.externalKeys && input.externalKeys.length > 0) {
    card.externalKeys = input.externalKeys;
  }

  if (input.metadata) {
    card.metadata = input.metadata;
  }

  return card;
}

export interface AgentCardValidationResult {
  valid: boolean;
  errors: string[];
}

/**
 * Validates an `agent-card.json` payload against the CIS-8004 / ERC-8004 registration schema.
 */
export function validateAgentCard(card: unknown): AgentCardValidationResult {
  const errors: string[] = [];

  if (card === null || typeof card !== 'object' || Array.isArray(card)) {
    return { valid: false, errors: ['AgentCard must be a JSON object'] };
  }

  const obj = card as Record<string, unknown>;

  if (obj.type !== CIS8004_REGISTRATION_TYPE) {
    errors.push(`type must equal "${CIS8004_REGISTRATION_TYPE}"`);
  }

  for (const field of ['protocolVersion', 'name', 'description', 'version'] as const) {
    if (typeof obj[field] !== 'string' || (obj[field] as string).trim().length === 0) {
      errors.push(`${field} must be a non-empty string`);
    }
  }

  if (typeof obj.active !== 'boolean') {
    errors.push('active must be a boolean');
  }

  const provider = obj.provider as Record<string, unknown> | undefined;
  if (!provider || typeof provider !== 'object' || Array.isArray(provider)) {
    errors.push('provider must be an object');
  } else {
    if (typeof provider.organization !== 'string' || !provider.organization.trim()) {
      errors.push('provider.organization must be a non-empty string');
    }
    if (typeof provider.url !== 'string' || !isValidHttpUrl(provider.url)) {
      errors.push('provider.url must be a valid HTTP/HTTPS URL');
    }
  }

  if (!Array.isArray(obj.endpoints) || obj.endpoints.length === 0) {
    errors.push('endpoints must be a non-empty array');
  } else {
    obj.endpoints.forEach((ep, idx) => {
      if (!ep || typeof ep !== 'object' || Array.isArray(ep)) {
        errors.push(`endpoints[${idx}] must be an object`);
        return;
      }
      const item = ep as Record<string, unknown>;
      if (typeof item.name !== 'string' || !item.name.trim()) {
        errors.push(`endpoints[${idx}].name must be a non-empty string`);
      }
      if (typeof item.endpoint !== 'string' || !isValidHttpUrl(item.endpoint)) {
        errors.push(`endpoints[${idx}].endpoint must be a valid HTTP/HTTPS URL`);
      }
    });
  }

  const caps = obj.capabilities as Record<string, unknown> | undefined;
  if (!caps || typeof caps !== 'object' || Array.isArray(caps)) {
    errors.push('capabilities must be an object');
  } else {
    if (caps.streaming !== undefined && typeof caps.streaming !== 'boolean') {
      errors.push('capabilities.streaming must be a boolean');
    }
    if (
      caps.pushNotifications !== undefined &&
      typeof caps.pushNotifications !== 'boolean'
    ) {
      errors.push('capabilities.pushNotifications must be a boolean');
    }
    if (
      caps.supportedTrust !== undefined &&
      (!Array.isArray(caps.supportedTrust) ||
        caps.supportedTrust.some((t) => typeof t !== 'string' || !t.trim()))
    ) {
      errors.push('capabilities.supportedTrust must be an array of non-empty strings');
    }
  }

  if (!Array.isArray(obj.registrations) || obj.registrations.length === 0) {
    errors.push('registrations must be a non-empty array');
  } else {
    obj.registrations.forEach((reg, idx) => {
      if (!reg || typeof reg !== 'object' || Array.isArray(reg)) {
        errors.push(`registrations[${idx}] must be an object`);
        return;
      }
      const item = reg as Record<string, unknown>;
      if (
        (typeof item.agentId !== 'number' && typeof item.agentId !== 'string') ||
        String(item.agentId).trim().length === 0
      ) {
        errors.push(`registrations[${idx}].agentId must be a number or non-empty string`);
      }
      if (
        typeof item.agentRegistry !== 'string' ||
        !/^ccd:[^:]+:\d+\.\d+$/.test(item.agentRegistry)
      ) {
        errors.push(
          `registrations[${idx}].agentRegistry must match format "ccd:<chainOrGenesis>:<index>.<subindex>"`
        );
      }
      if (typeof item.standard !== 'string' || !item.standard.trim()) {
        errors.push(`registrations[${idx}].standard must be a non-empty string`);
      }
    });
  }

  return { valid: errors.length === 0, errors };
}

/**
 * Computes the canonical SHA-256 commitment hash for `agent-card.json`.
 */
export function calculateAgentCardHash(card: AgentCard): string {
  const validation = validateAgentCard(card);
  if (!validation.valid) {
    throw new Error(`Invalid AgentCard: ${validation.errors.join('; ')}`);
  }
  return sha256Hex(canonicalJson(card));
}

export interface RegisterAgentCIS8004Options {
  agentCard: AgentCard;
  agentUri: string;
  mode?: 'simulate' | 'submit';
  client?: ConcordiumGRPCNodeClient;
  contractAddress?: ContractAddress.Type;
  receiveName?: string;
  senderAddress?: string;
  nonce?: bigint | number;
  maxContractExecutionEnergy?: bigint;
}

export interface RegisterAgentCIS8004Result {
  mode: 'simulate' | 'submit';
  contract: string;
  receiveName: string;
  agentId: number | string;
  agentUri: string;
  agentCardHash: string;
  parameterJson: string;
  parameterHex: string;
  dryRunResult?: unknown;
  unsignedTransaction?: ReturnType<typeof buildUpdateContractTransaction>;
  transactionHash?: string;
}

/**
 * Registers (or simulates registration of) a VERA agent in the CIS-8004 registry contract.
 * Commits `agentUri` and the canonical SHA-256 `agentCardHash`.
 */
export async function registerAgentInCIS8004(
  options: RegisterAgentCIS8004Options
): Promise<RegisterAgentCIS8004Result> {
  const validation = validateAgentCard(options.agentCard);
  if (!validation.valid) {
    throw new Error(`Invalid AgentCard: ${validation.errors.join('; ')}`);
  }
  if (!isValidHttpUrl(options.agentUri)) {
    throw new Error(`Invalid agentUri URL: ${options.agentUri}`);
  }

  const mode = options.mode ?? 'simulate';
  const contract =
    options.contractAddress ?? getContractAddressFromEnv('CONCORDIUM_CIS8004');
  if (!contract) {
    throw new Error(
      'Missing CIS-8004 contract address: pass contractAddress or set CONCORDIUM_CIS8004_INDEX'
    );
  }

  const receiveName =
    options.receiveName ??
    (process.env.CONCORDIUM_CIS8004_REGISTER_ENTRYPOINT?.trim() ||
      'cis8004_registry.register_agent');

  const agentCardHash = calculateAgentCardHash(options.agentCard);
  const agentId = options.agentCard.registrations[0].agentId;

  const registrationPayload = {
    agent_id: String(agentId),
    agent_uri: options.agentUri,
    agent_card_hash: agentCardHash,
    active: options.agentCard.active,
  };

  const parameterJson = canonicalJson(registrationPayload);
  const parameterHex = Buffer.from(parameterJson, 'utf8').toString('hex');

  if (mode === 'submit') {
    if (!options.client) {
      throw new Error('ConcordiumGRPCNodeClient is required when mode="submit"');
    }
    const submitted = await submitContractUpdate(options.client, {
      contract,
      receiveName,
      parameterHex,
      maxContractExecutionEnergy: options.maxContractExecutionEnergy,
      sender: options.senderAddress
        ? AccountAddress.fromBase58(options.senderAddress)
        : undefined,
    });

    return {
      mode,
      contract: contract.toString(),
      receiveName,
      agentId,
      agentUri: options.agentUri,
      agentCardHash,
      parameterJson,
      parameterHex,
      transactionHash: submitted.transactionHash.toString(),
    };
  }

  let dryRunResult: unknown;
  if (options.client) {
    dryRunResult = await invokeContractView(options.client, {
      contract,
      receiveName,
      parameterHex,
      invoker: options.senderAddress
        ? AccountAddress.fromBase58(options.senderAddress)
        : undefined,
      energy: options.maxContractExecutionEnergy,
    });
  }

  const senderForTx =
    options.senderAddress ?? process.env.CONCORDIUM_AGENT_ACCOUNT_ADDRESS?.trim();
  const unsignedTransaction = senderForTx
    ? buildUpdateContractTransaction({
        sender: AccountAddress.fromBase58(senderForTx),
        nonce: options.nonce ?? 1n,
        contract,
        receiveName,
        parameterHex,
        maxContractExecutionEnergy: options.maxContractExecutionEnergy,
      })
    : undefined;

  return {
    mode,
    contract: contract.toString(),
    receiveName,
    agentId,
    agentUri: options.agentUri,
    agentCardHash,
    parameterJson,
    parameterHex,
    dryRunResult,
    unsignedTransaction,
  };
}

export interface Cis8CanonicalMessageParams {
  concordiumAccount: string;
  cis8Contract: ContractAddress.Type;
  externalChain: string;
  keyType: 'ed25519' | 'secp256k1' | string;
  externalPublicKey: string;
  network?: string;
  agentId?: number | string;
}

export interface Cis8CanonicalMessage {
  domainTag: typeof CIS8_CANONICAL_DOMAIN_TAG;
  canonicalPayload: string;
  message: string;
  messageBytes: Buffer;
  digestSha256: string;
}

/**
 * Builds the deterministic CIS-8 canonical message prefixed with the
 * 18-byte ASCII domain separation tag `CIS-8/v1/canonical`.
 */
export function buildCis8CanonicalMessage(
  params: Cis8CanonicalMessageParams
): Cis8CanonicalMessage {
  // Validate Concordium account address format
  AccountAddress.fromBase58(params.concordiumAccount);

  const payload: Record<string, unknown> = {
    concordium_account: params.concordiumAccount,
    cis8_contract: {
      index: params.cis8Contract.index.toString(),
      subindex: params.cis8Contract.subindex.toString(),
    },
    external_chain: params.externalChain,
    key_type: params.keyType,
    external_public_key: params.externalPublicKey,
  };

  if (params.network) {
    payload.network = params.network;
  }
  if (params.agentId !== undefined) {
    payload.agent_id = String(params.agentId);
  }

  const canonicalPayload = canonicalJson(payload);
  const message = `${CIS8_CANONICAL_DOMAIN_TAG}\n${canonicalPayload}`;
  const messageBytes = Buffer.from(message, 'utf8');
  const digestSha256 = sha256Hex(messageBytes);

  return {
    domainTag: CIS8_CANONICAL_DOMAIN_TAG,
    canonicalPayload,
    message,
    messageBytes,
    digestSha256,
  };
}

export interface LinkExternalKeyCIS8Options {
  concordiumAccount?: string;
  externalChain: string;
  keyType: 'ed25519' | 'secp256k1' | string;
  externalPublicKey?: string;
  externalSignatureHex?: string;
  agentId?: number | string;
  network?: string;
  cis8Contract?: ContractAddress.Type;
  receiveName?: string;
  mode?: 'simulate' | 'submit';
  client?: ConcordiumGRPCNodeClient;
}

export interface LinkExternalKeyCIS8Result {
  mode: 'simulate' | 'submit';
  cis8Contract: string;
  receiveName: string;
  canonicalMessage: Cis8CanonicalMessage;
  externalPublicKey: string;
  externalSignatureHex: string;
  signatureVerified?: boolean;
  parameterJson: string;
  parameterHex: string;
  metadataEntry: ExternalKeyLinkMetadata;
  transactionHash?: string;
}

/**
 * Links (or simulates linking) an external blockchain key to a Concordium account/agent
 * following the CIS-8 standard with `CIS-8/v1/canonical` domain separation.
 *
 * In `simulate` mode, if `externalPublicKey` / `externalSignatureHex` are omitted,
 * an ephemeral Ed25519 keypair is generated in memory (never persisted or hardcoded).
 */
export async function linkExternalKeyCIS8(
  options: LinkExternalKeyCIS8Options
): Promise<LinkExternalKeyCIS8Result> {
  const mode = options.mode ?? 'simulate';
  const concordiumAccount =
    options.concordiumAccount ?? requireEnv('CONCORDIUM_AGENT_ACCOUNT_ADDRESS');
  const cis8Contract =
    options.cis8Contract ?? getContractAddressFromEnv('CONCORDIUM_CIS8');

  if (!cis8Contract) {
    throw new Error(
      'Missing CIS-8 contract address: pass cis8Contract or set CONCORDIUM_CIS8_INDEX'
    );
  }

  const network = options.network ?? process.env.CONCORDIUM_NETWORK?.trim();
  const receiveName =
    options.receiveName ??
    (process.env.CONCORDIUM_CIS8_REGISTER_ENTRYPOINT?.trim() ||
      'cis8_registry.register_external_key');

  let externalPublicKey = options.externalPublicKey;
  let externalSignatureHex = options.externalSignatureHex;
  let signatureVerified: boolean | undefined;

  if (!externalPublicKey || !externalSignatureHex) {
    if (mode === 'submit') {
      throw new Error(
        'externalPublicKey and externalSignatureHex are required when mode="submit"'
      );
    }
    // Ephemeral Ed25519 simulation keypair (no hardcoded secrets)
    const { publicKey, privateKey } = generateKeyPairSync('ed25519');
    const rawSpki = publicKey.export({ format: 'der', type: 'spki' });
    // Last 32 bytes of Ed25519 SPKI DER are the raw 32-byte public key
    externalPublicKey = rawSpki.subarray(rawSpki.length - 32).toString('hex');

    const canonicalMessage = buildCis8CanonicalMessage({
      concordiumAccount,
      cis8Contract,
      externalChain: options.externalChain,
      keyType: 'ed25519',
      externalPublicKey,
      network,
      agentId: options.agentId,
    });

    const sig = cryptoSign(null, canonicalMessage.messageBytes, privateKey);
    externalSignatureHex = sig.toString('hex');
    signatureVerified = cryptoVerify(null, canonicalMessage.messageBytes, publicKey, sig);

    const payload = {
      concordium_account: concordiumAccount,
      domain_tag: CIS8_CANONICAL_DOMAIN_TAG,
      external_chain: options.externalChain,
      key_type: 'ed25519',
      external_public_key: externalPublicKey,
      message_sha256: canonicalMessage.digestSha256,
      signature: externalSignatureHex,
    };

    const parameterJson = canonicalJson(payload);
    const parameterHex = Buffer.from(parameterJson, 'utf8').toString('hex');

    return {
      mode,
      cis8Contract: cis8Contract.toString(),
      receiveName,
      canonicalMessage,
      externalPublicKey,
      externalSignatureHex,
      signatureVerified,
      parameterJson,
      parameterHex,
      metadataEntry: {
        chain: options.externalChain,
        keyType: 'ed25519',
        publicKeyOrAddress: externalPublicKey,
        cis8Contract: cis8Contract.toString(),
        canonicalDomainTag: CIS8_CANONICAL_DOMAIN_TAG,
        digestSha256: canonicalMessage.digestSha256,
      },
    };
  }

  const canonicalMessage = buildCis8CanonicalMessage({
    concordiumAccount,
    cis8Contract,
    externalChain: options.externalChain,
    keyType: options.keyType,
    externalPublicKey,
    network,
    agentId: options.agentId,
  });

  const payload = {
    concordium_account: concordiumAccount,
    domain_tag: CIS8_CANONICAL_DOMAIN_TAG,
    external_chain: options.externalChain,
    key_type: options.keyType,
    external_public_key: externalPublicKey,
    message_sha256: canonicalMessage.digestSha256,
    signature: externalSignatureHex,
  };

  const parameterJson = canonicalJson(payload);
  const parameterHex = Buffer.from(parameterJson, 'utf8').toString('hex');

  let transactionHash: string | undefined;
  if (mode === 'submit') {
    if (!options.client) {
      throw new Error('ConcordiumGRPCNodeClient is required when mode="submit"');
    }
    const submitted = await submitContractUpdate(options.client, {
      contract: cis8Contract,
      receiveName,
      parameterHex,
      sender: AccountAddress.fromBase58(concordiumAccount),
    });
    transactionHash = submitted.transactionHash.toString();
  }

  return {
    mode,
    cis8Contract: cis8Contract.toString(),
    receiveName,
    canonicalMessage,
    externalPublicKey,
    externalSignatureHex,
    signatureVerified,
    parameterJson,
    parameterHex,
    metadataEntry: {
      chain: options.externalChain,
      keyType: options.keyType,
      publicKeyOrAddress: externalPublicKey,
      cis8Contract: cis8Contract.toString(),
      canonicalDomainTag: CIS8_CANONICAL_DOMAIN_TAG,
      digestSha256: canonicalMessage.digestSha256,
    },
    transactionHash,
  };
}
