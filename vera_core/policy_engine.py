"""Motor de Políticas determinista (`PolicyEngine`) para VERA Core.

Evalúa de forma auditable y reproducible la evidencia contenida en un
`AttestationGraph` (niveles 1 a 5), verificando:
1. Integridad criptográfica (`content_hash`, `batch_hash` y `evidence_root`).
2. Cobertura mínima de lecturas y completitud de niveles/zonas requeridas.
3. Vigencia de calibración del dispositivo y protección contra replay.
4. Cumplimiento de umbrales ambientales (recuperación ante anomalías en Home Air
   o intensidad Scope 2 y reducción mínima en Business).

Retorna un resultado auditable con estado en `{'ELIGIBLE', 'INCOMPLETE',
'TAMPERED', 'BLOCKED'}`, `reason`, `policy_hash` y `payout_eligible`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from vera_core.attestations import (
    AttestationGraph,
    AttestationLevel,
    AttestationNode,
    compute_policy_hash,
)
from vera_core.config import VeraConfig

STATUS_ELIGIBLE = "ELIGIBLE"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_TAMPERED = "TAMPERED"
STATUS_BLOCKED = "BLOCKED"

VALID_POLICY_STATUSES = frozenset({
    STATUS_ELIGIBLE,
    STATUS_INCOMPLETE,
    STATUS_TAMPERED,
    STATUS_BLOCKED,
})

VALID_SENSOR_STATUSES = frozenset({"VALID", "SYNTHETIC_DECLARED"})


def _parse_iso_utc(value: str) -> datetime:
    """Convierte una cadena ISO-8601 a `datetime` UTC con zona horaria."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class PolicyCheckResult:
    """Resultado individual de una regla evaluada por el `PolicyEngine`."""

    name: str
    passed: bool
    observed: Any
    expected: Any
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el check a diccionario."""
        return {
            "name": self.name,
            "passed": self.passed,
            "observed": self.observed,
            "expected": self.expected,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PolicyEvaluationResult:
    """Veredicto determinista y auditable emitido por `PolicyEngine`."""

    status: str
    reason: str
    policy_hash: str
    payout_eligible: bool
    evidence_root: str
    scenario_type: str
    source_classification: str
    evaluated_at: str
    checks: List[PolicyCheckResult] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in VALID_POLICY_STATUSES:
            raise ValueError(f"Estado de política inválido: {self.status!r}")
        if self.payout_eligible and self.status != STATUS_ELIGIBLE:
            raise ValueError("payout_eligible solo puede ser True cuando status == 'ELIGIBLE'.")

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el resultado completo de evaluación."""
        return {
            "status": self.status,
            "reason": self.reason,
            "policy_hash": self.policy_hash,
            "payout_eligible": self.payout_eligible,
            "evidence_root": self.evidence_root,
            "scenario_type": self.scenario_type,
            "source_classification": self.source_classification,
            "evaluated_at": self.evaluated_at,
            "checks": [c.to_dict() for c in self.checks],
            "metrics": dict(self.metrics),
        }


class PolicyEngine:
    """Evaluador determinista de políticas ambientales y elegibilidad de liquidación."""

    def __init__(
        self,
        config: Optional[VeraConfig] = None,
        settled_idempotency_keys: Optional[Sequence[str]] = None,
    ) -> None:
        self.config = config or VeraConfig.from_env()
        self.settled_idempotency_keys: Set[str] = set(settled_idempotency_keys or ())

    def get_policy_parameters(self, scenario_type: str) -> Dict[str, Any]:
        """Retorna el conjunto canónico de parámetros que definen la política activa."""
        base_params: Dict[str, Any] = {
            "policy_version": self.config.policy_version,
            "scenario_type": scenario_type,
            "min_coverage_ratio": self.config.min_coverage_ratio,
            "max_calibration_age_hours": self.config.max_calibration_age_hours,
        }
        if scenario_type == "home_air":
            base_params.update({
                "expected_batch_readings": self.config.expected_batch_readings,
                "require_recovery_for_anomaly": self.config.require_recovery_for_anomaly,
                "baseline_max_raw": self.config.home_air_baseline_max_raw,
                "anomaly_min_raw": self.config.home_air_anomaly_min_raw,
                "recovery_max_raw": self.config.home_air_recovery_max_raw,
            })
        elif scenario_type == "business":
            base_params.update({
                "required_zones": list(self.config.business_zones),
                "grid_emission_factor_kg_per_kwh": self.config.business_grid_emission_factor_kg_per_kwh,
                "max_intensity_kg_per_unit": self.config.business_max_intensity_kg_per_unit,
                "min_reduction_pct": self.config.business_min_reduction_pct,
            })
        elif scenario_type == "imported_gps":
            base_params.update({
                "source_classification": "imported",
                "require_recovery_for_anomaly": self.config.require_recovery_for_anomaly,
                "baseline_max_raw": self.config.home_air_baseline_max_raw,
                "anomaly_min_raw": self.config.home_air_anomaly_min_raw,
                "recovery_max_raw": self.config.home_air_recovery_max_raw,
            })
        elif scenario_type == "live_telemetry":
            base_params.update({
                "source_classification": "physical",
                "min_live_readings": 10,
                "require_recovery_for_anomaly": self.config.require_recovery_for_anomaly,
                "baseline_max_raw": self.config.home_air_baseline_max_raw,
                "anomaly_min_raw": self.config.home_air_anomaly_min_raw,
                "recovery_max_raw": self.config.home_air_recovery_max_raw,
            })
        else:
            raise ValueError(f"Escenario no soportado por PolicyEngine: {scenario_type!r}")
        return base_params

    def compute_policy_hash(self, scenario_type: str) -> str:
        """Calcula el `policy_hash` determinista para el escenario indicado."""
        return compute_policy_hash(self.get_policy_parameters(scenario_type))

    def _infer_scenario_type(self, graph: AttestationGraph, explicit: Optional[str]) -> str:
        if explicit:
            return explicit
        dataset_nodes = graph.get_nodes_by_level(AttestationLevel.DATASET)
        if dataset_nodes and "zones_summary" in dataset_nodes[0].payload:
            return "business"
        source_nodes = graph.get_nodes_by_level(AttestationLevel.DEVICE_SOURCE)
        if source_nodes and source_nodes[0].source_classification == "imported":
            return "imported_gps"
        return "home_air"

    def _collect_source_classification(self, graph: AttestationGraph) -> str:
        source_nodes = graph.get_nodes_by_level(AttestationLevel.DEVICE_SOURCE)
        if source_nodes:
            return source_nodes[0].source_classification
        if graph.nodes:
            first_node = next(iter(graph.nodes.values()))
            return first_node.source_classification
        return "synthetic_declared"

    def _check_integrity(
        self,
        graph: AttestationGraph,
        computed_root: str,
        expected_evidence_root: Optional[str],
    ) -> List[PolicyCheckResult]:
        checks: List[PolicyCheckResult] = []
        graph_ok, errors = graph.verify_integrity()
        checks.append(
            PolicyCheckResult(
                name="graph_hash_integrity",
                passed=graph_ok,
                observed="valid" if graph_ok else errors,
                expected="valid",
                detail=(
                    "Todos los content_hash y batch_hash del grafo coinciden."
                    if graph_ok
                    else "; ".join(errors)
                ),
            )
        )

        if expected_evidence_root is not None:
            root_match = computed_root == expected_evidence_root
            checks.append(
                PolicyCheckResult(
                    name="evidence_root_match",
                    passed=root_match,
                    observed=computed_root,
                    expected=expected_evidence_root,
                    detail=(
                        "El evidence_root calculado coincide con el anclado."
                        if root_match
                        else "El evidence_root calculado no coincide con el esperado (posible alteración)."
                    ),
                )
            )

        # Verifica coherencia entre número de lecturas del lote y valores declarados en dataset
        batch_nodes = graph.get_nodes_by_level(AttestationLevel.BATCH)
        dataset_nodes = graph.get_nodes_by_level(AttestationLevel.DATASET)
        if batch_nodes and dataset_nodes:
            total_batch_readings = sum(len(b.payload.get("readings", [])) for b in batch_nodes)
            declared_count = int(dataset_nodes[0].payload.get("reading_count", -1))
            count_match = total_batch_readings == declared_count
            checks.append(
                PolicyCheckResult(
                    name="batch_dataset_consistency",
                    passed=count_match,
                    observed=declared_count,
                    expected=total_batch_readings,
                    detail=(
                        "El conteo del dataset coincide con las lecturas del lote."
                        if count_match
                        else "Inconsistencia entre las lecturas del lote y el conteo agregado en dataset."
                    ),
                )
            )
        return checks

    def _check_completeness(
        self,
        graph: AttestationGraph,
        scenario_type: str,
    ) -> List[PolicyCheckResult]:
        checks: List[PolicyCheckResult] = []
        required_levels = (
            AttestationLevel.DEVICE_SOURCE,
            AttestationLevel.CALIBRATION,
            AttestationLevel.BATCH,
            AttestationLevel.DATASET,
            AttestationLevel.ANALYSIS,
        )
        missing_levels = [lvl.name for lvl in required_levels if not graph.get_nodes_by_level(lvl)]
        checks.append(
            PolicyCheckResult(
                name="required_attestation_levels",
                passed=len(missing_levels) == 0,
                observed=[lvl.name for lvl in required_levels if graph.get_nodes_by_level(lvl)],
                expected=[lvl.name for lvl in required_levels],
                detail=(
                    "Todos los niveles de evidencia (1-5) están presentes."
                    if not missing_levels
                    else f"Faltan niveles requeridos en el grafo: {', '.join(missing_levels)}."
                ),
            )
        )
        if missing_levels:
            return checks

        batch_node = graph.get_nodes_by_level(AttestationLevel.BATCH)[0]
        dataset_node = graph.get_nodes_by_level(AttestationLevel.DATASET)[0]

        received = len(batch_node.payload.get("readings", []))
        expected_count = int(dataset_node.payload.get("expected_count", batch_node.payload.get("expected_count", 0)))
        coverage = (received / expected_count) if expected_count > 0 else 0.0
        coverage_ok = received > 0 and coverage >= self.config.min_coverage_ratio

        checks.append(
            PolicyCheckResult(
                name="minimum_reading_coverage",
                passed=coverage_ok,
                observed=round(coverage, 4),
                expected=self.config.min_coverage_ratio,
                detail=(
                    f"Cobertura suficiente: {received}/{expected_count} lecturas ({coverage:.1%})."
                    if coverage_ok
                    else f"Cobertura insuficiente: {received}/{expected_count} lecturas ({coverage:.1%}), "
                         f"mínimo requerido {self.config.min_coverage_ratio:.1%}."
                ),
            )
        )

        if scenario_type == "business":
            zones_summary = dataset_node.payload.get("zones_summary", [])
            present_zones = {str(z.get("zone_name")) for z in zones_summary}
            required_zones = set(self.config.business_zones)
            missing_zones = sorted(required_zones - present_zones)
            checks.append(
                PolicyCheckResult(
                    name="business_zones_completeness",
                    passed=len(missing_zones) == 0,
                    observed=sorted(present_zones),
                    expected=sorted(required_zones),
                    detail=(
                        "Las cuatro zonas operativas requeridas reportaron datos."
                        if not missing_zones
                        else f"Faltan zonas obligatorias en el dataset: {', '.join(missing_zones)}."
                    ),
                )
            )
        return checks

    def _check_calibration_and_replay(
        self,
        graph: AttestationGraph,
        evaluation_timestamp: str,
        idempotency_key: Optional[str],
        already_settled: bool,
    ) -> List[PolicyCheckResult]:
        checks: List[PolicyCheckResult] = []
        cal_node = graph.get_nodes_by_level(AttestationLevel.CALIBRATION)[0]
        cal_payload = cal_node.payload

        eval_dt = _parse_iso_utc(evaluation_timestamp)
        expires_at_str = str(cal_payload.get("calibration_expires_at", ""))
        calibrated_at_str = str(cal_payload.get("calibrated_at", ""))
        sensor_status = str(cal_payload.get("sensor_status", "")).upper()

        expires_dt = _parse_iso_utc(expires_at_str)
        calibrated_dt = _parse_iso_utc(calibrated_at_str)

        not_expired = eval_dt <= expires_dt
        age_hours = (eval_dt - calibrated_dt).total_seconds() / 3600.0
        age_ok = 0.0 <= age_hours <= float(self.config.max_calibration_age_hours)
        status_ok = sensor_status in VALID_SENSOR_STATUSES
        calibration_valid = not_expired and age_ok and status_ok

        if not not_expired:
            detail = (
                f"Calibración expirada: evaluación {evaluation_timestamp} > "
                f"vigencia {expires_at_str}."
            )
        elif not status_ok:
            detail = f"Estado del sensor no apto para liquidación: {sensor_status}."
        elif not age_ok:
            detail = (
                f"Antigüedad de calibración ({age_hours:.1f} h) excede el máximo "
                f"permitido ({self.config.max_calibration_age_hours} h)."
            )
        else:
            detail = f"Calibración vigente hasta {expires_at_str} con estado {sensor_status}."

        checks.append(
            PolicyCheckResult(
                name="calibration_validity",
                passed=calibration_valid,
                observed={
                    "evaluated_at": evaluation_timestamp,
                    "calibration_expires_at": expires_at_str,
                    "sensor_status": sensor_status,
                },
                expected={
                    "max_timestamp": expires_at_str,
                    "valid_statuses": sorted(VALID_SENSOR_STATUSES),
                },
                detail=detail,
            )
        )

        replay_detected = already_settled or (
            idempotency_key is not None and idempotency_key in self.settled_idempotency_keys
        )
        checks.append(
            PolicyCheckResult(
                name="anti_replay_protection",
                passed=not replay_detected,
                observed={"idempotency_key": idempotency_key, "already_settled": replay_detected},
                expected={"already_settled": False},
                detail=(
                    "Liquidación única verificada (sin replay previo)."
                    if not replay_detected
                    else "Replay detectado: este lote/evidencia ya fue liquidado previamente."
                ),
            )
        )
        return checks

    def _check_environmental_thresholds(
        self,
        graph: AttestationGraph,
        scenario_type: str,
    ) -> Tuple[List[PolicyCheckResult], Dict[str, Any]]:
        analysis_node = graph.get_nodes_by_level(AttestationLevel.ANALYSIS)[0]
        dataset_node = graph.get_nodes_by_level(AttestationLevel.DATASET)[0]
        ap = analysis_node.payload
        dp = dataset_node.payload

        checks: List[PolicyCheckResult] = []
        metrics: Dict[str, Any] = {
            "reading_count": dp.get("reading_count"),
            "coverage_ratio": dp.get("coverage_ratio"),
            "baseline_value": ap.get("baseline_value"),
            "peak_value": ap.get("peak_value"),
            "recovery_value": ap.get("recovery_value"),
            "anomaly_detected": ap.get("anomaly_detected"),
            "anomaly_mitigated": ap.get("anomaly_mitigated"),
            "improvement_pct": ap.get("improvement_pct"),
        }

        if scenario_type in {"home_air", "imported_gps", "live_telemetry"}:
            peak = float(ap.get("peak_value", 0.0))
            recovery = float(ap.get("recovery_value", 0.0))
            improvement = float(ap.get("improvement_pct", 0.0))
            anomaly_detected = bool(ap.get("anomaly_detected", False))
            anomaly_mitigated = bool(ap.get("anomaly_mitigated", False))

            if anomaly_detected and self.config.require_recovery_for_anomaly:
                threshold_ok = (
                    anomaly_mitigated
                    and recovery <= self.config.home_air_recovery_max_raw
                    and improvement > 0.0
                )
                detail = (
                    f"Anomalía (pico {peak:.1f}) atendida y recuperada a {recovery:.1f} "
                    f"(umbral <= {self.config.home_air_recovery_max_raw:.1f}, mejora {improvement:.1f}%)."
                    if threshold_ok
                    else f"Anomalía detectada (pico {peak:.1f}) sin recuperación suficiente "
                         f"(valor final {recovery:.1f} > máximo {self.config.home_air_recovery_max_raw:.1f})."
                )
            else:
                mean_val = float(dp.get("mean_value", 0.0))
                threshold_ok = mean_val <= self.config.home_air_baseline_max_raw
                detail = (
                    f"Señal promedio {mean_val:.1f} dentro de línea base saludable "
                    f"(<= {self.config.home_air_baseline_max_raw:.1f})."
                    if threshold_ok
                    else f"Señal promedio {mean_val:.1f} supera umbral base {self.config.home_air_baseline_max_raw:.1f}."
                )

            checks.append(
                PolicyCheckResult(
                    name="home_air_environmental_threshold",
                    passed=threshold_ok,
                    observed={"peak": peak, "recovery": recovery, "improvement_pct": improvement},
                    expected={"recovery_max_raw": self.config.home_air_recovery_max_raw},
                    detail=detail,
                )
            )
            return checks, metrics

        # Escenario Business
        intensity = float(ap.get("intensity_kg_per_unit", 0.0))
        reduction_pct = float(ap.get("improvement_pct", 0.0))
        total_kwh = float(ap.get("total_kwh", 0.0))
        total_scope2 = float(ap.get("scope2_total_kg_co2e", 0.0))
        total_units = float(ap.get("total_units", 0.0))

        metrics.update({
            "total_kwh": total_kwh,
            "scope2_total_kg_co2e": total_scope2,
            "total_units": total_units,
            "intensity_kg_per_unit": intensity,
            "zones_summary": dp.get("zones_summary", []),
        })

        intensity_ok = intensity <= self.config.business_max_intensity_kg_per_unit
        checks.append(
            PolicyCheckResult(
                name="business_carbon_intensity_threshold",
                passed=intensity_ok,
                observed=intensity,
                expected=self.config.business_max_intensity_kg_per_unit,
                detail=(
                    f"Intensidad Scope 2 ({intensity:.4f} kg CO2e/unidad) dentro del límite "
                    f"(<= {self.config.business_max_intensity_kg_per_unit:.4f})."
                    if intensity_ok
                    else f"Intensidad Scope 2 ({intensity:.4f} kg CO2e/unidad) excede el límite "
                         f"({self.config.business_max_intensity_kg_per_unit:.4f})."
                ),
            )
        )

        reduction_ok = reduction_pct >= self.config.business_min_reduction_pct
        checks.append(
            PolicyCheckResult(
                name="business_scope2_reduction_threshold",
                passed=reduction_ok,
                observed=reduction_pct,
                expected=self.config.business_min_reduction_pct,
                detail=(
                    f"Reducción Scope 2 de {reduction_pct:.2f}% cumple mínimo "
                    f"(>= {self.config.business_min_reduction_pct:.2f}%)."
                    if reduction_ok
                    else f"Reducción Scope 2 de {reduction_pct:.2f}% es inferior al mínimo "
                         f"({self.config.business_min_reduction_pct:.2f}%)."
                ),
            )
        )
        return checks, metrics

    def evaluate(
        self,
        graph: AttestationGraph,
        *,
        scenario_type: Optional[str] = None,
        evaluation_timestamp: Optional[str] = None,
        expected_evidence_root: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        already_settled: bool = False,
        record_in_graph: bool = False,
        tx_hash: Optional[str] = None,
    ) -> PolicyEvaluationResult:
        """Evalúa deterministamente un `AttestationGraph` y opcionalmente añade niveles 6 y 7."""
        resolved_scenario = self._infer_scenario_type(graph, scenario_type)
        policy_hash = self.compute_policy_hash(resolved_scenario)
        source_classification = self._collect_source_classification(graph)
        eval_ts = evaluation_timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        evidence_tree = graph.build_evidence_tree(max_level=AttestationLevel.ANALYSIS)
        computed_root = evidence_tree.evidence_root

        all_checks: List[PolicyCheckResult] = []

        # 1. Integridad criptográfica -> TAMPERED
        integrity_checks = self._check_integrity(graph, computed_root, expected_evidence_root)
        all_checks.extend(integrity_checks)
        failed_integrity = [c for c in integrity_checks if not c.passed]
        if failed_integrity:
            result = PolicyEvaluationResult(
                status=STATUS_TAMPERED,
                reason=failed_integrity[0].detail,
                policy_hash=policy_hash,
                payout_eligible=False,
                evidence_root=computed_root,
                scenario_type=resolved_scenario,
                source_classification=source_classification,
                evaluated_at=eval_ts,
                checks=all_checks,
                metrics={},
            )
            if record_in_graph:
                self._attach_decision_nodes(graph, result, idempotency_key, tx_hash)
            return result

        # 2. Completitud y cobertura -> INCOMPLETE
        completeness_checks = self._check_completeness(graph, resolved_scenario)
        all_checks.extend(completeness_checks)
        failed_completeness = [c for c in completeness_checks if not c.passed]
        if failed_completeness:
            result = PolicyEvaluationResult(
                status=STATUS_INCOMPLETE,
                reason=failed_completeness[0].detail,
                policy_hash=policy_hash,
                payout_eligible=False,
                evidence_root=computed_root,
                scenario_type=resolved_scenario,
                source_classification=source_classification,
                evaluated_at=eval_ts,
                checks=all_checks,
                metrics={},
            )
            if record_in_graph:
                self._attach_decision_nodes(graph, result, idempotency_key, tx_hash)
            return result

        # 3. Vigencia de calibración y anti-replay -> BLOCKED
        cal_checks = self._check_calibration_and_replay(
            graph=graph,
            evaluation_timestamp=eval_ts,
            idempotency_key=idempotency_key,
            already_settled=already_settled,
        )
        all_checks.extend(cal_checks)
        failed_cal = [c for c in cal_checks if not c.passed]

        # 4. Umbrales ambientales -> BLOCKED
        env_checks, metrics = self._check_environmental_thresholds(graph, resolved_scenario)
        all_checks.extend(env_checks)
        failed_env = [c for c in env_checks if not c.passed]

        if failed_cal or failed_env:
            first_failure = (failed_cal + failed_env)[0]
            result = PolicyEvaluationResult(
                status=STATUS_BLOCKED,
                reason=first_failure.detail,
                policy_hash=policy_hash,
                payout_eligible=False,
                evidence_root=computed_root,
                scenario_type=resolved_scenario,
                source_classification=source_classification,
                evaluated_at=eval_ts,
                checks=all_checks,
                metrics=metrics,
            )
            if record_in_graph:
                self._attach_decision_nodes(graph, result, idempotency_key, tx_hash)
            return result

        # 5. Caso válido -> ELIGIBLE
        result = PolicyEvaluationResult(
            status=STATUS_ELIGIBLE,
            reason=(
                "Evidencia íntegra, cobertura completa, calibración vigente y "
                "umbrales ambientales cumplidos."
            ),
            policy_hash=policy_hash,
            payout_eligible=True,
            evidence_root=computed_root,
            scenario_type=resolved_scenario,
            source_classification=source_classification,
            evaluated_at=eval_ts,
            checks=all_checks,
            metrics=metrics,
        )
        if record_in_graph:
            self._attach_decision_nodes(graph, result, idempotency_key, tx_hash)
        return result

    def _attach_decision_nodes(
        self,
        graph: AttestationGraph,
        evaluation: PolicyEvaluationResult,
        idempotency_key: Optional[str],
        tx_hash: Optional[str],
    ) -> None:
        """Registra los niveles 6 (Política) y 7 (Liquidación) en el grafo."""
        analysis_nodes = graph.get_nodes_by_level(AttestationLevel.ANALYSIS)
        if analysis_nodes:
            parent_ids = [n.node_id for n in analysis_nodes]
        else:
            max_lvl = max((n.level for n in graph.nodes.values()), default=1)
            parent_ids = [n.node_id for n in graph.get_nodes_by_level(max_lvl)]

        policy_node_id = f"{graph.graph_id}-policy-eval"
        if policy_node_id not in graph.nodes:
            graph.register_policy_evaluation(
                node_id=policy_node_id,
                parent_ids=parent_ids,
                source_classification=evaluation.source_classification,
                timestamp=evaluation.evaluated_at,
                policy_version=self.config.policy_version,
                policy_hash=evaluation.policy_hash,
                status=evaluation.status,
                reason=evaluation.reason,
                payout_eligible=evaluation.payout_eligible,
                evidence_root=evaluation.evidence_root,
                checks=[c.to_dict() for c in evaluation.checks],
            )

        settlement_node_id = f"{graph.graph_id}-settlement"
        reward_amount = (
            self.config.business_reward_amount_micro_ccd
            if evaluation.scenario_type == "business"
            else self.config.home_reward_amount_micro_ccd
        )
        resolved_key = idempotency_key or f"{graph.graph_id}:{evaluation.evidence_root[:16]}"
        if settlement_node_id not in graph.nodes:
            graph.register_settlement(
                node_id=settlement_node_id,
                parent_ids=[policy_node_id],
                source_classification=evaluation.source_classification,
                timestamp=evaluation.evaluated_at,
                action="REWARD" if evaluation.payout_eligible else "REJECT",
                payout_eligible=evaluation.payout_eligible,
                amount_micro_ccd=reward_amount,
                recipient_account=self.config.concordium_agent_account,
                contract_index=self.config.reward_pool_index,
                contract_subindex=self.config.reward_pool_subindex,
                idempotency_key=resolved_key,
                tx_hash=tx_hash,
                rejection_reason=None if evaluation.payout_eligible else evaluation.reason,
            )
            if evaluation.payout_eligible:
                self.settled_idempotency_keys.add(resolved_key)
