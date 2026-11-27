"""Pruebas unitarias e integración API para escenario CSV+GPS, endpoints y bridge Concordium."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from vera_core import (
    STATUS_BLOCKED,
    STATUS_ELIGIBLE,
    STATUS_INCOMPLETE,
    STATUS_TAMPERED,
    AttestationLevel,
    PolicyEngine,
    VeraConfig,
    get_imported_gps_scenario,
)


TEST_ENV = {
    "VERA_AGENT_ID": "vera-test-agent",
    "VERA_POLICY_VERSION": "vera-policy-v1",
    "VERA_REFERENCE_TIME": "2026-11-25T12:00:00Z",
    "VERA_MIN_COVERAGE_RATIO": "0.85",
    "VERA_EXPECTED_BATCH_READINGS": "48",
    "VERA_MAX_CALIBRATION_AGE_HOURS": "720",
    "VERA_REQUIRE_RECOVERY_FOR_ANOMALY": "true",
    "CONCORDIUM_NETWORK": "testnet",
    "CONCORDIUM_AGENT_ACCOUNT_ADDRESS": "4xTestAccountAddressConcordiumExampleOnly111111111",
    "CONCORDIUM_EVIDENCE_REGISTRY_INDEX": "1000",
    "CONCORDIUM_REWARD_POOL_INDEX": "1001",
    "CONCORDIUM_CIS8004_INDEX": "1002",
}


class TestImportedGpsScenario(unittest.TestCase):
    """Verifica `get_imported_gps_scenario` y su grafo de 7 niveles."""

    def setUp(self) -> None:
        self.env_patcher = patch.dict(os.environ, TEST_ENV, clear=False)
        self.env_patcher.start()
        self.config = VeraConfig.from_env(load_dotenv_file=False)
        self.engine = PolicyEngine(config=self.config)

    def tearDown(self) -> None:
        self.env_patcher.stop()

    def test_imported_gps_valid_preserves_coords_and_seven_levels(self) -> None:
        bundle = get_imported_gps_scenario(config=self.config)
        self.assertEqual(bundle.scenario_type, "imported_gps")
        self.assertEqual(bundle.source_classification, "imported")
        self.assertEqual(bundle.metadata["source_type"], "imported")
        self.assertEqual(len(bundle.readings), 45)

        first = bundle.readings[0]
        self.assertEqual(first["source_type"], "imported")
        self.assertIn("lat", first)
        self.assertIn("lon", first)
        self.assertEqual(first["raw_signal"], first["co2"])
        self.assertAlmostEqual(first["lat"], 4.6144275)
        self.assertAlmostEqual(first["lon"], -74.1106485)
        self.assertEqual(first["raw_signal"], 340.0)

        result = self.engine.evaluate(
            bundle.graph,
            scenario_type=bundle.scenario_type,
            evaluation_timestamp=bundle.evaluation_timestamp,
            expected_evidence_root=bundle.expected_evidence_root,
            record_in_graph=True,
        )
        self.assertEqual(result.status, STATUS_ELIGIBLE)
        self.assertTrue(result.payout_eligible)
        self.assertEqual(len(bundle.graph.nodes), 7)

        settlement_nodes = bundle.graph.get_nodes_by_level(AttestationLevel.SETTLEMENT)
        self.assertEqual(len(settlement_nodes), 1)
        lineage = bundle.graph.get_lineage(settlement_nodes[0].node_id)
        self.assertEqual([n.level for n in lineage], [1, 2, 3, 4, 5, 6, 7])

    def test_imported_gps_variants_tampered_incomplete_expired(self) -> None:
        tampered = get_imported_gps_scenario(tampered=True, config=self.config)
        res_tampered = self.engine.evaluate(
            tampered.graph,
            scenario_type=tampered.scenario_type,
            evaluation_timestamp=tampered.evaluation_timestamp,
            expected_evidence_root=tampered.expected_evidence_root,
        )
        self.assertEqual(res_tampered.status, STATUS_TAMPERED)
        self.assertFalse(res_tampered.payout_eligible)

        incomplete = get_imported_gps_scenario(incomplete=True, config=self.config)
        res_incomplete = self.engine.evaluate(
            incomplete.graph,
            scenario_type=incomplete.scenario_type,
            evaluation_timestamp=incomplete.evaluation_timestamp,
            expected_evidence_root=incomplete.expected_evidence_root,
        )
        self.assertEqual(res_incomplete.status, STATUS_INCOMPLETE)
        self.assertFalse(res_incomplete.payout_eligible)

        expired = get_imported_gps_scenario(expired_calibration=True, config=self.config)
        res_expired = self.engine.evaluate(
            expired.graph,
            scenario_type=expired.scenario_type,
            evaluation_timestamp=expired.evaluation_timestamp,
            expected_evidence_root=expired.expected_evidence_root,
        )
        self.assertEqual(res_expired.status, STATUS_BLOCKED)
        self.assertFalse(res_expired.payout_eligible)


class TestFastApiEndpoints(unittest.TestCase):
    """Verifica endpoints de FastAPI, bridge Concordium y persistencia SQLite."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_vera.db")
        env = dict(TEST_ENV)
        env["SQLITE_DB_PATH"] = self.db_path
        self.env_patcher = patch.dict(os.environ, env, clear=False)
        self.env_patcher.start()
        main.config = VeraConfig.from_env(load_dotenv_file=False)
        main.agent.config = main.config
        main.policy_engine = PolicyEngine(main.config)
        main.init_db(self.db_path)
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.tmp_dir.cleanup()

    def test_concordium_status_and_agent_card(self) -> None:
        resp_status = self.client.get("/api/concordium/status")
        self.assertEqual(resp_status.status_code, 200)
        data = resp_status.json()
        self.assertEqual(data["network"], "testnet")
        self.assertIn("contracts", data)
        self.assertIn("cis8004_badge", data)

        resp_card = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(resp_card.status_code, 200)
        card = resp_card.json()
        self.assertEqual(card["registrations"][0]["standard"], "CIS-8004")

        resp_home = self.client.get("/")
        self.assertEqual(resp_home.status_code, 200)

    def test_scenario_endpoints_return_all_readings_and_flags(self) -> None:
        # Home Air completo (48 lecturas, no solo [:5])
        r_home = self.client.get("/api/scenarios/home-air")
        self.assertEqual(r_home.status_code, 200)
        home_data = r_home.json()
        self.assertEqual(home_data["evaluation"]["status"], STATUS_ELIGIBLE)
        self.assertEqual(len(home_data["readings"]), 48)
        self.assertEqual(len(home_data["lineage"]), 7)

        for flag, expected_status in [
            ("expired=true", STATUS_BLOCKED),
            ("tampered=true", STATUS_TAMPERED),
            ("incomplete=true", STATUS_INCOMPLETE),
            ("replay=true", STATUS_BLOCKED),
        ]:
            res = self.client.get(f"/api/scenarios/home-air?{flag}")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["evaluation"]["status"], expected_status)

        # Business completo (4 zonas)
        r_biz = self.client.get("/api/scenarios/business")
        self.assertEqual(r_biz.status_code, 200)
        biz_data = r_biz.json()
        self.assertEqual(biz_data["evaluation"]["status"], STATUS_ELIGIBLE)
        self.assertEqual(len(biz_data["zones"]), 4)
        self.assertEqual(len(biz_data["readings"]), 4)

        for flag, expected_status in [
            ("expired=true", STATUS_BLOCKED),
            ("tampered=true", STATUS_TAMPERED),
            ("incomplete=true", STATUS_INCOMPLETE),
            ("replay=true", STATUS_BLOCKED),
        ]:
            res = self.client.get(f"/api/scenarios/business?{flag}")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["evaluation"]["status"], expected_status)

        # Imported GPS (45 lecturas con lat/lon y source_type=imported)
        r_gps = self.client.get("/api/scenarios/imported-gps")
        self.assertEqual(r_gps.status_code, 200)
        gps_data = r_gps.json()
        self.assertEqual(gps_data["evaluation"]["status"], STATUS_ELIGIBLE)
        self.assertEqual(gps_data["source_classification"], "imported")
        self.assertEqual(len(gps_data["readings"]), 45)
        self.assertIn("lat", gps_data["readings"][0])
        self.assertIn("lon", gps_data["readings"][0])

        for flag, expected_status in [
            ("expired=true", STATUS_BLOCKED),
            ("tampered=true", STATUS_TAMPERED),
            ("incomplete=true", STATUS_INCOMPLETE),
            ("replay=true", STATUS_BLOCKED),
        ]:
            res = self.client.get(f"/api/scenarios/imported-gps?{flag}")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["evaluation"]["status"], expected_status)

    def test_settle_endpoint_persists_in_sqlite_and_blocks_replay(self) -> None:
        # 1. Escenario bloqueado no liquida ni persiste pago
        r_blocked = self.client.post("/api/settle", json={"scenario": "home_air", "expired": True})
        self.assertEqual(r_blocked.status_code, 200)
        blocked_data = r_blocked.json()
        self.assertFalse(blocked_data["settled"])
        self.assertEqual(blocked_data["evaluation"]["status"], STATUS_BLOCKED)

        # 2. Escenario elegible liquida (simulate si no hay llave) y guarda en SQLite settlements
        r_ok = self.client.post("/api/settle", json={"scenario": "imported_gps"})
        self.assertEqual(r_ok.status_code, 200)
        ok_data = r_ok.json()
        self.assertTrue(ok_data["settled"])
        self.assertEqual(ok_data["evaluation"]["status"], STATUS_ELIGIBLE)
        self.assertIsNotNone(ok_data["tx_hash"])
        self.assertEqual(len(ok_data["lineage"]), 7)

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT evidence_id, status, tx_hash, amount FROM settlements"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], ok_data["evidence_id"])
        self.assertEqual(rows[0][2], ok_data["tx_hash"])

        # 3. Segundo POST idéntico es rechazado por protección anti-replay en SQLite
        r_replay = self.client.post("/api/settle", json={"scenario": "imported_gps"})
        self.assertEqual(r_replay.status_code, 200)
        replay_data = r_replay.json()
        self.assertFalse(replay_data["settled"])
        self.assertEqual(replay_data["evaluation"]["status"], STATUS_BLOCKED)

    def test_live_telemetry_empty_and_insufficient_returns_incomplete(self) -> None:
        # 1. SQLite vacía -> INCOMPLETE sin error 500
        r_empty = self.client.get("/api/scenarios/live-telemetry")
        self.assertEqual(r_empty.status_code, 200)
        empty_data = r_empty.json()
        self.assertEqual(empty_data["scenario"], "live_telemetry")
        self.assertEqual(empty_data["source_classification"], "physical")
        self.assertEqual(empty_data["evaluation"]["status"], STATUS_INCOMPLETE)
        self.assertFalse(empty_data["evaluation"]["payout_eligible"])
        self.assertEqual(empty_data["readings"], [])
        self.assertEqual(len(empty_data["lineage"]), 7)

        # 2. Menos de 10 lecturas vía POST /api/telemetry -> INCOMPLETE
        token = os.environ.get("API_AUTH_TOKEN", "your_secure_random_api_token_here")
        headers = {"Authorization": f"Bearer {token}"}
        for i in range(5):
            r_post = self.client.post(
                "/api/telemetry",
                headers=headers,
                json={
                    "device_id": "esp8266-test-01",
                    "raw_signal": 315.0 + i,
                    "lat": 4.6144,
                    "lon": -74.1106,
                    "sequence": i + 1,
                    "timestamp": 1764072000 + i * 60,
                },
            )
            self.assertEqual(r_post.status_code, 200)

        r_partial = self.client.get("/api/scenarios/live-telemetry")
        self.assertEqual(r_partial.status_code, 200)
        partial_data = r_partial.json()
        self.assertEqual(partial_data["evaluation"]["status"], STATUS_INCOMPLETE)
        self.assertEqual(len(partial_data["readings"]), 5)
        self.assertTrue(all(r["source_type"] == "physical" for r in partial_data["readings"]))

    def test_live_telemetry_eligible_blocked_and_settle_after_post_telemetry(self) -> None:
        token = os.environ.get("API_AUTH_TOKEN", "your_secure_random_api_token_here")
        headers = {"Authorization": f"Bearer {token}"}
        # Curva válida: baseline -> pico -> recuperación dentro de umbral (12 lecturas >= 10)
        signals = [315.0, 320.0, 325.0, 480.0, 620.0, 590.0, 470.0, 410.0, 355.0, 335.0, 325.0, 320.0]
        for idx, sig in enumerate(signals):
            r_post = self.client.post(
                "/api/telemetry",
                headers=headers,
                json={
                    "device_id": "esp8266-live-01",
                    "source_type": "physical",
                    "raw_signal": sig,
                    "lat": 4.6144 + idx * 0.0001,
                    "lon": -74.1106 - idx * 0.0001,
                    "sequence": idx + 1,
                    "timestamp": 1764070000 + idx * 60,
                },
            )
            self.assertEqual(r_post.status_code, 200)

        r_live = self.client.get("/api/scenarios/live-telemetry")
        self.assertEqual(r_live.status_code, 200)
        live_data = r_live.json()
        self.assertEqual(live_data["scenario"], "live_telemetry")
        self.assertEqual(live_data["source_classification"], "physical")
        self.assertEqual(live_data["evaluation"]["status"], STATUS_ELIGIBLE)
        self.assertTrue(live_data["evaluation"]["payout_eligible"])
        self.assertEqual(live_data["metadata"]["sensor_model"], "MQ-135-raw")
        self.assertFalse(live_data["metadata"]["is_certified_co2"])
        self.assertEqual(len(live_data["readings"]), 12)
        self.assertEqual(live_data["readings"][0]["device_id"], "esp8266-live-01")
        self.assertEqual(live_data["readings"][0]["sequence"], 1)
        self.assertEqual(live_data["readings"][-1]["sequence"], 12)
        self.assertAlmostEqual(live_data["readings"][0]["lat"], 4.6144)
        self.assertAlmostEqual(live_data["readings"][0]["lon"], -74.1106)

        for flag, expected_status in [
            ("expired=true", STATUS_BLOCKED),
            ("tampered=true", STATUS_TAMPERED),
            ("incomplete=true", STATUS_INCOMPLETE),
            ("replay=true", STATUS_BLOCKED),
        ]:
            res = self.client.get(f"/api/scenarios/live-telemetry?{flag}")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["evaluation"]["status"], expected_status)

        # Liquidación vía POST /api/settle con scenario='live_telemetry'
        r_settle = self.client.post("/api/settle", json={"scenario": "live_telemetry"})
        self.assertEqual(r_settle.status_code, 200)
        settle_data = r_settle.json()
        self.assertTrue(settle_data["settled"])
        self.assertEqual(settle_data["evaluation"]["status"], STATUS_ELIGIBLE)

        # Segundo POST /api/settle bloqueado por anti-replay
        r_settle_replay = self.client.post("/api/settle", json={"scenario": "live_telemetry"})
        self.assertEqual(r_settle_replay.status_code, 200)
        self.assertFalse(r_settle_replay.json()["settled"])
        self.assertEqual(r_settle_replay.json()["evaluation"]["status"], STATUS_BLOCKED)

        # Insertar nuevas lecturas con anomalía sin recuperación -> BLOCKED
        for idx in range(12):
            r_bad = self.client.post(
                "/api/telemetry",
                headers=headers,
                json={
                    "device_id": "esp8266-live-01",
                    "raw_signal": 650.0,
                    "lat": 0.0,
                    "lon": 0.0,
                    "sequence": 100 + idx,
                    "timestamp": 1764080000 + idx * 60,
                },
            )
            self.assertEqual(r_bad.status_code, 200)

        r_blocked = self.client.get("/api/scenarios/live-telemetry")
        self.assertEqual(r_blocked.status_code, 200)
        self.assertEqual(r_blocked.json()["evaluation"]["status"], STATUS_BLOCKED)


if __name__ == "__main__":
    unittest.main()
