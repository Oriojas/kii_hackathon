"""Pruebas unitarias para `vera_core` (Attestations, PolicyEngine, Escenarios y Agente)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from vera_core import (
    STATUS_BLOCKED,
    STATUS_ELIGIBLE,
    STATUS_INCOMPLETE,
    STATUS_TAMPERED,
    AttestationLevel,
    PolicyEngine,
    VeraAgent,
    VeraConfig,
    build_evidence_tree,
    canonical_json,
    compute_evidence_root,
    compute_policy_hash,
    get_business_scenario,
    get_home_air_scenario,
    verify_merkle_proof,
)


TEST_ENV = {
    "VERA_AGENT_ID": "vera-test-agent",
    "VERA_POLICY_VERSION": "vera-policy-v1",
    "VERA_REFERENCE_TIME": "2026-11-25T12:00:00Z",
    "VERA_MIN_COVERAGE_RATIO": "0.85",
    "VERA_EXPECTED_BATCH_READINGS": "48",
    "VERA_MAX_CALIBRATION_AGE_HOURS": "720",
    "VERA_REQUIRE_RECOVERY_FOR_ANOMALY": "true",
    "VERA_HOME_DEVICE_ID": "esp32-mq135-test",
    "VERA_HOME_SENSOR_MODEL": "MQ-135",
    "VERA_HOME_AIR_BASELINE_RAW": "325.0",
    "VERA_HOME_AIR_BASELINE_MAX_RAW": "380.0",
    "VERA_HOME_AIR_ANOMALY_PEAK_RAW": "650.0",
    "VERA_HOME_AIR_ANOMALY_MIN_RAW": "520.0",
    "VERA_HOME_AIR_RECOVERY_RAW": "335.0",
    "VERA_HOME_AIR_RECOVERY_MAX_RAW": "390.0",
    "VERA_BUSINESS_ORG_ID": "org-test-01",
    "VERA_BUSINESS_ZONES": "Oficinas,Producción,Bodega,Logística",
    "VERA_BUSINESS_GRID_EMISSION_FACTOR_KG_PER_KWH": "0.164",
    "VERA_BUSINESS_MAX_INTENSITY_KG_PER_UNIT": "0.45",
    "VERA_BUSINESS_MIN_REDUCTION_PCT": "5.0",
    "VERA_BUSINESS_ZONE_BASELINE_KWH": "1200,4800,1600,2400",
    "VERA_BUSINESS_ZONE_CURRENT_KWH": "1050,4200,1440,2100",
    "VERA_BUSINESS_ZONE_UNITS": "900,2100,1100,1450",
    "VERA_REWARD_AMOUNT_MICRO_CCD": "1000000",
    "VERA_BUSINESS_REWARD_AMOUNT_MICRO_CCD": "2500000",
    "CONCORDIUM_NETWORK": "testnet",
    "CONCORDIUM_AGENT_ACCOUNT_ADDRESS": "4xTestAccountAddressConcordiumExampleOnly111111111",
    "CONCORDIUM_REWARD_POOL_INDEX": "1001",
    "CONCORDIUM_REWARD_POOL_SUBINDEX": "0",
}


class TestVeraCoreConfig(unittest.TestCase):
    """Verifica carga parametrizada desde variables de entorno."""

    def test_config_reads_environment_variables(self) -> None:
        custom_env = dict(TEST_ENV)
        custom_env["VERA_MIN_COVERAGE_RATIO"] = "0.92"
        custom_env["VERA_HOME_DEVICE_ID"] = "esp32-custom-99"
        with patch.dict(os.environ, custom_env, clear=False):
            cfg = VeraConfig.from_env(load_dotenv_file=False)
            self.assertEqual(cfg.min_coverage_ratio, 0.92)
            self.assertEqual(cfg.home_device_id, "esp32-custom-99")
            self.assertEqual(cfg.business_zones, ("Oficinas", "Producción", "Bodega", "Logística"))


class TestScenarios(unittest.TestCase):
    """Verifica la generación reproducible de los escenarios Home Air y Business."""

    def setUp(self) -> None:
        self.env_patcher = patch.dict(os.environ, TEST_ENV, clear=False)
        self.env_patcher.start()
        self.config = VeraConfig.from_env(load_dotenv_file=False)

    def tearDown(self) -> None:
        self.env_patcher.stop()

    def test_home_air_scenario_valid_48_readings(self) -> None:
        bundle = get_home_air_scenario(expired_calibration=False, config=self.config)
        self.assertEqual(bundle.scenario_type, "home_air")
        self.assertEqual(bundle.source_classification, "physical")
        self.assertEqual(len(bundle.readings), 48)

        phases = {r["phase"] for r in bundle.readings}
        self.assertEqual(phases, {"baseline", "anomaly_peak", "intervention", "recovery"})
        self.assertTrue(all(r["unit"] == "adc_raw" for r in bundle.readings))
        self.assertTrue(all(r["is_certified_co2"] is False for r in bundle.readings))

        baseline_vals = [r["raw_signal"] for r in bundle.readings if r["phase"] == "baseline"]
        peak_vals = [r["raw_signal"] for r in bundle.readings if r["phase"] == "anomaly_peak"]
        recovery_vals = [r["raw_signal"] for r in bundle.readings if r["phase"] == "recovery"]

        self.assertLess(max(baseline_vals), self.config.home_air_baseline_max_raw)
        self.assertGreaterEqual(max(peak_vals), self.config.home_air_anomaly_min_raw)
        self.assertLessEqual(sum(recovery_vals) / len(recovery_vals), self.config.home_air_recovery_max_raw)
        self.assertFalse(bundle.metadata["expired_calibration"])

    def test_home_air_scenario_expired_calibration(self) -> None:
        bundle = get_home_air_scenario(expired_calibration=True, config=self.config)
        self.assertEqual(len(bundle.readings), 48)
        self.assertTrue(bundle.metadata["expired_calibration"])
        cal_nodes = bundle.graph.get_nodes_by_level(AttestationLevel.CALIBRATION)
        self.assertEqual(len(cal_nodes), 1)
        self.assertEqual(cal_nodes[0].payload["sensor_status"], "EXPIRED")

    def test_business_scenario_four_synthetic_zones(self) -> None:
        bundle = get_business_scenario(config=self.config)
        self.assertEqual(bundle.scenario_type, "business")
        self.assertEqual(bundle.source_classification, "synthetic_declared")
        self.assertTrue(bundle.metadata["is_synthetic"])
        self.assertEqual(len(bundle.zones), 4)

        zone_names = [z["zone_name"] for z in bundle.zones]
        self.assertEqual(zone_names, ["Oficinas", "Producción", "Bodega", "Logística"])
        for zone in bundle.zones:
            self.assertGreater(zone["kwh"], 0)
            self.assertGreater(zone["scope2_emissions_kg"], 0)
            self.assertGreater(zone["units_produced"], 0)
            self.assertGreater(zone["intensity_kg_per_unit"], 0)
            self.assertTrue(zone["is_synthetic"])


class TestAttestationsAndMerkleParity(unittest.TestCase):
    """Verifica el grafo de 7 niveles, Merkle proofs y paridad con JS/TS."""

    def setUp(self) -> None:
        self.env_patcher = patch.dict(os.environ, TEST_ENV, clear=False)
        self.env_patcher.start()
        self.config = VeraConfig.from_env(load_dotenv_file=False)

    def tearDown(self) -> None:
        self.env_patcher.stop()

    def test_seven_level_attestation_lineage_and_merkle_proof(self) -> None:
        bundle = get_home_air_scenario(expired_calibration=False, config=self.config)
        engine = PolicyEngine(config=self.config)
        result = engine.evaluate(
            bundle.graph,
            scenario_type="home_air",
            evaluation_timestamp=bundle.evaluation_timestamp,
            expected_evidence_root=bundle.expected_evidence_root,
            record_in_graph=True,
            tx_hash="0xconcordium_demo_tx_01",
        )
        self.assertEqual(result.status, STATUS_ELIGIBLE)
        self.assertEqual(len(bundle.graph.nodes), 7)

        settlement_nodes = bundle.graph.get_nodes_by_level(AttestationLevel.SETTLEMENT)
        self.assertEqual(len(settlement_nodes), 1)
        lineage = bundle.graph.get_lineage(settlement_nodes[0].node_id)
        self.assertEqual([n.level for n in lineage], [1, 2, 3, 4, 5, 6, 7])

        tree = bundle.graph.build_evidence_tree()
        for node in lineage:
            proof = tree.get_proof_by_id(node.node_id)
            self.assertTrue(verify_merkle_proof(node.content_hash, proof, tree.evidence_root))

    def test_evidence_root_and_policy_hash_match_nodejs(self) -> None:
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js no está instalado en el entorno actual.")

        bundle = get_business_scenario(config=self.config)
        engine = PolicyEngine(config=self.config)
        policy_params = engine.get_policy_parameters("business")
        py_policy_hash = compute_policy_hash(policy_params)
        py_tree = bundle.graph.build_evidence_tree()
        py_evidence_root = compute_evidence_root(bundle.graph)
        self.assertEqual(py_tree.evidence_root, py_evidence_root)

        ordered_bodies = [
            n.hashable_body()
            for n in sorted(bundle.graph.nodes.values(), key=lambda x: (x.level, x.node_id))
        ]
        payload_for_js = {
            "policy_params": policy_params,
            "leaf_bodies": ordered_bodies,
            "sample_canonical": canonical_json({"b": 1.0, "a": {"z": 2, "y": [3.0, 4.5]}}),
        }

        js_script = """
const crypto = require('crypto');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));

function canonicalize(val) {
  if (val === null || typeof val !== 'object') return val;
  if (Array.isArray(val)) return val.map(canonicalize);
  const out = {};
  for (const k of Object.keys(val).sort()) {
    out[k] = canonicalize(val[k]);
  }
  return out;
}

function canonicalJson(val) {
  return JSON.stringify(canonicalize(val));
}

function sha256Hex(str) {
  return crypto.createHash('sha256').update(str, 'utf8').digest('hex');
}

function buildEvidenceRoot(leafBodies) {
  let level = leafBodies.map(b => sha256Hex(canonicalJson(b)));
  while (level.length > 1) {
    if (level.length % 2 === 1) level.push(level[level.length - 1]);
    const next = [];
    for (let i = 0; i < level.length; i += 2) {
      next.push(sha256Hex(level[i] + level[i + 1]));
    }
    level = next;
  }
  return level[0];
}

console.log(JSON.stringify({
  policy_hash: sha256Hex(canonicalJson(input.policy_params)),
  evidence_root: buildEvidenceRoot(input.leaf_bodies),
  sample_canonical: canonicalJson({b: 1.0, a: {z: 2, y: [3.0, 4.5]}}),
}));
"""
        proc = subprocess.run(
            [node_bin, "-e", js_script],
            input=json.dumps(payload_for_js),
            text=True,
            capture_output=True,
            check=True,
        )
        js_out = json.loads(proc.stdout)
        self.assertEqual(py_policy_hash, js_out["policy_hash"])
        self.assertEqual(py_evidence_root, js_out["evidence_root"])
        self.assertEqual(payload_for_js["sample_canonical"], js_out["sample_canonical"])

    def test_bridge_test_vectors_ts_exact_parity(self) -> None:
        npm_bin = shutil.which("npm")
        bridge_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "concordium-bridge")
        vectors_path = os.path.join(bridge_dir, "src", "test_vectors.ts")
        if not npm_bin or not os.path.isfile(vectors_path):
            self.skipTest("Bridge TypeScript no disponible para verificación cruzada.")

        home_air_records = [
            {
                "batch_id": "home-air-batch-001",
                "seq": 1,
                "timestamp": "2026-11-25T18:00:00Z",
                "source_type": "physical_sensor",
                "device_id": "mq135-node-01",
                "raw_adc": 312,
                "baseline_adc": 300,
                "anomaly_detected": False,
                "calibration_valid": True,
            },
            {
                "batch_id": "home-air-batch-001",
                "seq": 2,
                "timestamp": "2026-11-25T18:05:00Z",
                "source_type": "physical_sensor",
                "device_id": "mq135-node-01",
                "raw_adc": 645,
                "baseline_adc": 300,
                "anomaly_detected": True,
                "calibration_valid": True,
            },
            {
                "batch_id": "home-air-batch-001",
                "seq": 3,
                "timestamp": "2026-11-25T18:10:00Z",
                "source_type": "physical_sensor",
                "device_id": "mq135-node-01",
                "raw_adc": 340,
                "baseline_adc": 300,
                "anomaly_detected": False,
                "calibration_valid": True,
            },
        ]
        business_policy = {
            "policy_id": "vera-business-scope2-v1",
            "version": "1.0.0",
            "rules": {
                "require_valid_calibration": True,
                "min_coverage_ratio": 0.95,
                "max_intensity_kg_co2e_per_unit": 1.25,
                "reject_replay_batch": True,
            },
            "allowed_sources": ["physical_sensor", "synthetic_declared", "imported"],
            "settlement": {
                "idempotent": True,
                "currency": "CCD",
            },
        }

        proc = subprocess.run(
            [npm_bin, "run", "--silent", "test:vectors"],
            cwd=bridge_dir,
            text=True,
            capture_output=True,
            check=True,
        )
        stdout = proc.stdout
        self.assertIn(compute_evidence_root([]), stdout)
        self.assertIn(compute_evidence_root([home_air_records[0]]), stdout)
        self.assertIn(compute_evidence_root(home_air_records), stdout)
        self.assertIn(compute_policy_hash(business_policy), stdout)


class TestPolicyEngineAndAgent(unittest.TestCase):
    """Pruebas unitarias para los 4 estados del PolicyEngine y salidas de VeraAgent."""

    def setUp(self) -> None:
        self.env_patcher = patch.dict(os.environ, TEST_ENV, clear=False)
        self.env_patcher.start()
        self.config = VeraConfig.from_env(load_dotenv_file=False)
        self.engine = PolicyEngine(config=self.config)
        self.agent = VeraAgent(config=self.config)

    def tearDown(self) -> None:
        self.env_patcher.stop()

    def test_case_1_eligible_home_air_and_business(self) -> None:
        home_bundle = get_home_air_scenario(expired_calibration=False, config=self.config)
        home_eval = self.engine.evaluate(
            home_bundle.graph,
            scenario_type="home_air",
            evaluation_timestamp=home_bundle.evaluation_timestamp,
            expected_evidence_root=home_bundle.expected_evidence_root,
            idempotency_key="home-valid-1",
            record_in_graph=True,
        )
        self.assertEqual(home_eval.status, STATUS_ELIGIBLE)
        self.assertTrue(home_eval.payout_eligible)

        biz_bundle = get_business_scenario(variant="eligible", config=self.config)
        biz_eval = self.engine.evaluate(
            biz_bundle.graph,
            scenario_type="business",
            evaluation_timestamp=biz_bundle.evaluation_timestamp,
            expected_evidence_root=biz_bundle.expected_evidence_root,
            idempotency_key="biz-valid-1",
            record_in_graph=True,
        )
        self.assertEqual(biz_eval.status, STATUS_ELIGIBLE)
        self.assertTrue(biz_eval.payout_eligible)

        # Verifica reporte voluntario de VeraAgent
        report = self.agent.generate_voluntary_report(biz_eval, biz_bundle.graph)
        self.assertTrue(report.is_synthetic)
        self.assertEqual(report.policy_status, STATUS_ELIGIBLE)
        self.assertEqual(len(report.lineage), 7)
        md = report.to_markdown()
        self.assertIn("Oficinas", md)
        self.assertIn("Sintético declarado", md)

    def test_case_2_incomplete_coverage_and_missing_zone(self) -> None:
        home_incomplete = get_home_air_scenario(variant="incomplete", config=self.config)
        res_home = self.engine.evaluate(
            home_incomplete.graph,
            scenario_type="home_air",
            evaluation_timestamp=home_incomplete.evaluation_timestamp,
            expected_evidence_root=home_incomplete.expected_evidence_root,
        )
        self.assertEqual(res_home.status, STATUS_INCOMPLETE)
        self.assertFalse(res_home.payout_eligible)

        biz_incomplete = get_business_scenario(variant="incomplete", config=self.config)
        res_biz = self.engine.evaluate(
            biz_incomplete.graph,
            scenario_type="business",
            evaluation_timestamp=biz_incomplete.evaluation_timestamp,
            expected_evidence_root=biz_incomplete.expected_evidence_root,
        )
        self.assertEqual(res_biz.status, STATUS_INCOMPLETE)
        self.assertFalse(res_biz.payout_eligible)

        explanation = self.agent.explain_decision(res_biz, biz_incomplete.graph)
        self.assertIn("INCOMPLETO", explanation)
        mitigations = self.agent.suggest_mitigations(res_biz, biz_incomplete.graph)
        self.assertTrue(any("zonas" in m.lower() for m in mitigations))

    def test_case_3_tampered_evidence(self) -> None:
        home_tampered = get_home_air_scenario(variant="tampered", config=self.config)
        res_home = self.engine.evaluate(
            home_tampered.graph,
            scenario_type="home_air",
            evaluation_timestamp=home_tampered.evaluation_timestamp,
            expected_evidence_root=home_tampered.expected_evidence_root,
        )
        self.assertEqual(res_home.status, STATUS_TAMPERED)
        self.assertFalse(res_home.payout_eligible)

        # También si el evidence_root esperado no coincide aunque los hashes internos fueran recalculados
        valid_bundle = get_home_air_scenario(expired_calibration=False, config=self.config)
        res_mismatch = self.engine.evaluate(
            valid_bundle.graph,
            scenario_type="home_air",
            evaluation_timestamp=valid_bundle.evaluation_timestamp,
            expected_evidence_root="0" * 64,
        )
        self.assertEqual(res_mismatch.status, STATUS_TAMPERED)
        self.assertFalse(res_mismatch.payout_eligible)

        explanation = self.agent.explain_decision(res_home, home_tampered.graph)
        self.assertIn("MANIPULADO", explanation)

    def test_case_4_blocked_expired_calibration_replay_and_thresholds(self) -> None:
        # 4a. Calibración expirada en Home Air
        home_expired = get_home_air_scenario(expired_calibration=True, config=self.config)
        res_expired = self.engine.evaluate(
            home_expired.graph,
            scenario_type="home_air",
            evaluation_timestamp=home_expired.evaluation_timestamp,
            expected_evidence_root=home_expired.expected_evidence_root,
            record_in_graph=True,
        )
        self.assertEqual(res_expired.status, STATUS_BLOCKED)
        self.assertFalse(res_expired.payout_eligible)
        self.assertIn("expirada", res_expired.reason.lower())

        # 4b. Anomalía sin recuperación en Home Air
        home_unmitigated = get_home_air_scenario(variant="unmitigated_anomaly", config=self.config)
        res_unmitigated = self.engine.evaluate(
            home_unmitigated.graph,
            scenario_type="home_air",
            evaluation_timestamp=home_unmitigated.evaluation_timestamp,
            expected_evidence_root=home_unmitigated.expected_evidence_root,
        )
        self.assertEqual(res_unmitigated.status, STATUS_BLOCKED)
        self.assertFalse(res_unmitigated.payout_eligible)

        # 4c. Escenario Business fuera de umbrales de intensidad/reducción
        biz_blocked = get_business_scenario(variant="blocked", config=self.config)
        res_biz_blocked = self.engine.evaluate(
            biz_blocked.graph,
            scenario_type="business",
            evaluation_timestamp=biz_blocked.evaluation_timestamp,
            expected_evidence_root=biz_blocked.expected_evidence_root,
        )
        self.assertEqual(res_biz_blocked.status, STATUS_BLOCKED)
        self.assertFalse(res_biz_blocked.payout_eligible)

        # 4d. Bloqueo por replay (misma clave de idempotencia)
        valid_home = get_home_air_scenario(expired_calibration=False, config=self.config)
        first = self.engine.evaluate(
            valid_home.graph,
            scenario_type="home_air",
            evaluation_timestamp=valid_home.evaluation_timestamp,
            expected_evidence_root=valid_home.expected_evidence_root,
            idempotency_key="replay-key-1",
            record_in_graph=True,
        )
        self.assertEqual(first.status, STATUS_ELIGIBLE)
        second = self.engine.evaluate(
            valid_home.graph,
            scenario_type="home_air",
            evaluation_timestamp=valid_home.evaluation_timestamp,
            expected_evidence_root=valid_home.expected_evidence_root,
            idempotency_key="replay-key-1",
        )
        self.assertEqual(second.status, STATUS_BLOCKED)
        self.assertFalse(second.payout_eligible)

        explanation = self.agent.explain_decision(res_expired, home_expired.graph)
        self.assertIn("BLOQUEADO", explanation)
        mitigations = self.agent.suggest_mitigations(res_expired, home_expired.graph)
        self.assertTrue(any("recalibrar" in m.lower() for m in mitigations))


if __name__ == "__main__":
    unittest.main()
