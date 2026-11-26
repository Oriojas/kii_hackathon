#![no_std]

use concordium_std::*;

/// Source classification for submitted environmental evidence.
#[derive(Serialize, SchemaType, Clone, Copy, Debug, PartialEq, Eq)]
#[concordium(repr(u8))]
pub enum SourceType {
    /// Telemetry captured by a physical hardware sensor.
    #[concordium(tag = 0)]
    Physical,
    /// Synthetic or simulated dataset declared explicitly as non-physical.
    #[concordium(tag = 1)]
    Synthetic,
}

/// Lifecycle status of a registered evidence record.
#[derive(Serialize, SchemaType, Clone, Copy, Debug, PartialEq, Eq)]
pub enum EvidenceStatus {
    Active,
    Revoked,
    Expired,
}

/// Persistent evidence commitment stored on-chain.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub struct EvidenceEntry {
    pub evidence_root: [u8; 32],
    pub source_type: SourceType,
    pub creator: Address,
    pub registered_at: Timestamp,
    pub calibration_expires_at: Timestamp,
    pub metadata_uri: String,
    pub revoked: bool,
    pub revoked_at: Option<Timestamp>,
    pub revoked_by: Option<Address>,
}

/// View returned by `get_evidence`.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub struct EvidenceView {
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

/// Parameters for `register_evidence`.
#[derive(Serialize, SchemaType, Clone, Debug, PartialEq, Eq)]
pub struct RegisterEvidenceParams {
    pub evidence_id: [u8; 32],
    pub evidence_root: [u8; 32],
    pub source_type: SourceType,
    pub calibration_expires_at: Timestamp,
    pub metadata_uri: String,
}

/// Contract state for `evidence_registry`.
#[derive(Serial, DeserialWithState)]
#[concordium(state_parameter = "S")]
pub struct State<S = StateApi> {
    pub admin: AccountAddress,
    pub evidences: StateMap<[u8; 32], EvidenceEntry, S>,
    pub total_registered: u64,
}

/// Events emitted by `evidence_registry`.
#[derive(Serialize, SchemaType, Debug, PartialEq, Eq)]
pub enum Event {
    EvidenceRegistered {
        evidence_id: [u8; 32],
        evidence_root: [u8; 32],
        source_type: SourceType,
        creator: Address,
        calibration_expires_at: Timestamp,
        metadata_uri: String,
    },
    EvidenceRevoked {
        evidence_id: [u8; 32],
        revoked_by: Address,
        revoked_at: Timestamp,
    },
    AdminUpdated {
        previous_admin: AccountAddress,
        new_admin: AccountAddress,
    },
}

/// Custom errors for `evidence_registry`.
#[derive(Debug, PartialEq, Eq, Reject, Serialize, SchemaType)]
pub enum Error {
    /// Failed parsing the parameter.
    #[from(ParseError)]
    ParseParams,
    /// Contract log is full.
    LogFull,
    /// Caller is not authorized to perform this action.
    Unauthorized,
    /// Evidence ID already exists (anti-replay protection).
    DuplicateEvidenceId,
    /// Requested evidence ID does not exist.
    EvidenceNotFound,
    /// Evidence has already been revoked.
    AlreadyRevoked,
    /// Calibration expiration timestamp is not in the future.
    CalibrationExpired,
    /// Metadata URI must not be empty.
    EmptyMetadataUri,
}

/// Initialize the Evidence Registry contract.
#[init(contract = "evidence_registry", event = "Event", error = "Error")]
fn init(ctx: &InitContext, state_builder: &mut StateBuilder) -> InitResult<State> {
    Ok(State {
        admin: ctx.init_origin(),
        evidences: state_builder.new_map(),
        total_registered: 0,
    })
}

/// Register a new evidence commitment. Rejects duplicate IDs and expired calibrations.
#[receive(
    contract = "evidence_registry",
    name = "register_evidence",
    parameter = "RegisterEvidenceParams",
    error = "Error",
    mutable,
    enable_logger
)]
fn register_evidence(
    ctx: &ReceiveContext,
    host: &mut Host<State>,
    logger: &mut Logger,
) -> Result<(), Error> {
    let params: RegisterEvidenceParams = ctx.parameter_cursor().get()?;
    let now = ctx.metadata().slot_time();

    ensure!(
        !params.metadata_uri.trim().is_empty(),
        Error::EmptyMetadataUri
    );
    ensure!(
        params.calibration_expires_at > now,
        Error::CalibrationExpired
    );

    let sender = ctx.sender();
    let state = host.state_mut();

    ensure!(
        state.evidences.get(&params.evidence_id).is_none(),
        Error::DuplicateEvidenceId
    );

    let entry = EvidenceEntry {
        evidence_root: params.evidence_root,
        source_type: params.source_type,
        creator: sender,
        registered_at: now,
        calibration_expires_at: params.calibration_expires_at,
        metadata_uri: params.metadata_uri.clone(),
        revoked: false,
        revoked_at: None,
        revoked_by: None,
    };

    let _ = state.evidences.insert(params.evidence_id, entry);
    state.total_registered = state.total_registered.saturating_add(1);

    logger
        .log(&Event::EvidenceRegistered {
            evidence_id: params.evidence_id,
            evidence_root: params.evidence_root,
            source_type: params.source_type,
            creator: sender,
            calibration_expires_at: params.calibration_expires_at,
            metadata_uri: params.metadata_uri,
        })
        .map_err(|_| Error::LogFull)?;

    Ok(())
}

/// Query evidence details, current calibration validity, and active/revoked status.
#[receive(
    contract = "evidence_registry",
    name = "get_evidence",
    parameter = "[u8; 32]",
    return_value = "EvidenceView",
    error = "Error"
)]
fn get_evidence(ctx: &ReceiveContext, host: &Host<State>) -> Result<EvidenceView, Error> {
    let evidence_id: [u8; 32] = ctx.parameter_cursor().get()?;
    let now = ctx.metadata().slot_time();

    let record = host
        .state()
        .evidences
        .get(&evidence_id)
        .ok_or(Error::EvidenceNotFound)?;

    let is_calibration_valid = now < record.calibration_expires_at;
    let is_revoked = record.revoked;
    let is_active = !is_revoked && is_calibration_valid;
    let status = if is_revoked {
        EvidenceStatus::Revoked
    } else if !is_calibration_valid {
        EvidenceStatus::Expired
    } else {
        EvidenceStatus::Active
    };

    Ok(EvidenceView {
        evidence_id,
        evidence_root: record.evidence_root,
        source_type: record.source_type,
        creator: record.creator,
        registered_at: record.registered_at,
        calibration_expires_at: record.calibration_expires_at,
        metadata_uri: record.metadata_uri.clone(),
        status,
        is_active,
        is_revoked,
        is_calibration_valid,
    })
}

/// Revoke an existing evidence record. Only the original creator or contract admin may revoke.
#[receive(
    contract = "evidence_registry",
    name = "revoke_evidence",
    parameter = "[u8; 32]",
    error = "Error",
    mutable,
    enable_logger
)]
fn revoke_evidence(
    ctx: &ReceiveContext,
    host: &mut Host<State>,
    logger: &mut Logger,
) -> Result<(), Error> {
    let evidence_id: [u8; 32] = ctx.parameter_cursor().get()?;
    let sender = ctx.sender();
    let now = ctx.metadata().slot_time();

    let admin_address = Address::Account(host.state().admin);
    let mut record = host
        .state_mut()
        .evidences
        .get_mut(&evidence_id)
        .ok_or(Error::EvidenceNotFound)?;

    ensure!(
        sender == record.creator || sender == admin_address,
        Error::Unauthorized
    );
    ensure!(!record.revoked, Error::AlreadyRevoked);

    record.revoked = true;
    record.revoked_at = Some(now);
    record.revoked_by = Some(sender);

    logger
        .log(&Event::EvidenceRevoked {
            evidence_id,
            revoked_by: sender,
            revoked_at: now,
        })
        .map_err(|_| Error::LogFull)?;

    Ok(())
}

/// Update contract admin. Callable only by current admin.
#[receive(
    contract = "evidence_registry",
    name = "update_admin",
    parameter = "AccountAddress",
    error = "Error",
    mutable,
    enable_logger
)]
fn update_admin(
    ctx: &ReceiveContext,
    host: &mut Host<State>,
    logger: &mut Logger,
) -> Result<(), Error> {
    let new_admin: AccountAddress = ctx.parameter_cursor().get()?;
    let state = host.state_mut();
    ensure!(
        ctx.sender() == Address::Account(state.admin),
        Error::Unauthorized
    );

    let previous_admin = state.admin;
    state.admin = new_admin;

    logger
        .log(&Event::AdminUpdated {
            previous_admin,
            new_admin,
        })
        .map_err(|_| Error::LogFull)?;

    Ok(())
}

/// Return the current admin account address.
#[receive(
    contract = "evidence_registry",
    name = "get_admin",
    return_value = "AccountAddress"
)]
fn get_admin(_ctx: &ReceiveContext, host: &Host<State>) -> ReceiveResult<AccountAddress> {
    Ok(host.state().admin)
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
    const CREATOR: AccountAddress = AccountAddress([2u8; 32]);
    #[cfg(target_arch = "wasm32")]
    const OTHER: AccountAddress = AccountAddress([3u8; 32]);

    #[cfg(target_arch = "wasm32")]
    fn setup_host() -> Host<State> {
        TestEnv.set_init_origin(ADMIN);
        let init_ctx = InitContext::default();
        let state_api = ExternStateApi::open();
        let mut state_builder = StateBuilder::open(state_api);
        let state = init(&init_ctx, &mut state_builder).expect("init should succeed");
        ExternHost {
            state,
            state_builder,
        }
    }

    #[cfg(target_arch = "wasm32")]
    fn make_register_params(
        evidence_id: [u8; 32],
        source_type: SourceType,
        calibration_expires_at: Timestamp,
    ) -> RegisterEvidenceParams {
        RegisterEvidenceParams {
            evidence_id,
            evidence_root: [9u8; 32],
            source_type,
            calibration_expires_at,
            metadata_uri: String::from("ipfs://bafybeiveraevidence001"),
        }
    }

    #[cfg(target_arch = "wasm32")]
    #[concordium_test]
    fn test_register_and_get_evidence_success() {
        let mut host = setup_host();
        let ctx = ReceiveContext::default();
        TestEnv
            .set_receive_sender(Address::Account(CREATOR))
            .set_slot_time(Timestamp::from_timestamp_millis(1_000));

        let evidence_id = [10u8; 32];
        let params = make_register_params(
            evidence_id,
            SourceType::Physical,
            Timestamp::from_timestamp_millis(10_000),
        );
        TestEnv.set_parameter(0, &to_bytes(&params));

        let mut logger = Logger::default();
        let res = register_evidence(&ctx, &mut host, &mut logger);
        claim!(res.is_ok(), "register_evidence should succeed");
        claim_eq!(host.state().total_registered, 1);

        TestEnv
            .set_slot_time(Timestamp::from_timestamp_millis(5_000))
            .set_parameter(0, &to_bytes(&evidence_id));

        let view = get_evidence(&ctx, &host).expect("get_evidence should succeed");
        claim_eq!(view.status, EvidenceStatus::Active);
        claim!(view.is_active, "evidence should be active");
        claim!(!view.is_revoked, "evidence should not be revoked");
        claim!(view.is_calibration_valid, "calibration should be valid");
        claim_eq!(view.source_type, SourceType::Physical);
    }

    #[cfg(target_arch = "wasm32")]
    #[concordium_test]
    fn test_register_duplicate_evidence_rejected_anti_replay() {
        let mut host = setup_host();
        let ctx = ReceiveContext::default();
        TestEnv
            .set_receive_sender(Address::Account(CREATOR))
            .set_slot_time(Timestamp::from_timestamp_millis(1_000));

        let evidence_id = [11u8; 32];
        let params = make_register_params(
            evidence_id,
            SourceType::Synthetic,
            Timestamp::from_timestamp_millis(20_000),
        );
        TestEnv.set_parameter(0, &to_bytes(&params));

        let mut logger = Logger::default();
        claim!(register_evidence(&ctx, &mut host, &mut logger).is_ok());

        // Replay with identical evidence_id must fail
        let replay = register_evidence(&ctx, &mut host, &mut logger);
        claim_eq!(replay, Err(Error::DuplicateEvidenceId));
    }

    #[cfg(target_arch = "wasm32")]
    #[concordium_test]
    fn test_calibration_validity_on_register_and_query() {
        let mut host = setup_host();
        let ctx = ReceiveContext::default();
        TestEnv
            .set_receive_sender(Address::Account(CREATOR))
            .set_slot_time(Timestamp::from_timestamp_millis(10_000));

        // 1. Registering already-expired calibration must be rejected
        let expired_id = [12u8; 32];
        let expired_params = make_register_params(
            expired_id,
            SourceType::Physical,
            Timestamp::from_timestamp_millis(10_000),
        );
        TestEnv.set_parameter(0, &to_bytes(&expired_params));
        let mut logger = Logger::default();
        claim_eq!(
            register_evidence(&ctx, &mut host, &mut logger),
            Err(Error::CalibrationExpired)
        );

        // 2. Register with future expiration, then advance clock past expiration
        let valid_id = [13u8; 32];
        let valid_params = make_register_params(
            valid_id,
            SourceType::Physical,
            Timestamp::from_timestamp_millis(15_000),
        );
        TestEnv.set_parameter(0, &to_bytes(&valid_params));
        claim!(register_evidence(&ctx, &mut host, &mut logger).is_ok());

        TestEnv
            .set_parameter(0, &to_bytes(&valid_id))
            .set_slot_time(Timestamp::from_timestamp_millis(15_000));

        let view = get_evidence(&ctx, &host).expect("get_evidence should succeed");
        claim_eq!(view.status, EvidenceStatus::Expired);
        claim!(!view.is_calibration_valid, "calibration should be expired");
        claim!(!view.is_active, "expired evidence must not be active");
        claim!(!view.is_revoked, "expired evidence is not revoked");
    }

    #[cfg(target_arch = "wasm32")]
    #[concordium_test]
    fn test_revoke_authorization_and_status() {
        let mut host = setup_host();
        let ctx = ReceiveContext::default();
        TestEnv
            .set_receive_sender(Address::Account(CREATOR))
            .set_slot_time(Timestamp::from_timestamp_millis(1_000));

        let evidence_id = [14u8; 32];
        let params = make_register_params(
            evidence_id,
            SourceType::Physical,
            Timestamp::from_timestamp_millis(50_000),
        );
        TestEnv.set_parameter(0, &to_bytes(&params));
        let mut logger = Logger::default();
        claim!(register_evidence(&ctx, &mut host, &mut logger).is_ok());

        TestEnv.set_parameter(0, &to_bytes(&evidence_id));

        // Unauthorized account cannot revoke
        TestEnv
            .set_receive_sender(Address::Account(OTHER))
            .set_slot_time(Timestamp::from_timestamp_millis(2_000));
        claim_eq!(
            revoke_evidence(&ctx, &mut host, &mut logger),
            Err(Error::Unauthorized)
        );

        // Admin can revoke
        TestEnv
            .set_receive_sender(Address::Account(ADMIN))
            .set_slot_time(Timestamp::from_timestamp_millis(3_000));
        claim!(revoke_evidence(&ctx, &mut host, &mut logger).is_ok());

        let view = get_evidence(&ctx, &host).expect("get_evidence should succeed");
        claim_eq!(view.status, EvidenceStatus::Revoked);
        claim!(view.is_revoked, "evidence should be marked revoked");
        claim!(!view.is_active, "revoked evidence must not be active");
    }
}
