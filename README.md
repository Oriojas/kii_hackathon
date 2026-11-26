# VERA — Verified Environmental Responsibility Agent

**Concordium Trusted Agents Hackathon**

VERA es un agente ambiental verificable de extremo a extremo construido sobre **Concordium** que transforma telemetría física de hardware, lotes CSV+GPS y declaraciones sintéticas empresariales en **evidencia criptográficamente trazable**, **decisiones deterministas auditables** y **liquidaciones idempotentes de incentivos en Testnet**.

---

## 1. El Problema y la Propuesta de VERA

Los reportes ambientales tradicionales y los sistemas de incentivos ecológicos suelen sufrir tres fallas estructurales:

1. **Opacidad y *greenwashing***: no existe prueba verificable de dónde provienen las lecturas ni de si fueron manipuladas antes de generar un reporte o reclamar una recompensa.
2. **Decisiones opacas de IA o scripts ad-hoc**: delegar pagos a modelos no deterministas o reglas sin hash impide auditar por qué se aprobó o rechazó un incentivo.
3. **Mezcla indebida entre datos físicos, importados y simulados**: sensores de bajo costo o datos sintéticos de demostración se presentan con frecuencia como si fueran mediciones certificadas.

**VERA** resuelve esto separando estrictamente la **verificación determinista** (hash canónico, árbol de Merkle, motor de políticas y contratos inteligentes Rust/Wasm) de la **explicabilidad** (agente en español con recomendaciones accionables y declaración explícita de alcance honesto).

---

## 2. Arquitectura del Sistema

```text
+-------------------------------------------------------------------------+
| 1. INGESTA DE DATOS (Físicos, Importados y Sintéticos Declarados)       |
| - Hardware ESP8266 + MQ-135 + GPS (HTTP POST JSON + Bearer Token)       |
| - Lotes CSV+GPS (tests/fixtures/sample_gps_mq135.csv)                   |
| - Generadores reproducibles (Home Air 48 lecturas / Business 4 zonas)   |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
| 2. NÚCLEO DETERMINISTA (vera_core - Python)                             |
| - Canonicalización JSON + SHA-256 + Merkle Tree (paridad exacta JS/TS)  |
| - Grafo de Attestations de 7 niveles (AttestationGraph)                 |
| - PolicyEngine determinista -> policy_hash + Veredicto:                 |
|     ELIGIBLE | INCOMPLETE | TAMPERED | BLOCKED                          |
| - VeraAgent -> Explicación, mitigación y VoluntaryReport (sin pagos     |
|   arbitrarios)                                                          |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
| 3. BRIDGE & IDENTIDAD CONCORDIUM (concordium-bridge - TypeScript)       |
| - @concordium/web-sdk (gRPC TLS / Testnet)                              |
| - CIS-8004: Agent Card (/.well-known/agent-card.json) y registro        |
| - CIS-8: Vinculación de claves externas con dominio CIS-8/v1/canonical  |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
| 4. CONTRATOS INTELIGENTES RUST / WASM (contracts/)                      |
| - evidence_registry: registro inmutable, anti-replay, expiración y      |
|   revocación                                                            |
| - reward_pool: validación de policy_hash, verificación cruzada con      |
|   evidence_registry, anti-doble-gasto, pausa de emergencia y pago CCD   |
+-------------------------------------------------------------------------+
```

### Componentes Principales

- **Contratos Inteligentes Rust/Wasm (`contracts/`)**:
  - [`evidence_registry`](contracts/evidence_registry/src/lib.rs): Almacena compromisos on-chain (`evidence_id`, `evidence_root` de 32 bytes, `SourceType::Physical` o `SourceType::Synthetic`, `calibration_expires_at`, `metadata_uri`). Rechaza IDs duplicados (`DuplicateEvidenceId`), calibraciones vencidas (`CalibrationExpired`) y permite revocación controlada (`revoke_evidence`).
  - [`reward_pool`](contracts/reward_pool/src/lib.rs): Administra el fondo en CCD, exige coincidencia con el `policy_hash` activo (`InvalidPolicyHash`), valida la ventana de calibración (`CalibrationExpired`), consulta opcionalmente a `evidence_registry` para rechazar evidencia revocada o inválida (`EvidenceRevoked`, `RegistryCheckFailed`), previene doble gasto (`AlreadySettled`) y soporta pausa de emergencia (`pause` / `unpause`).
- **Bridge Concordium TypeScript (`concordium-bridge/`)**:
  - Construido sobre `@concordium/web-sdk` (`src/client.ts`).
  - **Paridad criptográfica (`src/canonical.ts`)**: Serialización JSON canónica (claves ordenadas recursivamente, sin espacios, enteros normalizados) y árbol de Merkle SHA-256 bit-a-bit idénticos a Python (`vera_core/attestations.py`).
  - **Identidad de Agentes CIS-8004 y CIS-8 (`src/agentIdentity.ts`)**: Generación/validación de `agent-card.json` compatible con `https://eips.ethereum.org/EIPS/eip-8004#registration-v1`, formato de registro `ccd:<genesisHashOrNetwork>:<index>.<subindex>`, interacción con `cis8004_registry.register_agent`, y firma/verificación de claves externas bajo `CIS-8/v1/canonical` hacia `cis8_registry.register_external_key`.
- **Núcleo VERA (`vera_core/`)**:
  - **Grafo de Attestations de 7 niveles (`vera_core/attestations.py`)**:
    1. `device_source` — Dispositivo / Fuente
    2. `calibration` — Calibración
    3. `batch` — Lote de lecturas
    4. `dataset` — Dataset agregado
    5. `analysis` — Análisis / Cálculo
    6. `policy_evaluation` — Evaluación de Política
    7. `settlement` — Liquidación
  - **Policy Engine Determinista (`vera_core/policy_engine.py`)**: Evalúa integridad de `content_hash` / `batch_hash` / `evidence_root`, cobertura mínima (`VERA_MIN_COVERAGE_RATIO`), vigencia de calibración (`VERA_MAX_CALIBRATION_AGE_HOURS`), protección anti-replay (`idempotency_key`) y umbrales específicos de cada escenario.
  - **Agente Explicable (`vera_core/agent.py`)**: `VeraAgent` nunca decide pagos de forma arbitraria; traduce el veredicto del `PolicyEngine` a resúmenes en español, propone acciones de mitigación y genera `VoluntaryReport` en JSON/Markdown.
- **Firmware IoT (`hardware_files/co2GpsSensor/co2GpsSensor/co2GpsSensor.ino`)**:
  - Nodo ESP8266 con sensor analógico MQ-135 y módulo GPS (`TinyGPS++`).
  - Envía peticiones **`HTTP POST`** con cuerpo JSON a `SERVER_URL`, cabeceras `Content-Type: application/json` y `Authorization: Bearer <API_TOKEN>`, y payload explícito con `"device_id"`, `"source_type":"physical"`, `"sensor_model":"MQ-135-raw"`, `"raw_signal"`, `"lat"`, `"lon"` y `"sequence"`. Todas las credenciales y parámetros provienen de `env.h`.
- **Backend FastAPI & Dashboard (`main.py` y `templates/home_page/index.html`)**:
  - Expone la interfaz web interactiva, la Agent Card pública en `/.well-known/agent-card.json`, los endpoints de escenarios (`/api/scenarios/home-air`, `/api/scenarios/business`) y la ingesta de telemetría IoT (`POST /api/telemetry`) respaldada por SQLite local por defecto (`SQLITE_DB_PATH`).

---

## 3. Escenarios y Estados Reproducibles

### Escenarios del MVP

1. **Vera Home Air (`home_air`)**:
   - Genera y evalúa un lote reproducible de **48 lecturas** del sensor **MQ-135** (`source_classification="physical"`, `unit="adc_raw"`, `is_certified_co2=false`) distribuidas en cuatro fases: línea base (`baseline`), pico de anomalía (`anomaly_peak`), intervención (`intervention`) y recuperación (`recovery`).
   - Verifica que ante un pico anómalo exista recuperación dentro de los umbrales configurados y que la calibración esté vigente.
2. **Vera Business (`business`)**:
   - Evalúa una operación empresarial multi-zona con **4 zonas declaradas** por defecto (`Oficinas`, `Producción`, `Bodega`, `Logística`) bajo `source_classification="synthetic_declared"` (`is_synthetic=true`).
   - Calcula consumo eléctrico (`kWh`), emisiones estimadas **Scope 2** (`kg CO2e`), intensidad por unidad producida y porcentaje de reducción frente a línea base.
3. **Datos Importados CSV+GPS**:
   - Conjunto georreferenciado de lecturas MQ-135 con coordenadas (`lat`, `lon`), identificador de origen y señal analógica disponible en [tests/fixtures/sample_gps_mq135.csv](tests/fixtures/sample_gps_mq135.csv) y utilidades de envío en [Test_send_data_endpoint.py](Test_send_data_endpoint.py). Permite validar flujos de ingesta con trazabilidad espacial.

### Los 4 Estados Reproducibles del `PolicyEngine`

| Estado | `payout_eligible` | Condición que lo dispara |
|---|---:|---|
| **`ELIGIBLE`** | `true` | Hashes y `evidence_root` íntegros, cobertura suficiente, calibración vigente, sin replay y métricas dentro de umbrales. |
| **`INCOMPLETE`** | `false` | Cobertura inferior a `VERA_MIN_COVERAGE_RATIO` (p. ej. lecturas faltantes en Home Air o zonas faltantes en Business). |
| **`TAMPERED`** | `false` | Alteración posterior de nodos/lecturas que rompe `content_hash`, `batch_hash` o discrepa con `evidence_root`. |
| **`BLOCKED`** | `false` | Calibración expirada (`EXPIRED`), anomalía sin recuperación, incumplimiento de intensidad/reducción en Business o clave `idempotency_key` repetida (anti-replay). |

---

## 4. Alcance Honesto del MVP

- **Señal cruda MQ-135 vs. CO2 certificado**: Las lecturas del sensor MQ-135 se registran y etiquetan explícitamente como señal analógica cruda de ADC (`"sensor_model":"MQ-135-raw"`, `unit="adc_raw"`, `is_certified_co2=false`). Sirven para detectar cambios relativos y episodios de anomalía/ventilación, **no representan ppm certificados de CO2** ni sustituyen instrumentación NDIR calibrada o monitoreo regulatorio.
- **Datos sintéticos declarados**: El escenario *Vera Business* marca de forma explícita `source_classification="synthetic_declared"` y `is_synthetic=true` tanto en el Grafo de Attestations como en los reportes generados por `VeraAgent`. Una attestation criptográfica garantiza integridad, autoría y trazabilidad de la declaración, **no** convierte datos sintéticos en mediciones físicas ni reemplaza auditorías externas o inventarios verificados.
- **Uso de red**: El proyecto está diseñado para **Concordium Testnet** con fines de demostración técnica y verificación en hackathon.

---

## 5. Guía Paso a Paso de Instalación y Ejecución

### Requisitos Previos

- **Python** `3.10+`
- **Node.js** `20+` y `npm`
- **Rust** con target `wasm32v1-none` y [`cargo-concordium`](https://developer.concordium.software/en/mainnet/smart-contracts/guides/setup-tools.html) (para compilar y probar los contratos inteligentes)
- **concordium-client** (opcional, para despliegue directo desde CLI a Testnet)
- **Arduino IDE** (opcional, para flashear el firmware en ESP8266)

### Paso 1: Configurar Variables de Entorno

Copia la plantilla `.env.example` a `.env` en la raíz del proyecto y ajusta tus valores (nunca subas `.env` ni claves reales a git):

```bash
cp .env.example .env
```

Variables principales en [`.env.example`](.env.example):
- **Servidor y Auth**: `PORT`, `HOST`, `APP_ENV`, `API_AUTH_TOKEN`
- **Red y Contratos Concordium**: `CONCORDIUM_GRPC_HOST`, `CONCORDIUM_GRPC_PORT`, `CONCORDIUM_NETWORK`, `CONCORDIUM_AGENT_ACCOUNT_ADDRESS`, `CONCORDIUM_AGENT_PRIVATE_KEY_PATH`, `CONCORDIUM_EVIDENCE_REGISTRY_INDEX`, `CONCORDIUM_REWARD_POOL_INDEX`, `CONCORDIUM_CIS8004_INDEX`, `CONCORDIUM_CIS8_INDEX`
- **Persistencia**: `USE_SQLITE=true` y `SQLITE_DB_PATH=./vera_storage.db` por defecto (o parámetros genéricos `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` si se habilita base remota)
- **Políticas y Escenarios VERA**: `VERA_AGENT_ID`, `VERA_POLICY_VERSION`, `VERA_MIN_COVERAGE_RATIO`, `VERA_MAX_CALIBRATION_AGE_HOURS`, `VERA_HOME_*` y `VERA_BUSINESS_*`

### Paso 2: Instalar Dependencias Python

Con `poetry` o `pip`:

```bash
# Opción A: Poetry
poetry install

# Opción B: Entorno virtual estándar
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn python-dotenv jinja2 pandas plotly pymysql sshtunnel
```

### Paso 3: Instalar y Compilar `concordium-bridge` (TypeScript)

```bash
cd concordium-bridge
npm install
npm run build
cd ..
```

### Paso 4: Configurar Firmware IoT (Opcional, si usas hardware ESP8266)

1. Copia la plantilla de configuración junto al sketch `.ino`:
   ```bash
   cp hardware_files/co2GpsSensor/co2GpsSensor/env.h.example hardware_files/co2GpsSensor/co2GpsSensor/env.h
   ```
2. Edita `hardware_files/co2GpsSensor/co2GpsSensor/env.h` con tus credenciales locales:
   - `ssid` y `password` de tu red Wi-Fi
   - `SERVER_URL` apuntando a `http://<tu-host-vera>:8086/api/telemetry`
   - `API_TOKEN` idéntico a `API_AUTH_TOKEN` en `.env`
   - `DEVICE_ID` identificador del nodo (por ejemplo `esp8266-mq135-demo-01`)
3. Compila y carga [`co2GpsSensor.ino`](hardware_files/co2GpsSensor/co2GpsSensor/co2GpsSensor.ino) en la placa ESP8266 con las librerías `ESP8266WiFi`, `ESP8266HTTPClient`, `ESP8266WebServer`, `SoftwareSerial` y `TinyGPS++`.

---

## 6. Ejecución de Tests (Python, TypeScript y Rust)

### Tests de Python (`vera_core` y paridad Merkle/JSON con Node.js)

```bash
python3 -m unittest tests/test_vera_core.py -v
```

### Tests y Typecheck de TypeScript (`concordium-bridge`)

```bash
cd concordium-bridge
npm run typecheck
npm run test:vectors
cd ..
```

### Tests de Contratos Inteligentes Rust/Wasm (`cargo concordium test`)

```bash
cd contracts/evidence_registry
cargo concordium test

cd ../reward_pool
cargo concordium test
```

---

## 7. Arranque del Servidor FastAPI

Desde la raíz del repositorio (con `.env` configurado):

```bash
python3 main.py
```

O directamente con `uvicorn`:

```bash
uvicorn main:app --host 0.0.0.0 --port 8086 --reload
```

Endpoints disponibles tras el arranque:
- **Dashboard Web**: `http://localhost:8086/`
- **Agent Card CIS-8004**: `http://localhost:8086/.well-known/agent-card.json`
- **Escenario Home Air**: `GET /api/scenarios/home-air` (soporta `?expired=true`, `?tampered=true`, `?incomplete=true`)
- **Escenario Business**: `GET /api/scenarios/business` (soporta `?tampered=true`, `?incomplete=true`)
- **Ingesta de Telemetría IoT**: `POST /api/telemetry` con cabecera `Authorization: Bearer <API_AUTH_TOKEN>` y cuerpo JSON:
  ```json
  {
    "device_id": "esp8266-mq135-demo-01",
    "source_type": "physical",
    "sensor_model": "MQ-135-raw",
    "raw_signal": 340,
    "lat": 4.6144275,
    "lon": -74.1106485,
    "sequence": 1
  }
  ```

---

## 8. Compilación y Despliegue en Concordium Testnet

1. **Compilar los contratos Wasm con esquema embebido**:
   ```bash
   cd contracts/evidence_registry
   cargo concordium build --out concordium-out/module.wasm.v1 --schema-embed

   cd ../reward_pool
   cargo concordium build --out concordium-out/module.wasm.v1 --schema-embed
   ```
   *(Para builds verificables en producción/auditoría, utiliza `--verifiable` según la documentación de `cargo-concordium`.)*

2. **Desplegar los módulos en Testnet** (usando `concordium-client` y tu cuenta de Testnet fondeada):
   ```bash
   concordium-client module deploy contracts/evidence_registry/concordium-out/module.wasm.v1 \
     --sender <TU_CUENTA_TESTNET> \
     --grpc-ip grpc.testnet.concordium.com --grpc-port 20000 --secure

   concordium-client module deploy contracts/reward_pool/concordium-out/module.wasm.v1 \
     --sender <TU_CUENTA_TESTNET> \
     --grpc-ip grpc.testnet.concordium.com --grpc-port 20000 --secure
   ```

3. **Inicializar las instancias de contrato**:
   - Inicializa `evidence_registry` (sin parámetros de inicialización) para obtener su `<EVIDENCE_REGISTRY_INDEX>`.
   - Calcula el `policy_hash` determinista (desde `PolicyEngine` / `concordium-bridge`) e inicializa `reward_pool` pasando `InitParams` (`policy_hash` de 32 bytes y `evidence_registry` apuntando a `<EVIDENCE_REGISTRY_INDEX>,0`).
   - Fondea `reward_pool` mediante el entrypoint `fund` (o al inicializarlo con `--amount`).

4. **Actualizar `.env` y verificar con el bridge**:
   - Registra los índices desplegados en `.env` (`CONCORDIUM_EVIDENCE_REGISTRY_INDEX`, `CONCORDIUM_REWARD_POOL_INDEX`, `CONCORDIUM_CIS8004_INDEX`, `CONCORDIUM_CIS8_INDEX`) y configura `CONCORDIUM_AGENT_ACCOUNT_ADDRESS` y `CONCORDIUM_AGENT_PRIVATE_KEY_PATH`.
   - Ejecuta `cd concordium-bridge && npm run test:vectors` para verificar la generación de la Agent Card CIS-8004, el enlace CIS-8 y la paridad de hashes con el backend Python.

---

## Licencia

Consulta el archivo [`LICENSE`](LICENSE) en la raíz del repositorio.
