"""Configuración centralizada de VERA Core basada exclusivamente en variables de entorno.

Todas las variables configurables del motor de políticas, escenarios, grafo de
attestations y agente se leen desde el entorno (con soporte para archivos `.env`
mediante `python-dotenv`), evitando cualquier valor hardcodeado en los módulos
de lógica de negocio.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple

from dotenv import load_dotenv


def _env_str(name: str, default: str) -> str:
    """Obtiene una variable de entorno de tipo cadena de texto."""
    value = os.environ.get(name)
    return value.strip() if value is not None and value.strip() != "" else default


def _env_int(name: str, default: int) -> int:
    """Obtiene una variable de entorno entera y valida su conversión."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"La variable de entorno {name} debe ser un entero válido, recibido: {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    """Obtiene una variable de entorno flotante y valida su conversión."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"La variable de entorno {name} debe ser numérica válida, recibido: {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    """Obtiene una variable de entorno booleana."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "si", "sí", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"La variable de entorno {name} debe ser booleana, recibido: {raw!r}")


def _env_str_tuple(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    """Obtiene una lista separada por comas desde una variable de entorno."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not items:
        raise ValueError(f"La variable de entorno {name} no puede estar vacía si se define.")
    return items


def _env_float_tuple(name: str, default: Tuple[float, ...]) -> Tuple[float, ...]:
    """Obtiene una tupla de flotantes separados por comas desde una variable de entorno."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        items = tuple(float(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ValueError(f"La variable de entorno {name} debe contener números separados por comas.") from exc
    if not items:
        raise ValueError(f"La variable de entorno {name} no puede estar vacía si se define.")
    return items


@dataclass(frozen=True)
class VeraConfig:
    """Configuración inmutable de VERA Core cargada desde variables de entorno."""

    # Identidad del agente y red Concordium
    agent_id: str
    agent_display_name: str
    concordium_network: str
    concordium_agent_account: str
    evidence_registry_index: int
    evidence_registry_subindex: int
    reward_pool_index: int
    reward_pool_subindex: int
    cis8004_index: int
    cis8004_subindex: int

    # Parámetros del Policy Engine
    policy_version: str
    reference_time_iso: str
    min_coverage_ratio: float
    expected_batch_readings: int
    max_calibration_age_hours: int
    require_recovery_for_anomaly: bool

    # Parámetros escenario Home Air (MQ-135)
    home_device_id: str
    home_sensor_model: str
    home_firmware_version: str
    home_location_label: str
    home_air_interval_minutes: int
    home_air_baseline_raw: float
    home_air_baseline_max_raw: float
    home_air_anomaly_peak_raw: float
    home_air_anomaly_min_raw: float
    home_air_recovery_raw: float
    home_air_recovery_max_raw: float

    # Parámetros escenario Business (datos sintéticos declarados)
    business_org_id: str
    business_org_name: str
    business_generator_version: str
    business_zones: Tuple[str, ...]
    business_grid_emission_factor_kg_per_kwh: float
    business_max_intensity_kg_per_unit: float
    business_min_reduction_pct: float
    business_zone_baseline_kwh: Tuple[float, ...]
    business_zone_current_kwh: Tuple[float, ...]
    business_zone_units: Tuple[float, ...]

    # Liquidación / recompensas
    home_reward_amount_micro_ccd: int
    business_reward_amount_micro_ccd: int

    @classmethod
    def from_env(cls, load_dotenv_file: bool = True) -> "VeraConfig":
        """Construye la configuración leyendo variables de entorno (y `.env` si existe)."""
        if load_dotenv_file:
            load_dotenv(override=False)

        zones = _env_str_tuple(
            "VERA_BUSINESS_ZONES",
            ("Oficinas", "Producción", "Bodega", "Logística"),
        )
        baseline_kwh = _env_float_tuple(
            "VERA_BUSINESS_ZONE_BASELINE_KWH",
            (1200.0, 4800.0, 1600.0, 2400.0),
        )
        current_kwh = _env_float_tuple(
            "VERA_BUSINESS_ZONE_CURRENT_KWH",
            (1050.0, 4200.0, 1440.0, 2100.0),
        )
        zone_units = _env_float_tuple(
            "VERA_BUSINESS_ZONE_UNITS",
            (900.0, 2100.0, 1100.0, 1450.0),
        )

        if not (len(zones) == len(baseline_kwh) == len(current_kwh) == len(zone_units)):
            raise ValueError(
                "Las variables VERA_BUSINESS_ZONES, VERA_BUSINESS_ZONE_BASELINE_KWH, "
                "VERA_BUSINESS_ZONE_CURRENT_KWH y VERA_BUSINESS_ZONE_UNITS deben tener la misma longitud."
            )

        min_cov = _env_float("VERA_MIN_COVERAGE_RATIO", 0.85)
        if not (0.0 < min_cov <= 1.0):
            raise ValueError("VERA_MIN_COVERAGE_RATIO debe estar en el rango (0.0, 1.0].")

        expected_readings = _env_int("VERA_EXPECTED_BATCH_READINGS", 48)
        if expected_readings <= 0:
            raise ValueError("VERA_EXPECTED_BATCH_READINGS debe ser mayor a 0.")

        return cls(
            agent_id=_env_str("VERA_AGENT_ID", "vera-agent-demo-01"),
            agent_display_name=_env_str("VERA_AGENT_DISPLAY_NAME", "VERA Agente Ambiental Verificable"),
            concordium_network=_env_str("CONCORDIUM_NETWORK", "testnet"),
            concordium_agent_account=_env_str(
                "CONCORDIUM_AGENT_ACCOUNT_ADDRESS",
                "4xTestAccountAddressConcordiumExampleOnly111111111",
            ),
            evidence_registry_index=_env_int("CONCORDIUM_EVIDENCE_REGISTRY_INDEX", 1000),
            evidence_registry_subindex=_env_int("CONCORDIUM_EVIDENCE_REGISTRY_SUBINDEX", 0),
            reward_pool_index=_env_int("CONCORDIUM_REWARD_POOL_INDEX", 1001),
            reward_pool_subindex=_env_int("CONCORDIUM_REWARD_POOL_SUBINDEX", 0),
            cis8004_index=_env_int("CONCORDIUM_CIS8004_INDEX", 1002),
            cis8004_subindex=_env_int("CONCORDIUM_CIS8004_SUBINDEX", 0),
            policy_version=_env_str("VERA_POLICY_VERSION", "vera-policy-v1"),
            reference_time_iso=_env_str("VERA_REFERENCE_TIME", ""),
            min_coverage_ratio=min_cov,
            expected_batch_readings=expected_readings,
            max_calibration_age_hours=_env_int("VERA_MAX_CALIBRATION_AGE_HOURS", 720),
            require_recovery_for_anomaly=_env_bool("VERA_REQUIRE_RECOVERY_FOR_ANOMALY", True),
            home_device_id=_env_str("VERA_HOME_DEVICE_ID", "esp32-mq135-demo-01"),
            home_sensor_model=_env_str("VERA_HOME_SENSOR_MODEL", "MQ-135"),
            home_firmware_version=_env_str("VERA_HOME_FIRMWARE_VERSION", "vera-fw-1.0.0"),
            home_location_label=_env_str("VERA_HOME_LOCATION_LABEL", "home-living-room"),
            home_air_interval_minutes=_env_int("VERA_HOME_AIR_INTERVAL_MINUTES", 30),
            home_air_baseline_raw=_env_float("VERA_HOME_AIR_BASELINE_RAW", 325.0),
            home_air_baseline_max_raw=_env_float("VERA_HOME_AIR_BASELINE_MAX_RAW", 380.0),
            home_air_anomaly_peak_raw=_env_float("VERA_HOME_AIR_ANOMALY_PEAK_RAW", 650.0),
            home_air_anomaly_min_raw=_env_float("VERA_HOME_AIR_ANOMALY_MIN_RAW", 520.0),
            home_air_recovery_raw=_env_float("VERA_HOME_AIR_RECOVERY_RAW", 335.0),
            home_air_recovery_max_raw=_env_float("VERA_HOME_AIR_RECOVERY_MAX_RAW", 390.0),
            business_org_id=_env_str("VERA_BUSINESS_ORG_ID", "org-demo-enterprise-01"),
            business_org_name=_env_str("VERA_BUSINESS_ORG_NAME", "Empresa Demo Sostenible"),
            business_generator_version=_env_str("VERA_BUSINESS_GENERATOR_VERSION", "vera-synth-v1"),
            business_zones=zones,
            business_grid_emission_factor_kg_per_kwh=_env_float(
                "VERA_BUSINESS_GRID_EMISSION_FACTOR_KG_PER_KWH", 0.164
            ),
            business_max_intensity_kg_per_unit=_env_float(
                "VERA_BUSINESS_MAX_INTENSITY_KG_PER_UNIT", 0.45
            ),
            business_min_reduction_pct=_env_float("VERA_BUSINESS_MIN_REDUCTION_PCT", 5.0),
            business_zone_baseline_kwh=baseline_kwh,
            business_zone_current_kwh=current_kwh,
            business_zone_units=zone_units,
            home_reward_amount_micro_ccd=_env_int("VERA_REWARD_AMOUNT_MICRO_CCD", 1000000),
            business_reward_amount_micro_ccd=_env_int("VERA_BUSINESS_REWARD_AMOUNT_MICRO_CCD", 2500000),
        )
