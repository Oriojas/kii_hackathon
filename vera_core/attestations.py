"""Grafo de Attestations de 7 niveles y árbol de evidencias determinista para VERA.

Implementa:
1. Canonicalización JSON determinista y hashing SHA-256 paritario con el bridge JS/TS:
   - Claves de objetos ordenadas lexicográficamente de forma recursiva.
   - Sin espacios (`separators=(",", ":")`) y codificación UTF-8 (`ensure_ascii=False`).
   - Flotantes enteros (p. ej. `1.0`) normalizados a `int` (`1`) para coincidir con
     `JSON.stringify` en JavaScript/TypeScript.
2. Árbol binario de Merkle (`EvidenceTree`) para calcular `evidence_root` y generar/verificar
   pruebas de inclusión combinando pares de hashes hexadecimales `sha256(left + right)`.
3. Grafo dirigido acíclico (`AttestationGraph`) con los 7 niveles de trazabilidad:
   1) Dispositivo / Fuente (`device_source`)
   2) Calibración (`calibration`)
   3) Lote (`batch`)
   4) Dataset (`dataset`)
   5) Análisis / Cálculo (`analysis`)
   6) Evaluación de Política (`policy_evaluation`)
   7) Liquidación (`settlement`)
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


class AttestationLevel(IntEnum):
    """Niveles oficiales del Grafo de Attestations de VERA."""

    DEVICE_SOURCE = 1
    CALIBRATION = 2
    BATCH = 3
    DATASET = 4
    ANALYSIS = 5
    POLICY_EVALUATION = 6
    SETTLEMENT = 7


LEVEL_NAMES: Dict[int, str] = {
    AttestationLevel.DEVICE_SOURCE: "device_source",
    AttestationLevel.CALIBRATION: "calibration",
    AttestationLevel.BATCH: "batch",
    AttestationLevel.DATASET: "dataset",
    AttestationLevel.ANALYSIS: "analysis",
    AttestationLevel.POLICY_EVALUATION: "policy_evaluation",
    AttestationLevel.SETTLEMENT: "settlement",
}

LEVEL_LABELS_ES: Dict[int, str] = {
    AttestationLevel.DEVICE_SOURCE: "Dispositivo / Fuente",
    AttestationLevel.CALIBRATION: "Calibración",
    AttestationLevel.BATCH: "Lote de lecturas",
    AttestationLevel.DATASET: "Dataset agregado",
    AttestationLevel.ANALYSIS: "Análisis / Cálculo",
    AttestationLevel.POLICY_EVALUATION: "Evaluación de Política",
    AttestationLevel.SETTLEMENT: "Liquidación",
}

VALID_SOURCE_CLASSIFICATIONS = frozenset({"physical", "synthetic_declared", "imported"})


def canonicalize_value(value: Any) -> Any:
    """Normaliza recursivamente cualquier valor Python para serialización JSON canónica.

    Garantiza paridad con JavaScript/TypeScript (`JSON.stringify` con claves ordenadas):
    - Rechaza `NaN` o infinitos.
    - Convierte flotantes con parte decimal nula (p. ej. `48.0`) a enteros (`48`).
    - Ordena alfabéticamente las claves de todos los diccionarios anidados.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"No se permite serializar valores no finitos en JSON canónico: {value!r}")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, Mapping):
        return {str(k): canonicalize_value(value[k]) for k in sorted(value.keys(), key=lambda x: str(x))}
    if isinstance(value, (list, tuple)):
        return [canonicalize_value(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return canonicalize_value(value.to_dict())
    raise TypeError(f"Tipo no serializable en JSON canónico: {type(value).__name__}")


def canonical_json(data: Any) -> str:
    """Devuelve una cadena JSON canónica determinista idéntica al bridge JS/TS."""
    normalized = canonicalize_value(data)
    return json.dumps(normalized, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data: Union[bytes, str, Any]) -> str:
    """Calcula el digest SHA-256 en hexadecimal minúscula (64 caracteres)."""
    if isinstance(data, bytes):
        raw_bytes = data
    elif isinstance(data, str):
        raw_bytes = data.encode("utf-8")
    else:
        raw_bytes = canonical_json(data).encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest()


def hash_merkle_pair(left_hex: str, right_hex: str) -> str:
    """Combina dos nodos hermanos del árbol de Merkle de forma idéntica a JS/TS."""
    return sha256_hex(f"{left_hex}{right_hex}")


def compute_policy_hash(policy_params: Mapping[str, Any]) -> str:
    """Calcula el `policy_hash` determinista a partir de los parámetros de la política."""
    return sha256_hex(dict(policy_params))


@dataclass(frozen=True)
class MerkleProofStep:
    """Paso individual dentro de una prueba de inclusión de Merkle."""

    position: str  # "left" | "right"
    hash: str

    def to_dict(self) -> Dict[str, str]:
        """Serializa el paso de prueba a diccionario."""
        return {"position": self.position, "hash": self.hash}


@dataclass(frozen=True)
class EvidenceTree:
    """Árbol binario de evidencias de VERA con raíz `evidence_root` y niveles Merkle."""

    evidence_root: str
    leaf_hashes: Tuple[str, ...]
    leaf_ids: Tuple[str, ...]
    levels: Tuple[Tuple[str, ...], ...]

    def get_proof(self, leaf_index: int) -> List[MerkleProofStep]:
        """Genera la prueba de inclusión de Merkle para la hoja en `leaf_index`."""
        if leaf_index < 0 or leaf_index >= len(self.leaf_hashes):
            raise IndexError(f"Índice de hoja fuera de rango: {leaf_index}")

        proof: List[MerkleProofStep] = []
        idx = leaf_index
        for level in self.levels[:-1]:
            working = list(level)
            if len(working) % 2 == 1:
                working.append(working[-1])
            if idx % 2 == 0:
                sibling_hash = working[idx + 1]
                proof.append(MerkleProofStep(position="right", hash=sibling_hash))
            else:
                sibling_hash = working[idx - 1]
                proof.append(MerkleProofStep(position="left", hash=sibling_hash))
            idx //= 2
        return proof

    def get_proof_by_id(self, leaf_id: str) -> List[MerkleProofStep]:
        """Genera la prueba de inclusión de Merkle buscando por identificador de hoja/nodo."""
        try:
            idx = self.leaf_ids.index(leaf_id)
        except ValueError as exc:
            raise KeyError(f"Hoja no encontrada en el árbol de evidencias: {leaf_id}") from exc
        return self.get_proof(idx)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el árbol completo a una estructura JSON-compatible."""
        return {
            "evidence_root": self.evidence_root,
            "leaf_ids": list(self.leaf_ids),
            "leaf_hashes": list(self.leaf_hashes),
            "levels": [list(lvl) for lvl in self.levels],
        }


def verify_merkle_proof(
    leaf_hash: str,
    proof: Sequence[Union[MerkleProofStep, Mapping[str, str]]],
    expected_root: str,
) -> bool:
    """Verifica criptográficamente si una hoja pertenece al `expected_root`."""
    current = leaf_hash
    for step in proof:
        if isinstance(step, MerkleProofStep):
            pos, sibling = step.position, step.hash
        else:
            pos, sibling = step["position"], step["hash"]
        if pos == "right":
            current = hash_merkle_pair(current, sibling)
        elif pos == "left":
            current = hash_merkle_pair(sibling, current)
        else:
            raise ValueError(f"Posición de prueba Merkle inválida: {pos!r}")
    return current == expected_root


@dataclass
class AttestationNode:
    """Nodo inmutable en esencia dentro del Grafo de Attestations de 7 niveles."""

    node_id: str
    level: int
    level_name: str
    parent_ids: List[str]
    source_classification: str
    timestamp: str
    payload: Dict[str, Any]
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.level not in LEVEL_NAMES:
            raise ValueError(f"Nivel de attestation inválido: {self.level}. Debe estar entre 1 y 7.")
        expected_name = LEVEL_NAMES[self.level]
        if self.level_name != expected_name:
            raise ValueError(
                f"El nivel {self.level} requiere level_name={expected_name!r}, recibido {self.level_name!r}."
            )
        if self.source_classification not in VALID_SOURCE_CLASSIFICATIONS:
            raise ValueError(
                f"Clasificación de fuente inválida: {self.source_classification!r}. "
                f"Válidas: {sorted(VALID_SOURCE_CLASSIFICATIONS)}"
            )
        if not self.content_hash:
            self.content_hash = self.compute_content_hash()

    def hashable_body(self) -> Dict[str, Any]:
        """Estructura canónica exacta usada para calcular `content_hash` y hojas Merkle."""
        return {
            "node_id": self.node_id,
            "level": self.level,
            "level_name": self.level_name,
            "parent_ids": list(self.parent_ids),
            "source_classification": self.source_classification,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def compute_content_hash(self) -> str:
        """Recalcula el hash SHA-256 canónico del nodo."""
        return sha256_hex(self.hashable_body())

    def is_hash_valid(self) -> bool:
        """Verifica que el `content_hash` almacenado coincida con el contenido actual."""
        return self.content_hash == self.compute_content_hash()

    def to_dict(self) -> Dict[str, Any]:
        """Convierte el nodo a diccionario serializable."""
        return {
            "node_id": self.node_id,
            "level": self.level,
            "level_name": self.level_name,
            "level_label_es": LEVEL_LABELS_ES[self.level],
            "parent_ids": list(self.parent_ids),
            "source_classification": self.source_classification,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AttestationNode":
        """Reconstruye un `AttestationNode` desde un diccionario."""
        return cls(
            node_id=str(data["node_id"]),
            level=int(data["level"]),
            level_name=str(data["level_name"]),
            parent_ids=list(data.get("parent_ids", [])),
            source_classification=str(data["source_classification"]),
            timestamp=str(data["timestamp"]),
            payload=dict(data.get("payload", {})),
            content_hash=str(data.get("content_hash", "")),
        )


def build_evidence_tree(
    items: Sequence[Union[AttestationNode, Mapping[str, Any], str]],
) -> EvidenceTree:
    """Construye el árbol binario de Merkle y calcula el `evidence_root`.

    Acepta nodos `AttestationNode` (ordenados deterministamente por `(level, node_id)`),
    diccionarios arbitrarios (hasheados mediante `canonical_json`) o hashes hexadecimales
    de 64 caracteres ya calculados.
    """
    if not items:
        empty_hash = sha256_hex("")
        return EvidenceTree(
            evidence_root=empty_hash,
            leaf_hashes=(),
            leaf_ids=(),
            levels=((empty_hash,),),
        )

    leaf_entries: List[Tuple[str, str]] = []
    if all(isinstance(item, AttestationNode) for item in items):
        sorted_nodes = sorted(items, key=lambda n: (n.level, n.node_id))  # type: ignore[arg-type]
        for node in sorted_nodes:
            assert isinstance(node, AttestationNode)
            leaf_entries.append((node.node_id, node.compute_content_hash()))
    else:
        for idx, item in enumerate(items):
            if isinstance(item, AttestationNode):
                leaf_entries.append((item.node_id, item.compute_content_hash()))
            elif isinstance(item, str):
                leaf_entries.append((f"leaf-{idx}", item))
            else:
                item_id = str(item.get("node_id") or item.get("id") or f"leaf-{idx}")
                leaf_entries.append((item_id, sha256_hex(item)))

    leaf_ids = tuple(entry[0] for entry in leaf_entries)
    leaf_hashes = tuple(entry[1] for entry in leaf_entries)

    levels: List[Tuple[str, ...]] = [leaf_hashes]
    current = list(leaf_hashes)
    while len(current) > 1:
        if len(current) % 2 == 1:
            current.append(current[-1])
        next_level: List[str] = []
        for i in range(0, len(current), 2):
            next_level.append(hash_merkle_pair(current[i], current[i + 1]))
        levels.append(tuple(next_level))
        current = next_level

    return EvidenceTree(
        evidence_root=current[0],
        leaf_hashes=leaf_hashes,
        leaf_ids=leaf_ids,
        levels=tuple(levels),
    )


def compute_evidence_root(
    source: Union["AttestationGraph", Sequence[Union[AttestationNode, Mapping[str, Any], str]]],
) -> str:
    """Calcula el `evidence_root` de un `AttestationGraph` o secuencia de evidencias."""
    if isinstance(source, AttestationGraph):
        return source.build_evidence_tree().evidence_root
    return build_evidence_tree(source).evidence_root


@dataclass
class AttestationGraph:
    """Grafo de Attestations de 7 niveles que conecta fuente hasta liquidación."""

    graph_id: str
    nodes: Dict[str, AttestationNode] = field(default_factory=dict)

    def add_node(
        self,
        *,
        node_id: str,
        level: int,
        parent_ids: Sequence[str],
        source_classification: str,
        timestamp: str,
        payload: Mapping[str, Any],
    ) -> AttestationNode:
        """Crea y registra un nodo validando unicidad, nivel y padres existentes."""
        if node_id in self.nodes:
            raise ValueError(f"Ya existe un nodo con node_id={node_id!r} en el grafo.")
        for pid in parent_ids:
            if pid not in self.nodes:
                raise ValueError(f"El nodo padre {pid!r} no existe en el grafo antes de añadir {node_id!r}.")
            if self.nodes[pid].level >= level:
                raise ValueError(
                    f"Jerarquía inválida: padre {pid!r} (nivel {self.nodes[pid].level}) "
                    f"debe ser estrictamente menor al nivel {level} de {node_id!r}."
                )

        node = AttestationNode(
            node_id=node_id,
            level=int(level),
            level_name=LEVEL_NAMES[int(level)],
            parent_ids=list(parent_ids),
            source_classification=source_classification,
            timestamp=timestamp,
            payload=dict(payload),
        )
        self.nodes[node_id] = node
        return node

    def register_device_source(
        self,
        *,
        node_id: str,
        source_classification: str,
        timestamp: str,
        device_id: str,
        sensor_model: str,
        firmware_or_generator: str,
        location_label: str,
        declared_limitations: Sequence[str],
        extra_metadata: Optional[Mapping[str, Any]] = None,
    ) -> AttestationNode:
        """Nivel 1: Registra el dispositivo físico o fuente sintética declarada."""
        is_synthetic = source_classification == "synthetic_declared"
        payload: Dict[str, Any] = {
            "device_id": device_id,
            "sensor_model": sensor_model,
            "firmware_or_generator": firmware_or_generator,
            "is_synthetic": is_synthetic,
            "location_label": location_label,
            "declared_limitations": list(declared_limitations),
        }
        if extra_metadata:
            payload["metadata"] = dict(extra_metadata)
        return self.add_node(
            node_id=node_id,
            level=AttestationLevel.DEVICE_SOURCE,
            parent_ids=[],
            source_classification=source_classification,
            timestamp=timestamp,
            payload=payload,
        )

    def register_calibration(
        self,
        *,
        node_id: str,
        parent_ids: Sequence[str],
        source_classification: str,
        timestamp: str,
        calibrated_at: str,
        calibration_expires_at: str,
        sensor_status: str,
        reference_baseline: float,
        notes: str = "",
    ) -> AttestationNode:
        """Nivel 2: Registra la attestation de calibración y vigencia del sensor."""
        return self.add_node(
            node_id=node_id,
            level=AttestationLevel.CALIBRATION,
            parent_ids=parent_ids,
            source_classification=source_classification,
            timestamp=timestamp,
            payload={
                "calibrated_at": calibrated_at,
                "calibration_expires_at": calibration_expires_at,
                "sensor_status": sensor_status,
                "reference_baseline": reference_baseline,
                "notes": notes,
            },
        )

    def register_batch(
        self,
        *,
        node_id: str,
        parent_ids: Sequence[str],
        source_classification: str,
        timestamp: str,
        readings: Sequence[Mapping[str, Any]],
        expected_count: int,
        unit: str,
    ) -> AttestationNode:
        """Nivel 3: Registra un lote de lecturas crudas con su hash determinista."""
        readings_list = [dict(r) for r in readings]
        batch_hash = sha256_hex(readings_list)
        first_ts = readings_list[0].get("timestamp") if readings_list else None
        last_ts = readings_list[-1].get("timestamp") if readings_list else None
        return self.add_node(
            node_id=node_id,
            level=AttestationLevel.BATCH,
            parent_ids=parent_ids,
            source_classification=source_classification,
            timestamp=timestamp,
            payload={
                "readings": readings_list,
                "received_count": len(readings_list),
                "expected_count": expected_count,
                "unit": unit,
                "first_reading_at": first_ts,
                "last_reading_at": last_ts,
                "batch_hash": batch_hash,
            },
        )

    def register_dataset(
        self,
        *,
        node_id: str,
        parent_ids: Sequence[str],
        source_classification: str,
        timestamp: str,
        aggregation_window: str,
        reading_count: int,
        expected_count: int,
        coverage_ratio: float,
        mean_value: float,
        min_value: float,
        max_value: float,
        zones_summary: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> AttestationNode:
        """Nivel 4: Registra el dataset agregado con estadísticas y cobertura."""
        payload: Dict[str, Any] = {
            "aggregation_window": aggregation_window,
            "reading_count": reading_count,
            "expected_count": expected_count,
            "coverage_ratio": round(coverage_ratio, 6),
            "mean_value": round(mean_value, 4),
            "min_value": round(min_value, 4),
            "max_value": round(max_value, 4),
        }
        if zones_summary is not None:
            payload["zones_summary"] = [dict(z) for z in zones_summary]
        return self.add_node(
            node_id=node_id,
            level=AttestationLevel.DATASET,
            parent_ids=parent_ids,
            source_classification=source_classification,
            timestamp=timestamp,
            payload=payload,
        )

    def register_analysis(
        self,
        *,
        node_id: str,
        parent_ids: Sequence[str],
        source_classification: str,
        timestamp: str,
        methodology: str,
        baseline_value: float,
        peak_value: float,
        recovery_value: float,
        anomaly_detected: bool,
        anomaly_mitigated: bool,
        improvement_pct: float,
        extra_metrics: Optional[Mapping[str, Any]] = None,
    ) -> AttestationNode:
        """Nivel 5: Registra el cálculo de línea base, anomalías e indicadores."""
        payload: Dict[str, Any] = {
            "methodology": methodology,
            "baseline_value": round(baseline_value, 4),
            "peak_value": round(peak_value, 4),
            "recovery_value": round(recovery_value, 4),
            "anomaly_detected": anomaly_detected,
            "anomaly_mitigated": anomaly_mitigated,
            "improvement_pct": round(improvement_pct, 4),
        }
        if extra_metrics:
            payload.update(dict(extra_metrics))
        return self.add_node(
            node_id=node_id,
            level=AttestationLevel.ANALYSIS,
            parent_ids=parent_ids,
            source_classification=source_classification,
            timestamp=timestamp,
            payload=payload,
        )

    def register_policy_evaluation(
        self,
        *,
        node_id: str,
        parent_ids: Sequence[str],
        source_classification: str,
        timestamp: str,
        policy_version: str,
        policy_hash: str,
        status: str,
        reason: str,
        payout_eligible: bool,
        evidence_root: str,
        checks: Sequence[Mapping[str, Any]],
    ) -> AttestationNode:
        """Nivel 6: Registra el resultado determinista de evaluación del Policy Engine."""
        return self.add_node(
            node_id=node_id,
            level=AttestationLevel.POLICY_EVALUATION,
            parent_ids=parent_ids,
            source_classification=source_classification,
            timestamp=timestamp,
            payload={
                "policy_version": policy_version,
                "policy_hash": policy_hash,
                "status": status,
                "reason": reason,
                "payout_eligible": payout_eligible,
                "evidence_root": evidence_root,
                "checks": [dict(c) for c in checks],
            },
        )

    def register_settlement(
        self,
        *,
        node_id: str,
        parent_ids: Sequence[str],
        source_classification: str,
        timestamp: str,
        action: str,
        payout_eligible: bool,
        amount_micro_ccd: int,
        recipient_account: str,
        contract_index: int,
        contract_subindex: int,
        idempotency_key: str,
        tx_hash: Optional[str] = None,
        rejection_reason: Optional[str] = None,
    ) -> AttestationNode:
        """Nivel 7: Registra la liquidación (recompensa o rechazo) asociada a la evaluación."""
        return self.add_node(
            node_id=node_id,
            level=AttestationLevel.SETTLEMENT,
            parent_ids=parent_ids,
            source_classification=source_classification,
            timestamp=timestamp,
            payload={
                "action": action,
                "payout_eligible": payout_eligible,
                "amount_micro_ccd": amount_micro_ccd if payout_eligible else 0,
                "recipient_account": recipient_account,
                "contract_index": contract_index,
                "contract_subindex": contract_subindex,
                "idempotency_key": idempotency_key,
                "tx_hash": tx_hash,
                "rejection_reason": rejection_reason,
            },
        )

    def get_nodes_by_level(self, level: int) -> List[AttestationNode]:
        """Retorna todos los nodos pertenecientes a un nivel dado."""
        return sorted(
            (n for n in self.nodes.values() if n.level == level),
            key=lambda n: n.node_id,
        )

    def get_lineage(self, target_node_id: str) -> List[AttestationNode]:
        """Recorre el grafo hacia atrás desde `target_node_id` hasta el Nivel 1."""
        if target_node_id not in self.nodes:
            raise KeyError(f"Nodo no encontrado en el grafo: {target_node_id}")

        visited: Dict[str, AttestationNode] = {}

        def _dfs(nid: str) -> None:
            if nid in visited:
                return
            node = self.nodes[nid]
            for pid in node.parent_ids:
                if pid in self.nodes:
                    _dfs(pid)
            visited[nid] = node

        _dfs(target_node_id)
        return sorted(visited.values(), key=lambda n: (n.level, n.node_id))

    def verify_integrity(self) -> Tuple[bool, List[str]]:
        """Verifica hashes internos, consistencia de lotes y referencias entre nodos."""
        errors: List[str] = []
        for node in sorted(self.nodes.values(), key=lambda n: (n.level, n.node_id)):
            if not node.is_hash_valid():
                errors.append(
                    f"Hash alterado en nodo {node.node_id} (nivel {node.level} - {node.level_name})."
                )
            for pid in node.parent_ids:
                if pid not in self.nodes:
                    errors.append(f"Referencia padre inexistente {pid!r} en nodo {node.node_id}.")
                elif self.nodes[pid].level >= node.level:
                    errors.append(
                        f"Jerarquía inválida entre padre {pid!r} y nodo {node.node_id}."
                    )
            if node.level == AttestationLevel.BATCH:
                readings = node.payload.get("readings", [])
                expected_batch_hash = sha256_hex(readings)
                if node.payload.get("batch_hash") != expected_batch_hash:
                    errors.append(
                        f"Hash de lote manipulado en nodo {node.node_id}: "
                        f"esperado {expected_batch_hash}, almacenado {node.payload.get('batch_hash')}."
                    )
        return len(errors) == 0, errors

    def build_evidence_tree(self, max_level: Optional[int] = None) -> EvidenceTree:
        """Construye el árbol de evidencias sobre los nodos del grafo."""
        selected = [
            node
            for node in self.nodes.values()
            if max_level is None or node.level <= max_level
        ]
        return build_evidence_tree(selected)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el grafo completo, incluyendo nodos ordenados y su `evidence_root`."""
        ordered_nodes = sorted(self.nodes.values(), key=lambda n: (n.level, n.node_id))
        tree = build_evidence_tree(ordered_nodes)
        return {
            "graph_id": self.graph_id,
            "evidence_root": tree.evidence_root,
            "nodes": [n.to_dict() for n in ordered_nodes],
            "evidence_tree": tree.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AttestationGraph":
        """Reconstruye un `AttestationGraph` desde su representación en diccionario."""
        graph = cls(graph_id=str(data["graph_id"]))
        for raw_node in data.get("nodes", []):
            node = AttestationNode.from_dict(raw_node)
            graph.nodes[node.node_id] = node
        return graph
