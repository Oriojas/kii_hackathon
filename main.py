"""
VERA - Verified Environmental Responsibility Agent
Servidor Backend FastAPI y endpoints para Concordium Trusted Agents Hackathon.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from vera_core.agent import VeraAgent
from vera_core.attestations import canonical_json
from vera_core.config import VeraConfig
from vera_core.policy_engine import PolicyEngine, PolicyEvaluationResult
from vera_core.scenarios import (
    ScenarioBundle,
    get_business_scenario,
    get_home_air_scenario,
    get_imported_gps_scenario,
)

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = REPO_ROOT / "concordium-bridge"

app = FastAPI(
    title="VERA Core & Concordium Agent API",
    description="Verified Environmental Responsibility Agent para Concordium Trusted Agents Hackathon",
    version="1.0.0",
)

# Servir archivos estáticos y plantillas
app.mount("/css", StaticFiles(directory=str(REPO_ROOT / "css")), name="css")
app.mount("/img", StaticFiles(directory=str(REPO_ROOT / "img")), name="img")
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

config = VeraConfig.from_env()
agent = VeraAgent(config)
policy_engine = PolicyEngine(config)

# Inicializar Base de Datos SQLite local si aplica
SQLITE_DB_PATH = os.environ.get("SQLITE_DB_PATH", "./vera_storage.db")


def _get_sqlite_path() -> str:
    return os.environ.get("SQLITE_DB_PATH", SQLITE_DB_PATH)


def init_db(db_path: Optional[str] = None) -> None:
    conn = sqlite3.connect(db_path or _get_sqlite_path())
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            raw_signal REAL NOT NULL,
            lat REAL,
            lon REAL,
            sequence INTEGER,
            timestamp INTEGER NOT NULL,
            evidence_hash TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            recipient TEXT NOT NULL,
            amount REAL NOT NULL,
            tx_hash TEXT,
            timestamp INTEGER NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


init_db()


class SettleRequest(BaseModel):
    """Solicitud de evaluación y liquidación on-chain (o simulada) en Concordium."""

    scenario: str = "home_air"
    expired: bool = False
    tampered: bool = False
    incomplete: bool = False
    replay: bool = False
    recipient: Optional[str] = None
    idempotency_key: Optional[str] = None
    csv_path: Optional[str] = None


def _build_agent_card() -> Dict[str, Any]:
    """Construye el payload CIS-8004 Agent Card desde variables de entorno y VeraConfig."""
    port = os.environ.get("PORT", "8086")
    endpoint_url = os.environ.get("VERA_AGENT_ENDPOINT_URL", f"http://localhost:{port}")
    provider_url = os.environ.get("VERA_PROVIDER_URL", endpoint_url)
    version = os.environ.get("VERA_AGENT_VERSION", "1.0.0")
    genesis_or_net = os.environ.get("CONCORDIUM_GENESIS_HASH", "").strip() or config.concordium_network
    active_env = os.environ.get("VERA_AGENT_ACTIVE", "true").strip().lower()
    is_active = active_env in {"1", "true", "yes", "on"}

    return {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "protocolVersion": "1.0",
        "name": os.environ.get("VERA_AGENT_NAME", config.agent_display_name),
        "description": os.environ.get(
            "VERA_AGENT_DESCRIPTION",
            "Verified Environmental Responsibility Agent for Concordium Trusted Agents",
        ),
        "version": version,
        "provider": {
            "organization": os.environ.get("VERA_PROVIDER_ORG", "VERA Protocol"),
            "url": provider_url,
        },
        "endpoints": [
            {
                "name": os.environ.get("VERA_AGENT_ENDPOINT_NAME", "VERA-API"),
                "endpoint": endpoint_url,
                "protocolBinding": os.environ.get("VERA_AGENT_PROTOCOL_BINDING", "HTTP+JSON"),
                "version": version,
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "supportedTrust": ["attestation", "crypto-economic"],
        },
        "registrations": [
            {
                "agentId": config.agent_id,
                "agentRegistry": f"ccd:{genesis_or_net}:{config.cis8004_index}.{config.cis8004_subindex}",
                "network": config.concordium_network,
                "standard": "CIS-8004",
            }
        ],
        "active": is_active,
    }


def _bridge_command_args(cli_subcommand: str, payload: Optional[Dict[str, Any]] = None) -> List[str]:
    """Resuelve el comando para invocar `concordium-bridge` CLI (`node dist/cli.js` o `npx tsx`)."""
    dist_cli = BRIDGE_DIR / "dist" / "cli.js"
    src_cli = BRIDGE_DIR / "src" / "cli.ts"
    node_bin = shutil.which("node")
    npx_bin = shutil.which("npx")

    if dist_cli.is_file() and node_bin:
        cmd = [node_bin, str(dist_cli), cli_subcommand]
    elif npx_bin and src_cli.is_file():
        cmd = [npx_bin, "tsx", str(src_cli), cli_subcommand]
    else:
        raise RuntimeError("No se encontró Node.js/npx para invocar concordium-bridge.")

    if payload is not None:
        cmd.extend(["--input", json.dumps(payload)])
    return cmd


def _invoke_concordium_bridge(
    cli_subcommand: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 15,
) -> Dict[str, Any]:
    """Ejecuta `concordium-bridge` CLI y parsea su salida JSON con fallback limpio."""
    try:
        cmd = _bridge_command_args(cli_subcommand, payload)
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = (proc.stdout or "").strip()
        if stdout:
            parsed = json.loads(stdout.splitlines()[-1])
            if isinstance(parsed, dict):
                return parsed
        raise RuntimeError(proc.stderr.strip() or f"Salida vacía del bridge (exit={proc.returncode})")
    except Exception as exc:
        return {
            "ok": False,
            "mode": "simulate",
            "simulated": True,
            "network": config.concordium_network,
            "error": str(exc),
            "contracts": {
                "evidence_registry": {
                    "configured": True,
                    "index": str(config.evidence_registry_index),
                    "subindex": str(config.evidence_registry_subindex),
                    "on_chain": False,
                },
                "reward_pool": {
                    "configured": True,
                    "index": str(config.reward_pool_index),
                    "subindex": str(config.reward_pool_subindex),
                    "on_chain": False,
                },
                "cis8004": {
                    "configured": True,
                    "index": str(config.cis8004_index),
                    "subindex": str(config.cis8004_subindex),
                    "on_chain": False,
                },
            },
            "cis8004_badge": {
                "standard": "CIS-8004",
                "verified": False,
                "on_chain_verified": False,
                "contract": f"{config.cis8004_index}.{config.cis8004_subindex}",
                "agent_account": config.concordium_agent_account,
            },
        }


def _resolve_scenario_bundle(
    scenario: str,
    *,
    expired: bool = False,
    tampered: bool = False,
    incomplete: bool = False,
    csv_path: Optional[str] = None,
) -> ScenarioBundle:
    """Construye el `ScenarioBundle` correspondiente al escenario y banderas solicitadas."""
    normalized = scenario.strip().lower().replace("-", "_")
    if normalized == "home_air":
        if tampered:
            variant = "tampered"
        elif incomplete:
            variant = "incomplete"
        elif expired:
            variant = "expired_calibration"
        else:
            variant = "valid"
        return get_home_air_scenario(
            expired_calibration=expired,
            variant=variant,
            config=config,
        )

    if normalized == "business":
        if tampered:
            variant = "tampered"
        elif incomplete:
            variant = "incomplete"
        elif expired:
            variant = "expired_calibration"
        else:
            variant = "eligible"
        return get_business_scenario(
            variant=variant,
            expired_calibration=expired,
            config=config,
        )

    if normalized == "imported_gps":
        return get_imported_gps_scenario(
            csv_path=csv_path,
            tampered=tampered,
            incomplete=incomplete,
            expired_calibration=expired,
            config=config,
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Escenario no soportado: {scenario!r}. Use 'home_air', 'business' o 'imported_gps'.",
    )


def _format_scenario_response(
    bundle: ScenarioBundle,
    result: PolicyEvaluationResult,
) -> Dict[str, Any]:
    """Serializa la respuesta estándar de los endpoints `/api/scenarios/*`."""
    report = agent.generate_voluntary_report(result, bundle.graph)
    report_dict = report.to_dict()
    report_dict["executive_summary"] = report.executive_summary_es

    ordered_nodes = sorted(bundle.graph.nodes.values(), key=lambda n: (n.level, n.node_id))
    lineage = [n.to_dict() for n in ordered_nodes]

    return {
        "scenario": bundle.scenario_type,
        "variant": bundle.variant,
        "source_classification": bundle.source_classification,
        "evaluation_timestamp": bundle.evaluation_timestamp,
        "expected_evidence_root": bundle.expected_evidence_root,
        "evaluation": result.to_dict(),
        "report": report_dict,
        "lineage": lineage,
        "readings": list(bundle.readings),
        "zones": list(bundle.zones),
        "metadata": dict(bundle.metadata),
    }


def _is_evidence_settled_in_db(evidence_id: str) -> bool:
    init_db()
    conn = sqlite3.connect(_get_sqlite_path())
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM settlements WHERE evidence_id = ? AND status IN ('SETTLED', 'SIMULATED') LIMIT 1",
        (evidence_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None


def _save_settlement_in_db(
    *,
    evidence_id: str,
    status_str: str,
    policy_hash: str,
    recipient: str,
    amount: float,
    tx_hash: Optional[str],
) -> None:
    init_db()
    conn = sqlite3.connect(_get_sqlite_path())
    cursor = conn.cursor()
    ts = int(datetime.now(timezone.utc).timestamp())
    cursor.execute(
        """
        INSERT OR REPLACE INTO settlements
        (evidence_id, status, policy_hash, recipient, amount, tx_hash, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (evidence_id, status_str, policy_hash, recipient, amount, tx_hash, ts),
    )
    conn.commit()
    conn.close()


@app.get("/.well-known/agent-card.json")
async def get_agent_card():
    """
    Especificación CIS-8004: Agent Card pública del Agente VERA con la extensión Concordium Badge.
    """
    card = _build_agent_card()
    card_json = canonical_json(card)
    sha256_hash = hashlib.sha256(card_json.encode("utf-8")).hexdigest()

    return JSONResponse(
        content=card,
        headers={
            "X-Agent-Card-Hash": sha256_hash,
            "X-Agent-Status": "Active" if card["active"] else "Inactive",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """
    Dashboard interactivo de VERA para la Concordium Trusted Agents Hackathon.
    """
    return templates.TemplateResponse(request, "home_page/index.html", {
        "request": request,
        "agent_name": config.agent_display_name,
        "cis8004_index": config.cis8004_index,
        "cis8_index": int(os.environ.get("CONCORDIUM_CIS8_INDEX", "1003")),
        "evidence_registry_index": config.evidence_registry_index,
        "reward_pool_index": config.reward_pool_index,
        "agent_account": config.concordium_agent_account,
        "external_key": os.environ.get("VERA_EXTERNAL_KEY_ADDRESS", ""),
    })


@app.get("/api/concordium/status")
async def concordium_status():
    """
    Consulta al bridge Node.js/TypeScript el estado de red Concordium Testnet,
    contratos inteligentes y badge CIS-8004 (con fallback limpio si el nodo no responde).
    """
    return _invoke_concordium_bridge("status")


@app.get("/api/scenarios/home-air")
async def scenario_home_air(
    expired: bool = False,
    tampered: bool = False,
    incomplete: bool = False,
    replay: bool = False,
):
    """
    Retorna el escenario Vera Home Air (48 lecturas MQ-135 + evaluación de política y grafo de 7 niveles).
    """
    bundle = _resolve_scenario_bundle(
        "home_air",
        expired=expired,
        tampered=tampered,
        incomplete=incomplete,
    )
    engine = PolicyEngine(config=config)
    result = engine.evaluate(
        bundle.graph,
        scenario_type=bundle.scenario_type,
        evaluation_timestamp=bundle.evaluation_timestamp,
        expected_evidence_root=bundle.expected_evidence_root,
        already_settled=replay,
        record_in_graph=True,
    )
    return _format_scenario_response(bundle, result)


@app.get("/api/scenarios/business")
async def scenario_business(
    expired: bool = False,
    tampered: bool = False,
    incomplete: bool = False,
    replay: bool = False,
):
    """
    Retorna el escenario Vera Business (todas las zonas sintéticas declaradas + evaluación de política).
    """
    bundle = _resolve_scenario_bundle(
        "business",
        expired=expired,
        tampered=tampered,
        incomplete=incomplete,
    )
    engine = PolicyEngine(config=config)
    result = engine.evaluate(
        bundle.graph,
        scenario_type=bundle.scenario_type,
        evaluation_timestamp=bundle.evaluation_timestamp,
        expected_evidence_root=bundle.expected_evidence_root,
        already_settled=replay,
        record_in_graph=True,
    )
    return _format_scenario_response(bundle, result)


@app.get("/api/scenarios/imported-gps")
async def scenario_imported_gps(
    expired: bool = False,
    tampered: bool = False,
    incomplete: bool = False,
    replay: bool = False,
    csv_path: Optional[str] = None,
):
    """
    Retorna el escenario de datos importados CSV+GPS (`source_type='imported'`) con `lat` y `lon`.
    """
    bundle = _resolve_scenario_bundle(
        "imported_gps",
        expired=expired,
        tampered=tampered,
        incomplete=incomplete,
        csv_path=csv_path,
    )
    engine = PolicyEngine(config=config)
    result = engine.evaluate(
        bundle.graph,
        scenario_type=bundle.scenario_type,
        evaluation_timestamp=bundle.evaluation_timestamp,
        expected_evidence_root=bundle.expected_evidence_root,
        already_settled=replay,
        record_in_graph=True,
    )
    return _format_scenario_response(bundle, result)


@app.post("/api/settle")
async def settle_scenario(payload: SettleRequest):
    """
    Evalúa el escenario solicitado; si `payout_eligible` es `True`, invoca al bridge
    Concordium (`settle-reward` / `simulate`) y registra la liquidación en SQLite `settlements`.
    """
    bundle = _resolve_scenario_bundle(
        payload.scenario,
        expired=payload.expired,
        tampered=payload.tampered,
        incomplete=payload.incomplete,
        csv_path=payload.csv_path,
    )
    evidence_id = payload.idempotency_key or bundle.expected_evidence_root
    already_settled = payload.replay or _is_evidence_settled_in_db(evidence_id)

    engine = PolicyEngine(config=config)
    result = engine.evaluate(
        bundle.graph,
        scenario_type=bundle.scenario_type,
        evaluation_timestamp=bundle.evaluation_timestamp,
        expected_evidence_root=bundle.expected_evidence_root,
        idempotency_key=evidence_id,
        already_settled=already_settled,
        record_in_graph=False,
    )

    bridge_result: Optional[Dict[str, Any]] = None
    tx_hash: Optional[str] = None
    settled = False
    mode = "rejected"
    recipient_account = payload.recipient or config.concordium_agent_account
    amount_micro_ccd = (
        config.business_reward_amount_micro_ccd
        if bundle.scenario_type == "business"
        else config.home_reward_amount_micro_ccd
    )

    if result.payout_eligible:
        bridge_payload = {
            "evidence_id": evidence_id,
            "evidence_root": bundle.expected_evidence_root,
            "policy_hash": result.policy_hash,
            "recipient": recipient_account,
            "amount_micro_ccd": amount_micro_ccd,
            "calibration_expires_at": bundle.metadata.get("calibration_expires_at"),
        }
        bridge_result = _invoke_concordium_bridge("settle-reward", bridge_payload)
        if bridge_result.get("ok"):
            settled = True
            mode = str(bridge_result.get("mode", "simulate"))
            tx_hash = bridge_result.get("tx_hash")
            db_status = "SETTLED" if mode == "submit" else "SIMULATED"
            _save_settlement_in_db(
                evidence_id=evidence_id,
                status_str=db_status,
                policy_hash=result.policy_hash,
                recipient=recipient_account,
                amount=float(amount_micro_ccd),
                tx_hash=tx_hash,
            )
        else:
            mode = "error"

    engine._attach_decision_nodes(bundle.graph, result, evidence_id, tx_hash)
    response_body = _format_scenario_response(bundle, result)
    response_body.update({
        "ok": True,
        "settled": settled,
        "mode": mode,
        "simulated": mode == "simulate",
        "evidence_id": evidence_id,
        "tx_hash": tx_hash,
        "recipient": recipient_account,
        "amount_micro_ccd": amount_micro_ccd if settled else 0,
        "bridge_result": bridge_result,
    })
    return response_body


@app.post("/api/telemetry")
async def receive_telemetry(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    """
    Recepción de telemetría IoT desde ESP8266/ESP32.
    """
    expected_token = os.environ.get("API_AUTH_TOKEN", "your_secure_random_api_token_here")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
    else:
        token = payload.get("token")

    if token != expected_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token no autorizado")

    device_id = payload.get("device_id", "sensor-esp8266")
    source_type = payload.get("source_type", "physical")
    raw_signal = float(payload.get("raw_signal", 0.0))
    lat = float(payload.get("lat", 0.0))
    lon = float(payload.get("lon", 0.0))
    sequence = int(payload.get("sequence", 0))
    ts = int(payload.get("timestamp", datetime.now(timezone.utc).timestamp()))

    init_db()
    conn = sqlite3.connect(_get_sqlite_path())
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO telemetry (device_id, source_type, raw_signal, lat, lon, sequence, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (device_id, source_type, raw_signal, lat, lon, sequence, ts),
    )
    conn.commit()
    conn.close()

    return {"status": "success", "message": "Telemetría recibida e indexada localmente"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8086))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run("main:app", host=host, port=port, reload=True)
