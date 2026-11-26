"""
VERA - Verified Environmental Responsibility Agent
Servidor Backend FastAPI y endpoints para Concordium Trusted Agents Hackathon.
"""

import os
import json
import sqlite3
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Query, Header, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from vera_core.config import VeraConfig
from vera_core.attestations import AttestationGraph, compute_evidence_root, compute_policy_hash
from vera_core.policy_engine import PolicyEngine
from vera_core.scenarios import get_home_air_scenario, get_business_scenario
from vera_core.agent import VeraAgent

load_dotenv()

app = FastAPI(
    title="VERA Core & Concordium Agent API",
    description="Verified Environmental Responsibility Agent para Concordium Trusted Agents Hackathon",
    version="1.0.0"
)

# Servir archivos estáticos y plantillas
app.mount("/css", StaticFiles(directory="css"), name="css")
app.mount("/img", StaticFiles(directory="img"), name="img")
templates = Jinja2Templates(directory="templates")

config = VeraConfig.from_env()
agent = VeraAgent(config)
policy_engine = PolicyEngine(config)

# Inicializar Base de Datos SQLite local si aplica
SQLITE_DB_PATH = os.environ.get("SQLITE_DB_PATH", "./vera_storage.db")

def init_db():
    conn = sqlite3.connect(SQLITE_DB_PATH)
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


@app.get("/.well-known/agent-card.json")
async def get_agent_card():
    """
    Especificación CIS-8004: Agent Card pública del Agente VERA con la extensión Concordium Badge.
    """
    card = agent.get_agent_card()
    card_json = json.dumps(card, sort_keys=True, separators=(',', ':'))
    import hashlib
    sha256_hash = hashlib.sha256(card_json.encode('utf-8')).hexdigest()
    
    return JSONResponse(
        content=card,
        headers={
            "X-Agent-Card-Hash": sha256_hash,
            "X-Agent-Status": "Active" if config.agent_active else "Inactive",
            "Access-Control-Allow-Origin": "*"
        }
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """
    Dashboard interactivo de VERA para la Concordium Trusted Agents Hackathon.
    """
    return templates.TemplateResponse("home_page/index.html", {
        "request": request,
        "agent_name": config.agent_name,
        "cis8004_index": config.cis8004_index,
        "cis8_index": config.cis8_index,
        "evidence_registry_index": config.evidence_registry_index,
        "reward_pool_index": config.reward_pool_index,
        "agent_account": config.concordium_agent_account,
        "external_key": config.external_key_address
    })


@app.get("/api/scenarios/home-air")
async def scenario_home_air(expired: bool = False, tampered: bool = False, incomplete: bool = False):
    """
    Retorna el escenario Vera Home Air (48 lecturas físicas/simuladas MQ-135 + evaluación de política).
    """
    graph, batch = get_home_air_scenario(
        expired_calibration=expired,
        tampered=tampered,
        incomplete=incomplete
    )
    result = policy_engine.evaluate_graph(graph, record_in_graph=True)
    report = agent.generate_voluntary_report(graph, result)
    
    return {
        "scenario": "home_air",
        "evaluation": {
            "status": result.status,
            "payout_eligible": result.payout_eligible,
            "reason": result.reason,
            "policy_hash": result.policy_hash,
            "checks": [
                {"name": c.check_name, "passed": c.passed, "details": c.details}
                for c in result.checks
            ]
        },
        "report": report.to_dict(),
        "lineage": graph.get_lineage(),
        "batch_sample": batch[:5]
    }


@app.get("/api/scenarios/business")
async def scenario_business(tampered: bool = False, incomplete: bool = False):
    """
    Retorna el escenario Vera Business (4 zonas sintéticas declaradas + evaluación de política).
    """
    graph, batch = get_business_scenario(
        tampered=tampered,
        incomplete=incomplete
    )
    result = policy_engine.evaluate_graph(graph, record_in_graph=True)
    report = agent.generate_voluntary_report(graph, result)
    
    return {
        "scenario": "business",
        "evaluation": {
            "status": result.status,
            "payout_eligible": result.payout_eligible,
            "reason": result.reason,
            "policy_hash": result.policy_hash,
            "checks": [
                {"name": c.check_name, "passed": c.passed, "details": c.details}
                for c in result.checks
            ]
        },
        "report": report.to_dict(),
        "lineage": graph.get_lineage(),
        "batch_sample": batch
    }


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

    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO telemetry (device_id, source_type, raw_signal, lat, lon, sequence, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (device_id, source_type, raw_signal, lat, lon, sequence, ts)
    )
    conn.commit()
    conn.close()

    return {"status": "success", "message": "Telemetría recibida e indexada localmente"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8086))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run("main:app", host=host, port=port, reload=True)
