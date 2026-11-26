"""Generadores reproducibles de escenarios para VERA Core.

Provee los dos escenarios oficiales del MVP:
1. `get_home_air_scenario()`:
   - 48 lecturas del sensor MQ-135 reportadas explícitamente como señal cruda ADC
     (no como CO2 certificado), cubriendo línea base, pico de anomalía,
     intervención y recuperación.
   - Permite generar el caso válido y el caso con calibración expirada (además
     de variantes incompleta, manipulada o sin recuperación para pruebas).
2. `get_business_scenario()`:
   - Datos sintéticos declarados (`synthetic_declared`) para las 4 zonas
     configuradas por entorno (`Oficinas`, `Producción`, `Bodega`, `Logística`),
     con consumo kWh, emisiones Scope 2, unidades producidas e intensidad.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Dict, List, Optional

from vera_core.attestations import AttestationGraph, AttestationLevel
from vera_core.config import VeraConfig


def _resolve_reference_time(config: VeraConfig, reference_time: Optional[datetime]) -> datetime:
    """Resuelve la marca temporal UTC de referencia desde parámetro o variable de entorno."""
    if reference_time is not None:
        dt = reference_time
    elif config.reference_time_iso:
        raw = config.reference_time_iso.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        dt = datetime.fromisoformat(raw)
    else:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


@dataclass
class ScenarioBundle:
    """Contenedor completo de un escenario listo para evaluación y auditoría."""

    scenario_type: str
    variant: str
    source_classification: str
    evaluation_timestamp: str
    expected_evidence_root: str
    graph: AttestationGraph
    readings: List[Dict[str, Any]] = field(default_factory=list)
    zones: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el escenario completo a diccionario."""
        return {
            "scenario_type": self.scenario_type,
            "variant": self.variant,
            "source_classification": self.source_classification,
            "evaluation_timestamp": self.evaluation_timestamp,
            "expected_evidence_root": self.expected_evidence_root,
            "readings": list(self.readings),
            "zones": list(self.zones),
            "metadata": dict(self.metadata),
            "graph": self.graph.to_dict(),
        }


def _build_home_air_curve(
    config: VeraConfig,
    eval_dt: datetime,
    unmitigated: bool = False,
) -> List[Dict[str, Any]]:
    """Genera la serie determinista de 48 lecturas MQ-135 en 4 fases."""
    total = config.expected_batch_readings
    baseline = config.home_air_baseline_raw
    peak = config.home_air_anomaly_peak_raw
    recovery = config.home_air_recovery_raw

    # Distribución proporcional para soportar cualquier N configurado (por defecto 48)
    n_base = max(1, round(total * 14 / 48))
    n_peak = max(1, round(total * 12 / 48))
    n_inter = max(1, round(total * 10 / 48))

    readings: List[Dict[str, Any]] = []
    start_dt = eval_dt - timedelta(minutes=config.home_air_interval_minutes * (total - 1))

    for idx in range(total):
        ts = (start_dt + timedelta(minutes=config.home_air_interval_minutes * idx)).isoformat()
        wobble = round(6.0 * math.sin(idx * 0.7), 2)

        if idx < n_base:
            phase = "baseline"
            raw_val = round(baseline + wobble, 2)
        elif idx < n_base + n_peak:
            phase = "anomaly_peak"
            progress = (idx - n_base + 1) / n_peak
            # Sube hasta el pico y lo sostiene en el tercio superior
            ramp = math.sin(progress * math.pi * 0.5)
            raw_val = round(baseline + (peak - baseline) * ramp + wobble * 0.5, 2)
        elif idx < n_base + n_peak + n_inter:
            phase = "intervention"
            if unmitigated:
                raw_val = round(peak - 15.0 + wobble, 2)
            else:
                progress = (idx - (n_base + n_peak) + 1) / n_inter
                raw_val = round(peak - (peak - recovery) * progress + wobble * 0.4, 2)
        else:
            phase = "recovery"
            if unmitigated:
                raw_val = round(peak - 25.0 + wobble, 2)
            else:
                raw_val = round(recovery + wobble * 0.6, 2)

        readings.append({
            "seq": idx + 1,
            "timestamp": ts,
            "raw_signal": raw_val,
            "unit": "adc_raw",
            "phase": phase,
            "sensor_model": config.home_sensor_model,
            "is_certified_co2": False,
        })
    return readings


def get_home_air_scenario(
    *,
    expired_calibration: bool = False,
    variant: Optional[str] = None,
    reference_time: Optional[datetime] = None,
    config: Optional[VeraConfig] = None,
) -> ScenarioBundle:
    """Genera el escenario Vera Home Air (48 lecturas MQ-135) y su grafo de attestations.

    Parámetros:
    - `expired_calibration`: Si es `True`, genera el caso con calibración expirada
      (`sensor_status='EXPIRED'` y `calibration_expires_at` anterior a la evaluación).
    - `variant`: Permite seleccionar explícitamente `'valid'`, `'expired_calibration'`,
      `'incomplete'`, `'tampered'` o `'unmitigated_anomaly'`.
    - `reference_time`: Marca temporal UTC de evaluación para reproducibilidad.
    - `config`: Instancia `VeraConfig` (si se omite, se carga desde variables de entorno).
    """
    cfg = config or VeraConfig.from_env()
    resolved_variant = variant or ("expired_calibration" if expired_calibration else "valid")
    eval_dt = _resolve_reference_time(cfg, reference_time)
    eval_ts = eval_dt.isoformat()

    unmitigated = resolved_variant == "unmitigated_anomaly"
    full_readings = _build_home_air_curve(cfg, eval_dt=eval_dt, unmitigated=unmitigated)

    if resolved_variant == "incomplete":
        # Reduce por debajo de min_coverage_ratio para probar estado INCOMPLETE
        keep_count = max(1, int(cfg.expected_batch_readings * max(0.25, cfg.min_coverage_ratio - 0.30)))
        batch_readings = full_readings[:keep_count]
    else:
        batch_readings = full_readings

    # Vigencia de calibración
    if resolved_variant == "expired_calibration" or expired_calibration:
        calibrated_dt = eval_dt - timedelta(hours=cfg.max_calibration_age_hours + 120)
        expires_dt = eval_dt - timedelta(hours=24)
        sensor_status = "EXPIRED"
        cal_notes = "Calibración vencida: ventana de vigencia expirada respecto al timestamp actual."
    else:
        calibrated_dt = eval_dt - timedelta(hours=48)
        expires_dt = eval_dt + timedelta(hours=max(24, cfg.max_calibration_age_hours - 48))
        sensor_status = "VALID"
        cal_notes = "Calibración vigente en aire limpio de referencia; señal reportada como ADC crudo."

    graph_id = f"home-air-{resolved_variant}"
    graph = AttestationGraph(graph_id=graph_id)
    source_cls = "physical"

    # Nivel 1: Dispositivo / Fuente
    src_node = graph.register_device_source(
        node_id=f"{graph_id}-source",
        source_classification=source_cls,
        timestamp=batch_readings[0]["timestamp"],
        device_id=cfg.home_device_id,
        sensor_model=cfg.home_sensor_model,
        firmware_or_generator=cfg.home_firmware_version,
        location_label=cfg.home_location_label,
        declared_limitations=[
            "El sensor MQ-135 reporta señal analógica cruda (ADC) sensible a múltiples gases/VOC.",
            "No representa concentración certificada de CO2 ni sustituye instrumentación acreditada.",
        ],
    )

    # Nivel 2: Calibración
    cal_node = graph.register_calibration(
        node_id=f"{graph_id}-calibration",
        parent_ids=[src_node.node_id],
        source_classification=source_cls,
        timestamp=calibrated_dt.isoformat(),
        calibrated_at=calibrated_dt.isoformat(),
        calibration_expires_at=expires_dt.isoformat(),
        sensor_status=sensor_status,
        reference_baseline=cfg.home_air_baseline_raw,
        notes=cal_notes,
    )

    # Nivel 3: Lote
    batch_node = graph.register_batch(
        node_id=f"{graph_id}-batch",
        parent_ids=[src_node.node_id, cal_node.node_id],
        source_classification=source_cls,
        timestamp=batch_readings[-1]["timestamp"],
        readings=batch_readings,
        expected_count=cfg.expected_batch_readings,
        unit="adc_raw",
    )

    # Nivel 4: Dataset
    raw_values = [float(r["raw_signal"]) for r in batch_readings]
    coverage_ratio = len(batch_readings) / cfg.expected_batch_readings
    dataset_node = graph.register_dataset(
        node_id=f"{graph_id}-dataset",
        parent_ids=[batch_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        aggregation_window=f"{cfg.expected_batch_readings * cfg.home_air_interval_minutes}m",
        reading_count=len(batch_readings),
        expected_count=cfg.expected_batch_readings,
        coverage_ratio=coverage_ratio,
        mean_value=mean(raw_values),
        min_value=min(raw_values),
        max_value=max(raw_values),
    )

    # Nivel 5: Análisis / Cálculo
    baseline_vals = [float(r["raw_signal"]) for r in batch_readings if r["phase"] == "baseline"]
    recovery_vals = [float(r["raw_signal"]) for r in batch_readings if r["phase"] == "recovery"]
    baseline_avg = mean(baseline_vals) if baseline_vals else raw_values[0]
    peak_val = max(raw_values)
    recovery_avg = mean(recovery_vals) if recovery_vals else raw_values[-1]
    anomaly_detected = peak_val >= cfg.home_air_anomaly_min_raw
    anomaly_mitigated = anomaly_detected and recovery_avg <= cfg.home_air_recovery_max_raw
    improvement_pct = ((peak_val - recovery_avg) / peak_val * 100.0) if peak_val > 0 else 0.0

    graph.register_analysis(
        node_id=f"{graph_id}-analysis",
        parent_ids=[dataset_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        methodology="mq135_raw_baseline_peak_recovery_v1",
        baseline_value=baseline_avg,
        peak_value=peak_val,
        recovery_value=recovery_avg,
        anomaly_detected=anomaly_detected,
        anomaly_mitigated=anomaly_mitigated,
        improvement_pct=improvement_pct,
        extra_metrics={
            "is_certified_co2": False,
            "unit": "adc_raw",
        },
    )

    expected_root = graph.build_evidence_tree(max_level=AttestationLevel.ANALYSIS).evidence_root

    # En variante tampered, se altera una lectura sin recalcular los hashes/raíz esperados
    if resolved_variant == "tampered":
        batch_node.payload["readings"][0]["raw_signal"] = 1.0

    return ScenarioBundle(
        scenario_type="home_air",
        variant=resolved_variant,
        source_classification=source_cls,
        evaluation_timestamp=eval_ts,
        expected_evidence_root=expected_root,
        graph=graph,
        readings=batch_node.payload["readings"],
        zones=[],
        metadata={
            "device_id": cfg.home_device_id,
            "sensor_model": cfg.home_sensor_model,
            "is_certified_co2": False,
            "expired_calibration": resolved_variant == "expired_calibration" or expired_calibration,
            "calibrated_at": calibrated_dt.isoformat(),
            "calibration_expires_at": expires_dt.isoformat(),
        },
    )


def get_business_scenario(
    *,
    variant: str = "eligible",
    reference_time: Optional[datetime] = None,
    config: Optional[VeraConfig] = None,
) -> ScenarioBundle:
    """Genera el escenario Vera Business con datos sintéticos declarados para 4 zonas.

    Zonas por defecto (configurables vía `VERA_BUSINESS_ZONES`):
    Oficinas, Producción, Bodega, Logística.
    Cada zona incluye `kwh`, `scope2_emissions_kg`, `units_produced` e `intensity_kg_per_unit`.
    """
    cfg = config or VeraConfig.from_env()
    eval_dt = _resolve_reference_time(cfg, reference_time)
    eval_ts = eval_dt.isoformat()
    source_cls = "synthetic_declared"

    mitigation_actions_by_index = (
        "Iluminación LED inteligente y climatización por ocupación",
        "Variadores de frecuencia y optimización de turnos térmicos",
        "Control de carga en refrigeración y sellado térmico de accesos",
        "Consolidación de rutas internas y carga eléctrica fuera de pico",
    )

    zones_data: List[Dict[str, Any]] = []
    readings: List[Dict[str, Any]] = []
    ef = cfg.business_grid_emission_factor_kg_per_kwh

    for idx, zone_name in enumerate(cfg.business_zones):
        base_kwh = cfg.business_zone_baseline_kwh[idx]
        curr_kwh = cfg.business_zone_current_kwh[idx]
        units = cfg.business_zone_units[idx]

        # En escenario bloqueado simulamos sobrecarga energética sin reducción ni cumplimiento de intensidad
        if variant == "blocked":
            curr_kwh = round(base_kwh * 1.25, 2)

        baseline_scope2 = round(base_kwh * ef, 4)
        curr_scope2 = round(curr_kwh * ef, 4)
        intensity = round(curr_scope2 / units, 6) if units > 0 else 0.0
        reduction_pct = round(((baseline_scope2 - curr_scope2) / baseline_scope2) * 100.0, 4) if baseline_scope2 > 0 else 0.0

        zone_entry = {
            "zone_name": zone_name,
            "kwh": round(curr_kwh, 2),
            "baseline_kwh": round(base_kwh, 2),
            "scope2_emissions_kg": curr_scope2,
            "baseline_scope2_kg": baseline_scope2,
            "units_produced": round(units, 2),
            "intensity_kg_per_unit": intensity,
            "reduction_pct": reduction_pct,
            "emission_factor_kg_per_kwh": ef,
            "is_synthetic": True,
            "mitigation_action": mitigation_actions_by_index[idx % len(mitigation_actions_by_index)],
        }
        zones_data.append(zone_entry)

    active_zones = zones_data[:-1] if variant == "incomplete" and len(zones_data) > 1 else zones_data

    for idx, z in enumerate(active_zones):
        readings.append({
            "seq": idx + 1,
            "timestamp": (eval_dt - timedelta(hours=len(active_zones) - idx)).isoformat(),
            "zone_name": z["zone_name"],
            "kwh": z["kwh"],
            "scope2_emissions_kg": z["scope2_emissions_kg"],
            "units_produced": z["units_produced"],
            "intensity_kg_per_unit": z["intensity_kg_per_unit"],
            "is_synthetic": True,
        })

    graph_id = f"business-{variant}"
    graph = AttestationGraph(graph_id=graph_id)

    # Nivel 1: Fuente sintética declarada
    src_node = graph.register_device_source(
        node_id=f"{graph_id}-source",
        source_classification=source_cls,
        timestamp=eval_ts,
        device_id=cfg.business_org_id,
        sensor_model="enterprise-synthetic-ledger",
        firmware_or_generator=cfg.business_generator_version,
        location_label=cfg.business_org_name,
        declared_limitations=[
            "Datos sintéticos declarados para demostración empresarial multi-zona.",
            "Las emisiones Scope 2 y reportes voluntarios no sustituyen auditorías ni inventarios acreditados.",
        ],
        extra_metadata={"zones_configured": list(cfg.business_zones)},
    )

    # Nivel 2: Calibración / validación metodológica de medidores
    cal_dt = eval_dt - timedelta(hours=72)
    expires_dt = eval_dt + timedelta(hours=max(24, cfg.max_calibration_age_hours - 72))
    cal_node = graph.register_calibration(
        node_id=f"{graph_id}-calibration",
        parent_ids=[src_node.node_id],
        source_classification=source_cls,
        timestamp=cal_dt.isoformat(),
        calibrated_at=cal_dt.isoformat(),
        calibration_expires_at=expires_dt.isoformat(),
        sensor_status="SYNTHETIC_DECLARED",
        reference_baseline=sum(cfg.business_zone_baseline_kwh),
        notes="Factor de emisión de red declarado y medidores zonales verificados en escenario sintético.",
    )

    # Nivel 3: Lote de lecturas zonales
    expected_zones = len(cfg.business_zones)
    batch_node = graph.register_batch(
        node_id=f"{graph_id}-batch",
        parent_ids=[src_node.node_id, cal_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        readings=readings,
        expected_count=expected_zones,
        unit="kwh_scope2_synthetic",
    )

    # Nivel 4: Dataset agregado
    total_kwh = sum(z["kwh"] for z in active_zones)
    total_baseline_kwh = sum(z["baseline_kwh"] for z in active_zones)
    total_scope2 = sum(z["scope2_emissions_kg"] for z in active_zones)
    total_baseline_scope2 = sum(z["baseline_scope2_kg"] for z in active_zones)
    total_units = sum(z["units_produced"] for z in active_zones)
    overall_intensity = (total_scope2 / total_units) if total_units > 0 else 0.0
    overall_reduction_pct = (
        ((total_baseline_scope2 - total_scope2) / total_baseline_scope2) * 100.0
        if total_baseline_scope2 > 0
        else 0.0
    )

    zone_intensities = [z["intensity_kg_per_unit"] for z in active_zones] or [0.0]
    dataset_node = graph.register_dataset(
        node_id=f"{graph_id}-dataset",
        parent_ids=[batch_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        aggregation_window="monthly_synthetic_period",
        reading_count=len(active_zones),
        expected_count=expected_zones,
        coverage_ratio=len(active_zones) / expected_zones,
        mean_value=mean(zone_intensities),
        min_value=min(zone_intensities),
        max_value=max(zone_intensities),
        zones_summary=active_zones,
    )

    # Nivel 5: Análisis / Cálculo Scope 2 e intensidad
    graph.register_analysis(
        node_id=f"{graph_id}-analysis",
        parent_ids=[dataset_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        methodology="ghg_scope2_location_based_synthetic_v1",
        baseline_value=total_baseline_scope2,
        peak_value=max(z["scope2_emissions_kg"] for z in active_zones),
        recovery_value=total_scope2,
        anomaly_detected=overall_reduction_pct < 0.0,
        anomaly_mitigated=overall_reduction_pct >= cfg.business_min_reduction_pct,
        improvement_pct=overall_reduction_pct,
        extra_metrics={
            "total_kwh": round(total_kwh, 2),
            "total_baseline_kwh": round(total_baseline_kwh, 2),
            "scope2_total_kg_co2e": round(total_scope2, 4),
            "baseline_scope2_kg_co2e": round(total_baseline_scope2, 4),
            "total_units": round(total_units, 2),
            "intensity_kg_per_unit": round(overall_intensity, 6),
            "is_synthetic": True,
        },
    )

    expected_root = graph.build_evidence_tree(max_level=AttestationLevel.ANALYSIS).evidence_root

    if variant == "tampered":
        batch_node.payload["readings"][0]["kwh"] = 0.0

    return ScenarioBundle(
        scenario_type="business",
        variant=variant,
        source_classification=source_cls,
        evaluation_timestamp=eval_ts,
        expected_evidence_root=expected_root,
        graph=graph,
        readings=batch_node.payload["readings"],
        zones=active_zones,
        metadata={
            "org_id": cfg.business_org_id,
            "org_name": cfg.business_org_name,
            "is_synthetic": True,
            "grid_emission_factor_kg_per_kwh": ef,
            "total_kwh": round(total_kwh, 2),
            "scope2_total_kg_co2e": round(total_scope2, 4),
            "total_units": round(total_units, 2),
            "intensity_kg_per_unit": round(overall_intensity, 6),
            "reduction_pct": round(overall_reduction_pct, 4),
        },
    )
