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

import csv
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    expired_calibration: bool = False,
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
    if expired_calibration or variant == "expired_calibration":
        cal_dt = eval_dt - timedelta(hours=cfg.max_calibration_age_hours + 120)
        expires_dt = eval_dt - timedelta(hours=24)
        sensor_status = "EXPIRED"
        cal_notes = "Calibración vencida: ventana de vigencia expirada en escenario empresarial."
    else:
        cal_dt = eval_dt - timedelta(hours=72)
        expires_dt = eval_dt + timedelta(hours=max(24, cfg.max_calibration_age_hours - 72))
        sensor_status = "SYNTHETIC_DECLARED"
        cal_notes = "Factor de emisión de red declarado y medidores zonales verificados en escenario sintético."

    cal_node = graph.register_calibration(
        node_id=f"{graph_id}-calibration",
        parent_ids=[src_node.node_id],
        source_classification=source_cls,
        timestamp=cal_dt.isoformat(),
        calibrated_at=cal_dt.isoformat(),
        calibration_expires_at=expires_dt.isoformat(),
        sensor_status=sensor_status,
        reference_baseline=sum(cfg.business_zone_baseline_kwh),
        notes=cal_notes,
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
            "expired_calibration": expired_calibration or variant == "expired_calibration",
            "calibrated_at": cal_dt.isoformat(),
            "calibration_expires_at": expires_dt.isoformat(),
        },
    )


def _resolve_imported_gps_csv_path(csv_path: Optional[str]) -> Path:
    """Resuelve la ruta al archivo CSV+GPS importado desde parámetro o entorno."""
    repo_root = Path(__file__).resolve().parent.parent
    env_override = os.environ.get("VERA_IMPORTED_GPS_CSV_PATH", "").strip()
    candidates: List[Path] = []
    if csv_path:
        p = Path(csv_path)
        candidates.append(p if p.is_absolute() else (repo_root / p))
        candidates.append(p)
    elif env_override:
        p = Path(env_override)
        candidates.append(p if p.is_absolute() else (repo_root / p))
    else:
        candidates.append(repo_root / "tests" / "fixtures" / "sample_gps_mq135.csv")
        candidates.append(repo_root / "hardware_files" / "Test_gps" / "data.csv")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "No se encontró el archivo CSV+GPS importado en las rutas configuradas."
    )


def _parse_csv_timestamp(raw_date: str, fallback_dt: datetime) -> str:
    """Normaliza la columna `date_c` del CSV o usa el timestamp determinista de respaldo."""
    cleaned = raw_date.strip()
    if not cleaned:
        return fallback_dt.isoformat()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        return fallback_dt.isoformat()


def get_imported_gps_scenario(
    csv_path: Optional[str] = None,
    tampered: bool = False,
    incomplete: bool = False,
    expired_calibration: bool = False,
    *,
    variant: Optional[str] = None,
    reference_time: Optional[datetime] = None,
    config: Optional[VeraConfig] = None,
) -> ScenarioBundle:
    """Genera el escenario de datos importados CSV+GPS (`source_type='imported'`).

    Lee `tests/fixtures/sample_gps_mq135.csv` (o `hardware_files/Test_gps/data.csv`),
    conserva `lat`, `lon` y `raw_signal` (`co2`), marca explícitamente
    `source_type='imported'` y construye el grafo de attestations de VERA.
    """
    cfg = config or VeraConfig.from_env()
    eval_dt = _resolve_reference_time(cfg, reference_time)
    eval_ts = eval_dt.isoformat()

    is_tampered = tampered or variant == "tampered"
    is_incomplete = incomplete or variant == "incomplete"
    is_expired = expired_calibration or variant == "expired_calibration"

    if variant:
        resolved_variant = variant
    elif is_tampered:
        resolved_variant = "tampered"
    elif is_incomplete:
        resolved_variant = "incomplete"
    elif is_expired:
        resolved_variant = "expired_calibration"
    else:
        resolved_variant = "valid"

    resolved_csv = _resolve_imported_gps_csv_path(csv_path)
    full_readings: List[Dict[str, Any]] = []
    with resolved_csv.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    if not rows:
        raise ValueError(f"El archivo CSV importado está vacío: {resolved_csv}")

    total_expected = len(rows)
    start_dt = eval_dt - timedelta(minutes=cfg.home_air_interval_minutes * (total_expected - 1))

    for idx, row in enumerate(rows):
        fallback_dt = start_dt + timedelta(minutes=cfg.home_air_interval_minutes * idx)
        ts = _parse_csv_timestamp(str(row.get("date_c") or ""), fallback_dt)
        co2_raw = float(row["co2"])
        lat = float(row["lat"])
        lon = float(row["lon"])
        origin = str(row.get("origin") or "sensor1").strip() or "sensor1"

        entry: Dict[str, Any] = {
            "seq": idx + 1,
            "timestamp": ts,
            "source_type": "imported",
            "origin": origin,
            "lat": lat,
            "lon": lon,
            "raw_signal": co2_raw,
            "co2": co2_raw,
            "unit": "adc_raw",
            "sensor_model": cfg.home_sensor_model,
            "is_certified_co2": False,
        }
        if row.get("co2_") not in (None, ""):
            entry["co2_"] = float(row["co2_"])
        full_readings.append(entry)

    if is_incomplete:
        keep_count = max(1, int(total_expected * max(0.25, cfg.min_coverage_ratio - 0.30)))
        batch_readings = full_readings[:keep_count]
    else:
        batch_readings = full_readings

    if is_expired:
        calibrated_dt = eval_dt - timedelta(hours=cfg.max_calibration_age_hours + 120)
        expires_dt = eval_dt - timedelta(hours=24)
        sensor_status = "EXPIRED"
        cal_notes = "Calibración vencida en lote importado CSV+GPS."
    else:
        calibrated_dt = eval_dt - timedelta(hours=48)
        expires_dt = eval_dt + timedelta(hours=max(24, cfg.max_calibration_age_hours - 48))
        sensor_status = "VALID"
        cal_notes = "Calibración vigente declarada para lote histórico importado CSV+GPS."

    lats = [float(r["lat"]) for r in batch_readings]
    lons = [float(r["lon"]) for r in batch_readings]
    raw_values = [float(r["raw_signal"]) for r in batch_readings]

    graph_id = f"imported-gps-{resolved_variant}"
    graph = AttestationGraph(graph_id=graph_id)
    source_cls = "imported"

    # Nivel 1: Fuente importada CSV+GPS
    src_node = graph.register_device_source(
        node_id=f"{graph_id}-source",
        source_classification=source_cls,
        timestamp=batch_readings[0]["timestamp"],
        device_id=f"{cfg.home_device_id}-gps-import",
        sensor_model=cfg.home_sensor_model,
        firmware_or_generator="csv-gps-importer-v1",
        location_label=f"gps-track ({min(lats):.4f},{min(lons):.4f})..({max(lats):.4f},{max(lons):.4f})",
        declared_limitations=[
            "Datos históricos importados desde archivo CSV con trazas GPS (source_type='imported').",
            "La columna 'co2' corresponde a señal MQ-135 registrada en campo y no equivale a medición certificada.",
        ],
        extra_metadata={
            "source_type": "imported",
            "csv_filename": resolved_csv.name,
            "lat_min": min(lats),
            "lat_max": max(lats),
            "lon_min": min(lons),
            "lon_max": max(lons),
        },
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

    # Nivel 3: Lote CSV+GPS
    batch_node = graph.register_batch(
        node_id=f"{graph_id}-batch",
        parent_ids=[src_node.node_id, cal_node.node_id],
        source_classification=source_cls,
        timestamp=batch_readings[-1]["timestamp"],
        readings=batch_readings,
        expected_count=total_expected,
        unit="adc_raw",
    )

    # Nivel 4: Dataset agregado
    coverage_ratio = len(batch_readings) / total_expected
    dataset_node = graph.register_dataset(
        node_id=f"{graph_id}-dataset",
        parent_ids=[batch_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        aggregation_window=f"{total_expected * cfg.home_air_interval_minutes}m",
        reading_count=len(batch_readings),
        expected_count=total_expected,
        coverage_ratio=coverage_ratio,
        mean_value=mean(raw_values),
        min_value=min(raw_values),
        max_value=max(raw_values),
    )

    # Nivel 5: Análisis / Cálculo sobre trayectoria GPS
    baseline_avg = mean(raw_values[: max(1, len(raw_values) // 4)])
    peak_val = max(raw_values)
    recovery_avg = mean(raw_values[-max(1, len(raw_values) // 4) :])
    anomaly_detected = peak_val >= cfg.home_air_anomaly_min_raw
    anomaly_mitigated = anomaly_detected and recovery_avg <= cfg.home_air_recovery_max_raw
    improvement_pct = ((peak_val - recovery_avg) / peak_val * 100.0) if peak_val > 0 else 0.0

    graph.register_analysis(
        node_id=f"{graph_id}-analysis",
        parent_ids=[dataset_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        methodology="imported_gps_mq135_track_v1",
        baseline_value=baseline_avg,
        peak_value=peak_val,
        recovery_value=recovery_avg,
        anomaly_detected=anomaly_detected,
        anomaly_mitigated=anomaly_mitigated,
        improvement_pct=improvement_pct,
        extra_metrics={
            "source_type": "imported",
            "is_certified_co2": False,
            "unit": "adc_raw",
            "gps_points": len(batch_readings),
            "lat_center": round(mean(lats), 6),
            "lon_center": round(mean(lons), 6),
        },
    )

    expected_root = graph.build_evidence_tree(max_level=AttestationLevel.ANALYSIS).evidence_root

    if is_tampered:
        batch_node.payload["readings"][0]["raw_signal"] = 1.0

    return ScenarioBundle(
        scenario_type="imported_gps",
        variant=resolved_variant,
        source_classification=source_cls,
        evaluation_timestamp=eval_ts,
        expected_evidence_root=expected_root,
        graph=graph,
        readings=batch_node.payload["readings"],
        zones=[],
        metadata={
            "source_type": "imported",
            "csv_path": str(resolved_csv),
            "device_id": f"{cfg.home_device_id}-gps-import",
            "sensor_model": cfg.home_sensor_model,
            "is_certified_co2": False,
            "expired_calibration": is_expired,
            "calibrated_at": calibrated_dt.isoformat(),
            "calibration_expires_at": expires_dt.isoformat(),
            "gps_points": len(batch_node.payload["readings"]),
            "lat_center": round(mean(lats), 6),
            "lon_center": round(mean(lons), 6),
        },
    )


def _format_telemetry_timestamp(raw_ts: Any, fallback_dt: datetime) -> tuple[str, Optional[int]]:
    """Convierte un timestamp entero (epoch) o ISO de SQLite a ISO-8601 UTC."""
    if raw_ts is None:
        return fallback_dt.isoformat(), int(fallback_dt.timestamp())
    if isinstance(raw_ts, (int, float)):
        epoch_int = int(raw_ts)
        dt = datetime.fromtimestamp(epoch_int, tz=timezone.utc).replace(microsecond=0)
        return dt.isoformat(), epoch_int
    raw_str = str(raw_ts).strip()
    if raw_str.isdigit():
        epoch_int = int(raw_str)
        dt = datetime.fromtimestamp(epoch_int, tz=timezone.utc).replace(microsecond=0)
        return dt.isoformat(), epoch_int
    normalized = f"{raw_str[:-1]}+00:00" if raw_str.endswith("Z") else raw_str
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc).replace(microsecond=0)
        return dt_utc.isoformat(), int(dt_utc.timestamp())
    except ValueError:
        return fallback_dt.isoformat(), int(fallback_dt.timestamp())


def get_live_telemetry_scenario(
    db_path: str,
    limit: int = 48,
    expired_calibration: bool = False,
    tampered: bool = False,
    incomplete: bool = False,
    *,
    variant: Optional[str] = None,
    reference_time: Optional[datetime] = None,
    config: Optional[VeraConfig] = None,
) -> ScenarioBundle:
    """Construye el escenario de telemetría física en vivo desde SQLite (`telemetry`).

    Consulta las últimas `limit` filas en orden cronológico ascendente. Si la tabla
    no existe o tiene menos de 10 lecturas (o `incomplete=True`), construye igualmente
    el grafo de niveles 1–5 con `source_type='physical'` para que `PolicyEngine`
    evalúe `INCOMPLETE` con razón clara.
    """
    cfg = config or VeraConfig.from_env()
    eval_dt = _resolve_reference_time(cfg, reference_time)
    eval_ts = eval_dt.isoformat()

    is_tampered = tampered or variant == "tampered"
    is_incomplete_flag = incomplete or variant == "incomplete"
    is_expired = expired_calibration or variant == "expired_calibration"

    rows: List[sqlite3.Row] = []
    safe_limit = max(1, int(limit))
    if db_path == ":memory:" or Path(db_path).is_file():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, device_id, source_type, raw_signal, lat, lon, sequence, timestamp, evidence_hash
                FROM telemetry
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (safe_limit,),
            )
            rows = cursor.fetchall()
            rows.reverse()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()

    full_readings: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        ts_iso, ts_epoch = _format_telemetry_timestamp(row["timestamp"], eval_dt)
        lat_val = float(row["lat"]) if row["lat"] is not None else 0.0
        lon_val = float(row["lon"]) if row["lon"] is not None else 0.0
        seq_val = int(row["sequence"]) if row["sequence"] is not None else (idx + 1)
        device_id_val = str(row["device_id"] or cfg.home_device_id).strip() or cfg.home_device_id

        full_readings.append({
            "seq": idx + 1,
            "sequence": seq_val,
            "device_id": device_id_val,
            "source_type": "physical",
            "timestamp": ts_iso,
            "timestamp_epoch": ts_epoch,
            "raw_signal": float(row["raw_signal"]),
            "lat": lat_val,
            "lon": lon_val,
            "unit": "adc_raw",
            "sensor_model": "MQ-135-raw",
            "is_certified_co2": False,
        })

    min_live_readings = 10
    has_insufficient_rows = len(full_readings) < min_live_readings

    if has_insufficient_rows:
        batch_readings = list(full_readings)
        min_expected_to_fail = int(math.ceil(min_live_readings / cfg.min_coverage_ratio)) + 1
        expected_count = max(cfg.expected_batch_readings, min_expected_to_fail)
    elif is_incomplete_flag:
        total_expected = len(full_readings)
        keep_count = max(1, int(total_expected * max(0.25, cfg.min_coverage_ratio - 0.30)))
        batch_readings = full_readings[:keep_count]
        min_expected_to_fail = int(math.ceil(keep_count / cfg.min_coverage_ratio)) + 1
        expected_count = max(total_expected, min_expected_to_fail)
    else:
        batch_readings = list(full_readings)
        expected_count = len(full_readings)

    if variant:
        resolved_variant = variant
    elif is_tampered:
        resolved_variant = "tampered"
    elif is_incomplete_flag or has_insufficient_rows:
        resolved_variant = "incomplete"
    elif is_expired:
        resolved_variant = "expired_calibration"
    else:
        resolved_variant = "valid"

    if is_expired:
        calibrated_dt = eval_dt - timedelta(hours=cfg.max_calibration_age_hours + 120)
        expires_dt = eval_dt - timedelta(hours=24)
        sensor_status = "EXPIRED"
        cal_notes = "Calibración vencida en telemetría física en vivo (ESP8266 / MQ-135-raw)."
    else:
        calibrated_dt = eval_dt - timedelta(hours=48)
        expires_dt = eval_dt + timedelta(hours=max(24, cfg.max_calibration_age_hours - 48))
        sensor_status = "VALID"
        cal_notes = "Calibración vigente declarada para telemetría física en vivo (MQ-135-raw)."

    raw_values = [float(r["raw_signal"]) for r in batch_readings]
    device_ids = sorted({str(r["device_id"]) for r in batch_readings})
    primary_device_id = str(batch_readings[0]["device_id"]) if batch_readings else cfg.home_device_id
    gps_readings = [
        r for r in batch_readings
        if float(r.get("lat", 0.0)) != 0.0 or float(r.get("lon", 0.0)) != 0.0
    ]
    gps_lats = [float(r["lat"]) for r in gps_readings]
    gps_lons = [float(r["lon"]) for r in gps_readings]
    location_label = (
        f"esp8266-gps ({min(gps_lats):.4f},{min(gps_lons):.4f})..({max(gps_lats):.4f},{max(gps_lons):.4f})"
        if gps_readings
        else cfg.home_location_label
    )

    graph_id = f"live-telemetry-{resolved_variant}"
    graph = AttestationGraph(graph_id=graph_id)
    source_cls = "physical"

    # Nivel 1: Dispositivo físico en vivo (ESP8266 / MQ-135-raw)
    src_node = graph.register_device_source(
        node_id=f"{graph_id}-source",
        source_classification=source_cls,
        timestamp=batch_readings[0]["timestamp"] if batch_readings else eval_ts,
        device_id=primary_device_id,
        sensor_model="MQ-135-raw",
        firmware_or_generator="esp8266-live-telemetry-v1",
        location_label=location_label,
        declared_limitations=[
            "Telemetría física en vivo recibida vía POST /api/telemetry y persistida en SQLite.",
            "El sensor MQ-135-raw reporta señal analógica cruda ADC; is_certified_co2=False.",
        ],
        extra_metadata={
            "source_type": "physical",
            "sensor_model": "MQ-135-raw",
            "is_certified_co2": False,
            "device_ids": device_ids,
            "gps_points": len(gps_readings),
        },
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

    # Nivel 3: Lote de telemetría en vivo
    batch_node = graph.register_batch(
        node_id=f"{graph_id}-batch",
        parent_ids=[src_node.node_id, cal_node.node_id],
        source_classification=source_cls,
        timestamp=batch_readings[-1]["timestamp"] if batch_readings else eval_ts,
        readings=batch_readings,
        expected_count=expected_count,
        unit="adc_raw",
    )

    # Nivel 4: Dataset agregado
    coverage_ratio = (len(batch_readings) / expected_count) if expected_count > 0 else 0.0
    mean_val = mean(raw_values) if raw_values else 0.0
    min_val = min(raw_values) if raw_values else 0.0
    max_val = max(raw_values) if raw_values else 0.0

    dataset_node = graph.register_dataset(
        node_id=f"{graph_id}-dataset",
        parent_ids=[batch_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        aggregation_window=f"live_last_{safe_limit}_readings",
        reading_count=len(batch_readings),
        expected_count=expected_count,
        coverage_ratio=coverage_ratio,
        mean_value=mean_val,
        min_value=min_val,
        max_value=max_val,
    )

    # Nivel 5: Análisis / Cálculo sobre serie en vivo
    if raw_values:
        baseline_avg = mean(raw_values[: max(1, len(raw_values) // 4)])
        peak_val = max(raw_values)
        recovery_avg = mean(raw_values[-max(1, len(raw_values) // 4) :])
    else:
        baseline_avg = 0.0
        peak_val = 0.0
        recovery_avg = 0.0

    anomaly_detected = peak_val >= cfg.home_air_anomaly_min_raw
    anomaly_mitigated = anomaly_detected and recovery_avg <= cfg.home_air_recovery_max_raw
    improvement_pct = ((peak_val - recovery_avg) / peak_val * 100.0) if peak_val > 0 else 0.0

    extra_analysis_metrics: Dict[str, Any] = {
        "source_type": "physical",
        "sensor_model": "MQ-135-raw",
        "is_certified_co2": False,
        "unit": "adc_raw",
        "gps_points": len(gps_readings),
    }
    if gps_readings:
        extra_analysis_metrics["lat_center"] = round(mean(gps_lats), 6)
        extra_analysis_metrics["lon_center"] = round(mean(gps_lons), 6)

    graph.register_analysis(
        node_id=f"{graph_id}-analysis",
        parent_ids=[dataset_node.node_id],
        source_classification=source_cls,
        timestamp=eval_ts,
        methodology="live_mq135_raw_telemetry_v1",
        baseline_value=baseline_avg,
        peak_value=peak_val,
        recovery_value=recovery_avg,
        anomaly_detected=anomaly_detected,
        anomaly_mitigated=anomaly_mitigated,
        improvement_pct=improvement_pct,
        extra_metrics=extra_analysis_metrics,
    )

    expected_root = graph.build_evidence_tree(max_level=AttestationLevel.ANALYSIS).evidence_root

    if is_tampered:
        if batch_node.payload["readings"]:
            batch_node.payload["readings"][0]["raw_signal"] = 1.0
        else:
            dataset_node.payload["reading_count"] = -1

    bundle_metadata: Dict[str, Any] = {
        "source_type": "physical",
        "db_path": str(db_path),
        "device_id": primary_device_id,
        "device_ids": device_ids,
        "sensor_model": "MQ-135-raw",
        "is_certified_co2": False,
        "total_db_readings": len(full_readings),
        "min_required_readings": min_live_readings,
        "expired_calibration": is_expired,
        "calibrated_at": calibrated_dt.isoformat(),
        "calibration_expires_at": expires_dt.isoformat(),
        "has_gps": bool(gps_readings),
        "gps_points": len(gps_readings),
    }
    if gps_readings:
        bundle_metadata["lat_center"] = round(mean(gps_lats), 6)
        bundle_metadata["lon_center"] = round(mean(gps_lons), 6)

    return ScenarioBundle(
        scenario_type="live_telemetry",
        variant=resolved_variant,
        source_classification=source_cls,
        evaluation_timestamp=eval_ts,
        expected_evidence_root=expected_root,
        graph=graph,
        readings=batch_node.payload["readings"],
        zones=[],
        metadata=bundle_metadata,
    )
