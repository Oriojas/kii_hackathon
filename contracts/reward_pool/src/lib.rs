#![no_std]

use concordium_std::*;

/// Lifecycle status matching `evidence_registry::EvidenceStatus`.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub enum EvidenceStatus {
    Active,
    Revoked,
    Expired,
}

/// Source classification matching `evidence_registry::SourceType`.
#[derive(Serialize, SchemaType, Clone, Copy, Debug, PartialEq, Eq)]
#[concordium(repr(u8))]
pub enum SourceType {
    #[concordium(tag = 0)]
    Physical,
    #[concordium(tag = 1)]
    Synthetic,
}

/// Read-only view returned by `evidence_registry::get_evidence`.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub struct RegistryEvidenceView {
    pub evidence_id: [u8; 32],
    pub evidence_root: [u8; 32],
    pub source_type: SourceType,
    pub creator: Address,
    pub registered_at: Timestamp,
    pub calibration_expires_at: Timestamp,
    pub metadata_uri: String,
    pub status: EvidenceStatus,
    pub is_active: bool,
    pub is_revoked: bool,
    pub is_calibration_valid: bool,
}

/// Initialization parameters for `reward_pool`.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub struct InitParams {
    pub policy_hash: [u8; 32],
    pub evidence_registry: Option<ContractAddress>,
}

/// Parameters for `settle_reward`.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub struct SettleRewardParams {
    pub recipient: AccountAddress,
    pub evidence_id: [u8; 32],
    pub policy_hash: [u8; 32],
    pub amount: Amount,
    pub calibration_expires_at: Timestamp,
}

/// Stored record of a completed reward settlement.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub struct SettlementRecord {
    pub recipient: AccountAddress,
    pub policy_hash: [u8; 32],
    pub amount: Amount,
    pub settled_at: Timestamp,
}

/// Summary view of the pool state.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub struct PoolInfo {
    pub admin: AccountAddress,
    pub settler: AccountAddress,
    pub policy_hash: [u8; 32],
    pub evidence_registry: Option<ContractAddress>,
    pub paused: bool,
    pub balance: Amount,
    pub total_funded: Amount,
    pub total_settled: Amount,
}

/// Contract state for `reward_pool`.
#[derive(Serial, DeserialWithState)]
#[concordium(state_parameter = "S")]
pub struct State<S = StateApi> {
    pub admin: AccountAddress,
    pub settler: AccountAddress,
    pub policy_hash: [u8; 32],
    pub evidence_registry: Option<ContractAddress>,
    pub paused: bool,
    pub settlements: StateMap<[u8; 32], SettlementRecord, S>,
    pub total_funded: Amount,
    pub total_settled: Amount,
}

/// Events emitted by `reward_pool`.
#[derive(Serialize, SchemaType, Debug, PartialEq, Eq)]
pub enum Event {
    PoolFunded {
        funder: Address,
        amount: Amount,
        new_balance: Amount,
    },
    PolicyHashUpdated {
        old_policy_hash: [u8; 32],
        new_policy_hash: [u8; 32],
        updated_by: AccountAddress,
    },
    RewardSettled {
        recipient: AccountAddress,
        evidence_id: [u8; 32],
        policy_hash: [u8; 32],
        amount: Amount,
        settled_at: Timestamp,
    },
    PauseStateChanged {
        paused: bool,
        changed_by: AccountAddress,
    },
}

/// Custom errors for `reward_pool`.
#[derive(Debug, PartialEq, Eq, Reject, Serialize, SchemaType)]
pub enum Error {
    /// Failed parsing the parameter.
    #[from(ParseError)]
    ParseParams,
    /// Contract log is full.
    LogFull,
    /// Caller is not authorized to perform this action.
    Unauthorized,
    /// Contract is currently paused due to emergency halt.
    ContractPaused,
    /// Amount must be greater than zero.
    ZeroAmount,
    /// Reward for this evidence ID has already been settled (anti-double-spend).
    AlreadySettled,
    /// Provided policy hash does not match the active contract policy hash.
    InvalidPolicyHash,
    /// Calibration window has expired.
    CalibrationExpired,
    /// Evidence is revoked in the linked registry.
    EvidenceRevoked,
    /// Cross-contract registry query failed or returned malformed data.
    RegistryCheckFailed,
    /// Pool balance is insufficient for the requested reward amount.
    InsufficientPoolBalance,
    /// Native CCD transfer failed.
    TransferFailed,
}

/// Initialize the Reward Pool contract. May optionally be funded on init.
#[init(
    contract = "reward_pool",
    parameter = "InitParams",
    event = "Event",
    error = "Error",
    payable
)]
fn init(ctx: &InitContext, state_builder: &mut StateBuilder, amount: Amount) -> InitResult<State> {
    let params: InitParams = ctx.parameter_cursor().get()?;
    let origin = ctx.init_origin();

    Ok(State {
        admin: origin,
        settler: origin,
        policy_hash: params.policy_hash,
        evidence_registry: params.evidence_registry,
        paused: false,
        settlements: state_builder.new_map(),
        total_funded: amount,
        total_settled: Amount::zero(),
    })
}

/// Deposit CCD into the reward pool.
#[receive(
    contract = "reward_pool",
    name = "fund",
    payable,
    mutable,
    enable_logger,
    error = "Error"
)]
fn fund(
    ctx: &ReceiveContext,
    host: &mut Host<State>,
    amount: Amount,
    logger: &mut Logger,
) -> Result<(), Error> {
    ensure!(!host.state().paused, Error::ContractPaused);
    ensure!(amount > Amount::zero(), Error::ZeroAmount);

    host.state_mut().total_funded += amount;
    let new_balance = host.self_balance();

    logger
        .log(&Event::PoolFunded {
            funder: ctx.sender(),
            amount,
            new_balance,
        })
        .map_err(|_| Error::LogFull)?;

    Ok(())
}

/// Update the active deterministic `policy_hash`. Admin only.
#[receive(
    contract = "reward_pool",
    name = "update_policy_hash",
    parameter = "[u8; 32]",
    mutable,
    enable_logger,
    error = "Error"
)]
fn update_policy_hash(
    ctx: &ReceiveContext,
    host: &mut Host<State>,
    logger: &mut Logger,
) -> Result<(), Error> {
    let new_policy_hash: [u8; 32] = ctx.parameter_cursor().get()?;
    let state = host.state_mut();
    ensure!(
        ctx.sender() == Address::Account(state.admin),
        Error::Unauthorized
    );

    let old_policy_hash = state.policy_hash;
    state.policy_hash = new_policy_hash;

    logger
        .log(&Event::PolicyHashUpdated {
            old_policy_hash,
            new_policy_hash,
            updated_by: state.admin,
        })
        .map_err(|_| Error::LogFull)?;

    Ok(())
}

/// Toggle emergency pause state (`true` = paused, `false` = active). Admin only.
#[receive(
    contract = "reward_pool",
    name = "set_paused",
    parameter = "bool",
    mutable,
    enable_logger,
    error = "Error"
)]
fn set_paused(ctx: &ReceiveContext, host: &mut Host<State>, logger: &mut Logger) -> Result<(), Error> {
    let paused: bool = ctx.parameter_cursor().get()?;
    apply_pause_state(ctx, host, logger, paused)
}

/// Activate emergency pause. Admin only.
#[receive(
    contract = "reward_pool",
    name = "pause",
    mutable,
    enable_logger,
    error = "Error"
)]
fn pause(ctx: &ReceiveContext, host: &mut Host<State>, logger: &mut Logger) -> Result<(), Error> {
    apply_pause_state(ctx, host, logger, true)
}

/// Lift emergency pause. Admin only.
#[receive(
    contract = "reward_pool",
    name = "unpause",
    mutable,
    enable_logger,
    error = "Error"
)]
fn unpause(ctx: &ReceiveContext, host: &mut Host<State>, logger: &mut Logger) -> Result<(), Error> {
    apply_pause_state(ctx, host, logger, false)
}

fn apply_pause_state(
    ctx: &ReceiveContext,
    host: &mut Host<State>,
    logger: &mut Logger,
    paused: bool,
) -> Result<(), Error> {
    let state = host.state_mut();
    ensure!(
        ctx.sender() == Address::Account(state.admin),
        Error::Unauthorized
    );
    state.paused = paused;

    logger
        .log(&Event::PauseStateChanged {
            paused,
            changed_by: state.admin,
        })
        .map_err(|_| Error::LogFull)?;

    Ok(())
}

/// Update the authorized settler account. Admin only.
#[receive(
    contract = "reward_pool",
    name = "set_settler",
    parameter = "AccountAddress",
    mutable,
    error = "Error"
)]
fn set_settler(ctx: &ReceiveContext, host: &mut Host<State>) -> Result<(), Error> {
    let settler: AccountAddress = ctx.parameter_cursor().get()?;
    let state = host.state_mut();
    ensure!(
        ctx.sender() == Address::Account(state.admin),
        Error::Unauthorized
    );
    state.settler = settler;
    Ok(())
}

/// Set or clear the optional linked `evidence_registry` contract. Admin only.
#[receive(
    contract = "reward_pool",
    name = "set_evidence_registry",
    parameter = "Option<ContractAddress>",
    mutable,
    error = "Error"
)]
fn set_evidence_registry(ctx: &ReceiveContext, host: &mut Host<State>) -> Result<(), Error> {
    let registry: Option<ContractAddress> = ctx.parameter_cursor().get()?;
    let state = host.state_mut();
    ensure!(
        ctx.sender() == Address::Account(state.admin),
        Error::Unauthorized
    );
    state.evidence_registry = registry;
    Ok(())
}

/// Validate settlement preconditions (excluding external cross-contract call) against pool state.
fn validate_settlement_preconditions(
    sender: Address,
    now: Timestamp,
    pool_balance: Amount,
    state: &State,
    params: &SettleRewardParams,
) -> Result<(), Error> {
    ensure!(
        sender == Address::Account(state.admin) || sender == Address::Account(state.settler),
        Error::Unauthorized
    );
    ensure!(!state.paused, Error::ContractPaused);
    ensure!(params.amount > Amount::zero(), Error::ZeroAmount);
    ensure!(
        state.settlements.get(&params.evidence_id).is_none(),
        Error::AlreadySettled
    );
    ensure!(
        params.policy_hash == state.policy_hash,
        Error::InvalidPolicyHash
    );
    ensure!(
        now < params.calibration_expires_at,
        Error::CalibrationExpired
    );
    ensure!(pool_balance >= params.amount, Error::InsufficientPoolBalance);
    Ok(())
}

/// Record a validated settlement in contract state before initiating the native transfer.
fn record_settlement(
    state: &mut State,
    params: &SettleRewardParams,
    settled_at: Timestamp,
) {
    let _ = state.settlements.insert(
        params.evidence_id,
        SettlementRecord {
            recipient: params.recipient,
            policy_hash: params.policy_hash,
            amount: params.amount,
            settled_at,
        },
    );
    state.total_settled += params.amount;
}

/// Verify policy hash, calibration validity, and anti-double-spend, then settle reward to recipient.
#[receive(
    contract = "reward_pool",
    name = "settle_reward",
    parameter = "SettleRewardParams",
    mutable,
    enable_logger,
    error = "Error"
)]
fn settle_reward(
    ctx: &ReceiveContext,
    host: &mut Host<State>,
    logger: &mut Logger,
) -> Result<(), Error> {
    let params: SettleRewardParams = ctx.parameter_cursor().get()?;
    let now = ctx.metadata().slot_time();
    let sender = ctx.sender();
    let pool_balance = host.self_balance();

    validate_settlement_preconditions(sender, now, pool_balance, host.state(), &params)?;

    if let Some(registry_addr) = host.state().evidence_registry {
        let mut response = host
            .invoke_contract_read_only(
                &registry_addr,
                &params.evidence_id,
                EntrypointName::new_unchecked("get_evidence"),
                Amount::zero(),
            )
            .map_err(|_| Error::RegistryCheckFailed)?
            .ok_or(Error::RegistryCheckFailed)?;

        let view: RegistryEvidenceView =
            response.get().map_err(|_| Error::RegistryCheckFailed)?;

        ensure!(!view.is_revoked, Error::EvidenceRevoked);
        ensure!(
            view.is_calibration_valid && now < view.calibration_expires_at,
            Error::CalibrationExpired
        );
        ensure!(view.is_active, Error::RegistryCheckFailed);
    }

    // Record settlement before external transfer (checks-effects-interactions).
    record_settlement(host.state_mut(), &params, now);

    host.invoke_transfer(&params.recipient, params.amount)
        .map_err(|_| Error::TransferFailed)?;

    logger
        .log(&Event::RewardSettled {
            recipient: params.recipient,
            evidence_id: params.evidence_id,
            policy_hash: params.policy_hash,
            amount: params.amount,
            settled_at: now,
        })
        .map_err(|_| Error::LogFull)?;

    Ok(())
}

/// Return current pool configuration and balances.
#[receive(
    contract = "reward_pool",
    name = "get_pool_info",
    return_value = "PoolInfo"
)]
fn get_pool_info(_ctx: &ReceiveContext, host: &Host<State>) -> ReceiveResult<PoolInfo> {
    let state = host.state();
    Ok(PoolInfo {
        admin: state.admin,
        settler: state.settler,
        policy_hash: state.policy_hash,
        evidence_registry: state.evidence_registry,
        paused: state.paused,
        balance: host.self_balance(),
        total_funded: state.total_funded,
        total_settled: state.total_settled,
    })
}

/// Check whether a reward for `evidence_id` has already been settled.
#[receive(
    contract = "reward_pool",
    name = "is_settled",
    parameter = "[u8; 32]",
    return_value = "bool"
)]
fn is_settled(ctx: &ReceiveContext, host: &Host<State>) -> ReceiveResult<bool> {
    let evidence_id: [u8; 32] = ctx.parameter_cursor().get()?;
    Ok(host.state().settlements.get(&evidence_id).is_some())
}

/// Retrieve the settlement record for a given `evidence_id`, if settled.
#[receive(
    contract = "reward_pool",
    name = "get_settlement",
    parameter = "[u8; 32]",
    return_value = "Option<SettlementRecord>"
)]
fn get_settlement(
    ctx: &ReceiveContext,
    host: &Host<State>,
) -> ReceiveResult<Option<SettlementRecord>> {
    let evidence_id: [u8; 32] = ctx.parameter_cursor().get()?;
    Ok(host.state().settlements.get(&evidence_id).map(|r| r.clone()))
}

#[concordium_cfg_test]
mod tests {
    #[cfg(target_arch = "wasm32")]
    use super::*;
    #[cfg(target_arch = "wasm32")]
    use concordium_std::test_env::TestEnv;

    #[cfg(target_arch = "wasm32")]
    const ADMIN: AccountAddress = AccountAddress([1u8; 32]);
    #[cfg(target_arch = "wasm32")]
    const RECIPIENT: AccountAddress = AccountAddress([2u8; 32]);
    #[cfg(target_arch = "wasm32")]
    const FUNDER: AccountAddress = AccountAddress([3u8; 32]);
    #[cfg(target_arch = "wasm32")]
    const POLICY_HASH: [u8; 32] = [0xAA; 32];
    #[cfg(target_arch = "wasm32")]
    const NEW_POLICY_HASH: [u8; 32] = [0xBB; 32];

    #[cfg(target_arch = "wasm32")]
    fn setup_funded_host(initial_ccd: u64) -> Host<State> {
        let init_params = InitParams {
            policy_hash: POLICY_HASH,
            evidence_registry: None,
        };
        TestEnv
            .set_init_origin(ADMIN)
            .set_parameter(0, &to_bytes(&init_params));

        let init_ctx = InitContext::default();
        let state_api = ExternStateApi::open();
        let mut state_builder = StateBuilder::open(state_api);
        let initial_amount = Amount::from_ccd(initial_ccd);
        let state =
            init(&init_ctx, &mut state_builder, initial_amount).expect("init should succeed");
        TestEnv.set_receive_balance(initial_amount);
        ExternHost {
            state,
            state_builder,
        }
    }

    #[cfg(target_arch = "wasm32")]
    #[concordium_test]
    fn test_fund_and_settle_reward_success() {
        let mut host = setup_funded_host(10);
        let ctx = ReceiveContext::default();
        let mut logger = Logger::default();

        // Fund with additional 5 CCD
        TestEnv
            .set_receive_sender(Address::Account(FUNDER))
            .set_receive_balance(Amount::from_ccd(15));
        let extra = Amount::from_ccd(5);
        claim!(fund(&ctx, &mut host, extra, &mut logger).is_ok());
        claim_eq!(host.state().total_funded, Amount::from_ccd(15));

        // Validate and record settlement of 3 CCD
        let evidence_id = [21u8; 32];
        let params = SettleRewardParams {
            recipient: RECIPIENT,
            evidence_id,
            policy_hash: POLICY_HASH,
            amount: Amount::from_ccd(3),
            calibration_expires_at: Timestamp::from_timestamp_millis(20_000),
        };
        let now = Timestamp::from_timestamp_millis(5_000);
        let res = validate_settlement_preconditions(
            Address::Account(ADMIN),
            now,
            host.self_balance(),
            host.state(),
            &params,
        );
        claim!(res.is_ok(), "settlement preconditions should pass");
        record_settlement(host.state_mut(), &params, now);

        claim_eq!(host.state().total_settled, Amount::from_ccd(3));
        claim!(host.state().settlements.get(&evidence_id).is_some());
    }

    #[cfg(target_arch = "wasm32")]
    #[concordium_test]
    fn test_settle_reward_replay_rejected() {
        let mut host = setup_funded_host(20);
        let evidence_id = [22u8; 32];
        let params = SettleRewardParams {
            recipient: RECIPIENT,
            evidence_id,
            policy_hash: POLICY_HASH,
            amount: Amount::from_ccd(4),
            calibration_expires_at: Timestamp::from_timestamp_millis(30_000),
        };
        let now = Timestamp::from_timestamp_millis(1_000);

        claim!(validate_settlement_preconditions(
            Address::Account(ADMIN),
            now,
            host.self_balance(),
            host.state(),
            &params,
        )
        .is_ok());
        record_settlement(host.state_mut(), &params, now);

        // Replay with identical evidence_id must fail even via full entrypoint
        TestEnv
            .set_receive_sender(Address::Account(ADMIN))
            .set_slot_time(now)
            .set_parameter(0, &to_bytes(&params));
        let mut logger = Logger::default();
        let ctx = ReceiveContext::default();
        let replay = settle_reward(&ctx, &mut host, &mut logger);
        claim_eq!(replay, Err(Error::AlreadySettled));
        claim_eq!(host.state().total_settled, Amount::from_ccd(4));
    }

    #[cfg(target_arch = "wasm32")]
    #[concordium_test]
    fn test_settle_reward_expired_calibration_rejected() {
        let mut host = setup_funded_host(20);
        let evidence_id = [23u8; 32];
        let params = SettleRewardParams {
            recipient: RECIPIENT,
            evidence_id,
            policy_hash: POLICY_HASH,
            amount: Amount::from_ccd(2),
            calibration_expires_at: Timestamp::from_timestamp_millis(10_000),
        };

        TestEnv
            .set_receive_sender(Address::Account(ADMIN))
            .set_slot_time(Timestamp::from_timestamp_millis(10_000))
            .set_parameter(0, &to_bytes(&params));

        let ctx = ReceiveContext::default();
        let mut logger = Logger::default();
        let res = settle_reward(&ctx, &mut host, &mut logger);
        claim_eq!(res, Err(Error::CalibrationExpired));
        claim_eq!(host.state().total_settled, Amount::zero());
    }

    #[cfg(target_arch = "wasm32")]
    #[concordium_test]
    fn test_policy_hash_update_and_emergency_pause() {
        let mut host = setup_funded_host(20);
        let ctx = ReceiveContext::default();
        let mut logger = Logger::default();

        // Update policy hash as admin
        TestEnv
            .set_receive_sender(Address::Account(ADMIN))
            .set_parameter(0, &to_bytes(&NEW_POLICY_HASH));
        claim!(update_policy_hash(&ctx, &mut host, &mut logger).is_ok());
        claim_eq!(host.state().policy_hash, NEW_POLICY_HASH);

        // Old policy hash must be rejected
        let evidence_id = [24u8; 32];
        let old_policy_params = SettleRewardParams {
            recipient: RECIPIENT,
            evidence_id,
            policy_hash: POLICY_HASH,
            amount: Amount::from_ccd(2),
            calibration_expires_at: Timestamp::from_timestamp_millis(20_000),
        };
        TestEnv
            .set_slot_time(Timestamp::from_timestamp_millis(5_000))
            .set_parameter(0, &to_bytes(&old_policy_params));
        claim_eq!(
            settle_reward(&ctx, &mut host, &mut logger),
            Err(Error::InvalidPolicyHash)
        );

        // Emergency pause blocks settlement even with valid policy
        claim!(pause(&ctx, &mut host, &mut logger).is_ok());
        let valid_params = SettleRewardParams {
            policy_hash: NEW_POLICY_HASH,
            ..old_policy_params
        };
        TestEnv.set_parameter(0, &to_bytes(&valid_params));
        claim_eq!(
            settle_reward(&ctx, &mut host, &mut logger),
            Err(Error::ContractPaused)
        );

        // Unpause allows validation to succeed
        claim!(unpause(&ctx, &mut host, &mut logger).is_ok());
        claim!(validate_settlement_preconditions(
            Address::Account(ADMIN),
            Timestamp::from_timestamp_millis(5_000),
            host.self_balance(),
            host.state(),
            &valid_params,
        )
        .is_ok());
    }
}
