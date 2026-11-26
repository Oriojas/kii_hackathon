"""Paquete central `vera_core` para VERA en Concordium.

Expone el grafo de attestations de 7 niveles, la construcción determinista del
árbol de evidencias (`evidence_root`), el motor determinista de políticas
(`PolicyEngine`), los generadores de escenarios (`get_home_air_scenario`,
`get_business_scenario`), la configuración por variables de entorno (`VeraConfig`)
y el agente explicable (`VeraAgent`).
"""

from vera_core.agent import VeraAgent, VoluntaryReport
from vera_core.attestations import (
    LEVEL_LABELS_ES,
    LEVEL_NAMES,
    AttestationGraph,
    AttestationLevel,
    AttestationNode,
    EvidenceTree,
    MerkleProofStep,
    build_evidence_tree,
    canonical_json,
    canonicalize_value,
    compute_evidence_root,
    compute_policy_hash,
    sha256_hex,
    verify_merkle_proof,
)
from vera_core.config import VeraConfig
from vera_core.policy_engine import (
    STATUS_BLOCKED,
    STATUS_ELIGIBLE,
    STATUS_INCOMPLETE,
    STATUS_TAMPERED,
    PolicyCheckResult,
    PolicyEngine,
    PolicyEvaluationResult,
)
from vera_core.scenarios import (
    ScenarioBundle,
    get_business_scenario,
    get_home_air_scenario,
)

__all__ = [
    "AttestationGraph",
    "AttestationLevel",
    "AttestationNode",
    "EvidenceTree",
    "LEVEL_LABELS_ES",
    "LEVEL_NAMES",
    "MerkleProofStep",
    "PolicyCheckResult",
    "PolicyEngine",
    "PolicyEvaluationResult",
    "STATUS_BLOCKED",
    "STATUS_ELIGIBLE",
    "STATUS_INCOMPLETE",
    "STATUS_TAMPERED",
    "ScenarioBundle",
    "VeraAgent",
    "VeraConfig",
    "VoluntaryReport",
    "build_evidence_tree",
    "canonical_json",
    "canonicalize_value",
    "compute_evidence_root",
    "compute_policy_hash",
    "get_business_scenario",
    "get_home_air_scenario",
    "sha256_hex",
    "verify_merkle_proof",
]
