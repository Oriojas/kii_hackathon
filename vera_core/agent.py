"""Agente VERA (`VeraAgent`) para explicabilidad, mitigación y reportes voluntarios.

El agente NO toma decisiones de pago arbitrarias: las reglas deterministas del
`PolicyEngine` y los contratos inteligentes conservan el control. `VeraAgent`
traduce esas decisiones a lenguaje natural en español, propone acciones de
mitigación accionables y genera reportes voluntarios estructurados con alcance
honesto y trazabilidad completa sobre el Grafo de Attestations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from vera_core.attestations import LEVEL_LABELS_ES, AttestationGraph, AttestationLevel
from vera_core.config import VeraConfig
from vera_core.policy_engine import (
    STATUS_BLOCKED,
    STATUS_ELIGIBLE,
    STATUS_INCOMPLETE,
    STATUS_TAMPERED,
    PolicyEvaluationResult,
)


@dataclass
class VoluntaryReport:
    """Reporte ambiental voluntario estructurado con trazabilidad verificable."""

    report_id: str
    generated_at: str
    agent_id: str
    scenario_type: str
    source_classification: str
    is_synthetic: bool
    policy_status: str
    payout_eligible: bool
    policy_hash: str
    evidence_root: str
    executive_summary_es: str
    honest_scope_notice_es: str
    metrics: Dict[str, Any]
    mitigation_actions: List[str]
    lineage: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el reporte voluntario a diccionario."""
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "agent_id": self.agent_id,
            "scenario_type": self.scenario_type,
            "source_classification": self.source_classification,
            "is_synthetic": self.is_synthetic,
            "policy_status": self.policy_status,
            "payout_eligible": self.payout_eligible,
            "policy_hash": self.policy_hash,
            "evidence_root": self.evidence_root,
            "executive_summary_es": self.executive_summary_es,
            "honest_scope_notice_es": self.honest_scope_notice_es,
            "metrics": dict(self.metrics),
            "mitigation_actions": list(self.mitigation_actions),
            "lineage": list(self.lineage),
            "checks": list(self.checks),
        }

    def to_markdown(self) -> str:
        """Renderiza el reporte voluntario en formato Markdown legible en español."""
        lines = [
            f"# Reporte Voluntario VERA ({self.report_id})",
            "",
            f"- **Agente:** `{self.agent_id}`",
            f"- **Fecha de evaluación:** `{self.generated_at}`",
            f"- **Escenario:** `{self.scenario_type}`",
            f"- **Origen de datos:** `{self.source_classification}` "
            f"({'Sintético declarado' if self.is_synthetic else 'Medición física / hardware'})",
            f"- **Estado Policy Engine:** `{self.policy_status}` (Pago elegible: `{'Sí' if self.payout_eligible else 'No'}`)",
            f"- **Policy Hash:** `{self.policy_hash}`",
            f"- **Evidence Root:** `{self.evidence_root}`",
            "",
            "## Resumen Ejecutivo",
            self.executive_summary_es,
            "",
            "## Alcance Honesto",
            f"> {self.honest_scope_notice_es}",
            "",
            "## Verificaciones Deterministas",
        ]
        for chk in self.checks:
            mark = "APROBADO" if chk.get("passed") else "FALLIDO"
            lines.append(f"- **[{mark}] `{chk.get('name')}`**: {chk.get('detail')}")

        zones = self.metrics.get("zones_summary")
        if zones:
            lines.extend([
                "",
                "## Desglose por Zonas (Scope 2 e Intensidad)",
                "| Zona | Energía (kWh) | Scope 2 (kg CO2e) | Unidades | Intensidad (kg/unidad) | Reducción (%) |",
                "|---|---:|---:|---:|---:|---:|",
            ])
            for z in zones:
                lines.append(
                    f"| {z['zone_name']} | {z['kwh']:.2f} | {z['scope2_emissions_kg']:.2f} | "
                    f"{z['units_produced']:.0f} | {z['intensity_kg_per_unit']:.4f} | {z['reduction_pct']:.2f}% |"
                )

        lines.extend(["", "## Acciones de Mitigación Recomendadas"])
        for action in self.mitigation_actions:
            lines.append(f"- {action}")

        if self.lineage:
            lines.extend(["", "## Trazabilidad (Grafo de Attestations)"])
            for node in self.lineage:
                lines.append(
                    f"- **Nivel {node['level']} ({node['level_label_es']})** — "
                    f"`{node['node_id']}` | hash `{node['content_hash'][:16]}...`"
                )

        return "\n".join(lines)


class VeraAgent:
    """Agente explicable de VERA para interpretación, mitigación y reporte voluntario."""

    def __init__(self, config: Optional[VeraConfig] = None) -> None:
        self.config = config or VeraConfig.from_env()

    def _honest_scope_notice(
        self,
        scenario_type: str,
        source_classification: str,
    ) -> str:
        if source_classification == "synthetic_declared":
            return (
                "Este reporte utiliza datos sintéticos declarados bajo reglas del protocolo VERA. "
                "Una attestation verifica autoría e integridad del contenido, pero no convierte datos "
                "sintéticos en mediciones físicas ni sustituye auditorías o verificadores acreditados."
            )
        if scenario_type == "home_air":
            return (
                "Las lecturas provienen de un sensor MQ-135 reportadas como señal analógica cruda (ADC) "
                "para detectar anomalías y recuperación relativa; no constituyen mediciones certificadas "
                "de CO2 ni reemplazan certificaciones regulatorias."
            )
        return (
            "Reporte voluntario emitido bajo las reglas deterministas del protocolo VERA; no sustituye "
            "auditorías externas ni créditos de carbono regulados."
        )

    def explain_decision(
        self,
        evaluation: PolicyEvaluationResult,
        graph: Optional[AttestationGraph] = None,
    ) -> str:
        """Explica en lenguaje natural (español) la decisión emitida por el `PolicyEngine`."""
        etiqueta_origen = (
            "datos sintéticos declarados"
            if evaluation.source_classification == "synthetic_declared"
            else "sensor físico"
        )
        escenario_es = (
            "calidad de aire en hogar (MQ-135 señal cruda)"
            if evaluation.scenario_type == "home_air"
            else "operación empresarial multi-zona (Scope 2)"
        )

        status_intro = {
            STATUS_ELIGIBLE: (
                f"Decisión: ELEGIBLE para liquidación en Concordium ({self.config.concordium_network})."
            ),
            STATUS_INCOMPLETE: (
                "Decisión: INCOMPLETO. La evidencia no cumple el mínimo requerido y no se autoriza pago."
            ),
            STATUS_TAMPERED: (
                "Decisión: MANIPULADO (TAMPERED). Se detectó alteración criptográfica en la evidencia; "
                "la liquidación queda rechazada."
            ),
            STATUS_BLOCKED: (
                "Decisión: BLOQUEADO. La evidencia es íntegra pero incumple una condición de vigencia, "
                "anti-replay o umbral ambiental."
            ),
        }[evaluation.status]

        detalles: List[str] = [
            status_intro,
            f"Escenario evaluado: {escenario_es} con origen '{etiqueta_origen}'.",
            f"Motivo determinante: {evaluation.reason}",
            f"Huella verificable: policy_hash={evaluation.policy_hash[:16]}..., "
            f"evidence_root={evaluation.evidence_root[:16]}...",
        ]

        if evaluation.scenario_type == "home_air" and evaluation.metrics:
            peak = evaluation.metrics.get("peak_value")
            recovery = evaluation.metrics.get("recovery_value")
            baseline = evaluation.metrics.get("baseline_value")
            if peak is not None and recovery is not None:
                detalles.append(
                    f"Comportamiento observado (ADC crudo): línea base {baseline}, "
                    f"pico de anomalía {peak}, recuperación final {recovery}."
                )
        elif evaluation.scenario_type == "business" and evaluation.metrics:
            total_kwh = evaluation.metrics.get("total_kwh")
            scope2 = evaluation.metrics.get("scope2_total_kg_co2e")
            intensity = evaluation.metrics.get("intensity_kg_per_unit")
            reduction = evaluation.metrics.get("improvement_pct")
            if total_kwh is not None:
                detalles.append(
                    f"Desempeño agregado: {total_kwh} kWh consumidos, {scope2} kg CO2e Scope 2, "
                    f"intensidad {intensity} kg/unidad y variación de {reduction}% frente a línea base."
                )

        if graph is not None:
            levels_present = sorted({n.level for n in graph.nodes.values()})
            labels = [LEVEL_LABELS_ES[lvl] for lvl in levels_present if lvl in LEVEL_LABELS_ES]
            detalles.append(f"Niveles trazados en el grafo de attestations: {', '.join(labels)}.")

        return " ".join(detalles)

    def suggest_mitigations(
        self,
        evaluation: PolicyEvaluationResult,
        graph: Optional[AttestationGraph] = None,
    ) -> List[str]:
        """Sugiere acciones concretas de mitigación y remediación operativa."""
        actions: List[str] = []

        if evaluation.status == STATUS_TAMPERED:
            actions.extend([
                "Aislar el lote rechazado, revocar el compromiso de evidencia en el Evidence Registry y "
                "regenerar las lecturas firmadas desde el dispositivo o fuente original.",
                "Auditar la cadena de custodia entre el recolector local y el bridge JS/TS para detectar "
                "modificaciones en tránsito.",
            ])
            return actions

        if evaluation.status == STATUS_INCOMPLETE:
            actions.append(
                f"Completar la captura de datos hasta superar el umbral mínimo de cobertura "
                f"({self.config.min_coverage_ratio:.0%}) y asegurar que existan los niveles 1 a 5 del grafo."
            )
            if evaluation.scenario_type == "business":
                actions.append(
                    f"Verificar que las cuatro zonas configuradas ({', '.join(self.config.business_zones)}) "
                    "emitan su lote antes del cierre del periodo."
                )
            return actions

        if evaluation.status == STATUS_BLOCKED:
            failed_names = {c.name for c in evaluation.checks if not c.passed}
            if "calibration_validity" in failed_names:
                actions.append(
                    "Recalibrar el sensor en aire limpio de referencia y emitir una nueva attestation de "
                    "calibración (Nivel 2) con fecha de vigencia actualizada antes de reenviar lotes."
                )
            if "anti_replay_protection" in failed_names:
                actions.append(
                    "Evitar el reenvío de lotes ya liquidados; generar un nuevo ciclo de medición con "
                    "idempotency_key único."
                )

        if evaluation.scenario_type == "home_air":
            if evaluation.status == STATUS_BLOCKED and evaluation.metrics.get("anomaly_detected"):
                actions.extend([
                    "Activar ventilación cruzada o extracción mecánica inmediata hasta reducir la señal "
                    f"cruda por debajo de {self.config.home_air_recovery_max_raw:.0f} ADC.",
                    "Inspeccionar posibles fuentes internas de compuestos orgánicos volátiles o humo "
                    "cercanas al sensor MQ-135.",
                ])
            elif evaluation.status == STATUS_ELIGIBLE:
                actions.extend([
                    "Mantener la pauta de ventilación preventiva aplicada tras el pico de anomalía para "
                    "conservar la señal dentro de la línea base.",
                    "Programar la próxima verificación de vigencia del sensor MQ-135 antes de superar "
                    f"las {self.config.max_calibration_age_hours} horas de calibración.",
                ])
            return actions

        # Escenario Business: priorizar la zona de mayor intensidad o mayor consumo Scope 2
        zones = list(evaluation.metrics.get("zones_summary", []))
        if zones:
            top_zone = max(zones, key=lambda z: float(z.get("scope2_emissions_kg", 0.0)))
            top_intensity_zone = max(zones, key=lambda z: float(z.get("intensity_kg_per_unit", 0.0)))
            actions.append(
                f"Priorizar eficiencia energética en '{top_zone['zone_name']}' "
                f"({top_zone['scope2_emissions_kg']:.2f} kg CO2e Scope 2): {top_zone.get('mitigation_action', 'optimizar cargas')}."
            )
            if top_intensity_zone["zone_name"] != top_zone["zone_name"]:
                actions.append(
                    f"Reducir intensidad específica en '{top_intensity_zone['zone_name']}' "
                    f"({top_intensity_zone['intensity_kg_per_unit']:.4f} kg/unidad) mediante balanceo de turnos."
                )
        if evaluation.status == STATUS_BLOCKED:
            actions.append(
                f"Implementar reducción adicional de consumo eléctrico para bajar la intensidad debajo de "
                f"{self.config.business_max_intensity_kg_per_unit:.2f} kg/unidad y alcanzar al menos "
                f"{self.config.business_min_reduction_pct:.1f}% de mejora Scope 2."
            )
        elif not actions:
            actions.append(
                "Continuar monitoreo zonal y registrar el siguiente periodo voluntario manteniendo la trazabilidad."
            )
        return actions

    def generate_voluntary_report(
        self,
        evaluation: PolicyEvaluationResult,
        graph: AttestationGraph,
        *,
        report_id: Optional[str] = None,
    ) -> VoluntaryReport:
        """Construye un reporte voluntario estructurado con trazabilidad completa del grafo."""
        is_synthetic = evaluation.source_classification == "synthetic_declared"
        summary = self.explain_decision(evaluation, graph=graph)
        honest_notice = self._honest_scope_notice(
            evaluation.scenario_type,
            evaluation.source_classification,
        )
        mitigations = self.suggest_mitigations(evaluation, graph=graph)

        # Selecciona el nodo de mayor nivel para trazar el lineage completo desde la fuente
        ordered_nodes = sorted(graph.nodes.values(), key=lambda n: (n.level, n.node_id))
        if ordered_nodes:
            highest_level = ordered_nodes[-1].level
            roots_or_tails = [n for n in ordered_nodes if n.level == highest_level]
            lineage_nodes = graph.get_lineage(roots_or_tails[-1].node_id)
        else:
            lineage_nodes = []

        resolved_report_id = report_id or f"VERA-RPT-{graph.graph_id}-{evaluation.evidence_root[:8]}"
        return VoluntaryReport(
            report_id=resolved_report_id,
            generated_at=evaluation.evaluated_at,
            agent_id=self.config.agent_id,
            scenario_type=evaluation.scenario_type,
            source_classification=evaluation.source_classification,
            is_synthetic=is_synthetic,
            policy_status=evaluation.status,
            payout_eligible=evaluation.payout_eligible,
            policy_hash=evaluation.policy_hash,
            evidence_root=evaluation.evidence_root,
            executive_summary_es=summary,
            honest_scope_notice_es=honest_notice,
            metrics=dict(evaluation.metrics),
            mitigation_actions=mitigations,
            lineage=[n.to_dict() for n in lineage_nodes],
            checks=[c.to_dict() for c in evaluation.checks],
        )
