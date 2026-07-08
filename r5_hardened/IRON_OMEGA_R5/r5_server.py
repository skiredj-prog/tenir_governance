"""
R5 IRON OMEGA: Unified FastAPI Server
======================================
Integrates all four R5 components into the V42CIron API:

  R5.1 Neuro-Symbolic  → /api/v1/adjudicate now routes through NSL inference engine
  R5.2 Graph Ontology  → All state persisted to Neo4j (replaces in-memory lists)
  R5.3 WebSocket VPS   → /ws/vps broadcasts live trajectory to 3D VPS engine
  R5.4 Distributed Crypto → Merkle ledger with consensus broadcast

Usage:
  uvicorn r5_server:app --host 0.0.0.0 --port 8000 --reload

Environment variables:
  NEO4J_URI          bolt://localhost:7687
  NEO4J_USER         neo4j
  NEO4J_PASSWORD     tenir_password
  NEO4J_DATABASE     tenir
  NEO4J_SEED         true  (seed partner_a/partner_b ontology on first run)
  OLLAMA_MODEL       tenir-nsl:latest  (or "grammar" for grammar-only mode)
  OLLAMA_URL         http://localhost:11434
  LEDGER_PATH        ledger/tenir_ledger.jsonl
  OATH_SECRET        <production secret>
  PEER_NODES         http://node2:8000,http://node3:8000  (comma-separated)
"""

import os
import json
import logging
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List
from uuid import uuid4

from fastapi import FastAPI, WebSocket, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# R5 components
from r5_neuro_symbolic.inference.nsl_inference import (
    NSLInferenceEngine, NSLInferenceConfig, get_inference_engine
)
from r5_graph_ontology.neo4j_graph import (
    Neo4jGraphDB, startup as db_startup, shutdown as db_shutdown, get_db
)
from r5_websocket.hub.ws_hub import (
    WebSocketHub, get_hub, handle_client
)
from r5_distributed_crypto.merkle.distributed_ledger import (
    DistributedLedger, KeyCeremony, PeerNode, ConsensusBroadcast,
    get_ledger, get_ceremony
)

# Kernel (unchanged from V42CIron)
from core.trajectory import TrajectoryKernel
from core.ces_matrix import CESMatrix, CESState
from tenir_governance.policy_engine import PolicyEngine
from tenir_governance.nomenclature import OperatingModeNames

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("r5.server")


# ─── LIFESPAN ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[R5] Starting IRON OMEGA R5 server…")

    # Initialize Neo4j
    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_pass = os.getenv("NEO4J_PASSWORD", "tenir_password")
    neo4j_db = os.getenv("NEO4J_DATABASE", "tenir")
    neo4j_seed = os.getenv("NEO4J_SEED", "false").lower() == "true"

    try:
        await db_startup(
            uri=neo4j_uri,
            auth=(neo4j_user, neo4j_pass),
            database=neo4j_db,
            seed=neo4j_seed,
        )
        logger.info("[R5] Neo4j connected")
    except Exception as e:
        logger.warning(f"[R5] Neo4j unavailable ({e}) — graph features disabled")

    # Initialize NSL inference engine
    ollama_model = os.getenv("OLLAMA_MODEL", "tenir-nsl:latest")
    grammar_only = ollama_model.lower() == "grammar"
    nsl_config = NSLInferenceConfig(
        model_name=ollama_model,
        grammar_only_mode=grammar_only,
        ollama_base_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
    )
    get_inference_engine(nsl_config)
    logger.info(f"[R5] NSL inference: {'grammar-only' if grammar_only else f'LLM={ollama_model}'}")

    # Initialize policy first — runtime depends on a validated shared policy contract
    policy_profile = os.getenv("TENIR_POLICY_PROFILE", "um6p_shadow_v4").strip().lower()
    if policy_profile in {"partner_b", "ocp_sovereign", "ocp_sovereign_pilot"}:
        app.state.policy = PolicyEngine.ocp_sovereign_pilot()
    elif policy_profile in {"default", "canonical"}:
        app.state.policy = PolicyEngine.default()
    else:
        app.state.policy = PolicyEngine.um6p_shadow_v4()
    app.state.policy.validate()
    logger.info(f"[R5] Governance policy: {app.state.policy.version}")

    # Initialize distributed ledger + key ceremony
    ledger_path = os.getenv("LEDGER_PATH", "ledger/tenir_ledger.jsonl")
    oath_secret = os.getenv("OATH_SECRET", "").strip()
    if not oath_secret:
        raise RuntimeError(
            "OATH_SECRET environment variable is required and must not be empty. "
            "This secret signs the SHADOW->ENFORCE oath ceremony (KeyCeremony). "
            "There is no safe default: refusing to start with an unset or "
            "well-known signing secret would allow forged mode-transition oaths."
        )
    ledger = get_ledger(ledger_path)
    used_nonces = ledger.recover_used_nonces()
    get_ceremony(oath_secret, used_nonces=used_nonces)
    logger.info(f"[R5] Distributed ledger: {ledger_path}")

    # Initialize peer nodes for consensus
    peer_urls = [u.strip() for u in os.getenv("PEER_NODES", "").split(",") if u.strip()]
    if peer_urls:
        peers = [PeerNode(node_id=f"peer-{i}", url=url, public_key="") for i, url in enumerate(peer_urls)]
        app.state.consensus = ConsensusBroadcast(peers)
        logger.info(f"[R5] Consensus peers: {peer_urls}")
    else:
        app.state.consensus = ConsensusBroadcast([])
        logger.info("[R5] Single-node mode (no peers)")

    # Initialize kernel (stateful trajectory engine)
    app.state.kernel = TrajectoryKernel(epsilon=app.state.policy.epsilon)
    app.state.ces = CESMatrix()
    app.state.mode = ledger.recover_last_mode(default=OperatingModeNames.SHADOW_PASSIVE)
    app.state.tenant_id = os.getenv("TENANT_ID", "partner_a")

    logger.info("[R5] IRON OMEGA R5 ready")
    yield

    # Shutdown
    await db_shutdown()
    logger.info("[R5] Shutdown complete")


# ─── APP ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="TENIR 2C — IRON OMEGA R5",
    version="5.0.0",
    description="Neuro-Symbolic Governance Platform: NSL + Neo4j + Live VPS + Distributed Crypto",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── REQUEST / RESPONSE MODELS ────────────────────────────────────────────────

class AdjudicationRequest(BaseModel):
    """
    R5 accepts EITHER:
      - Strict JSON EventSample (legacy V42CIron format)
      - Raw natural language (routed through NSL inference engine)
    """
    raw_input: str = Field(..., description="Natural language OR JSON EventSample")
    case_id: Optional[str] = Field(None, description="Optional case identifier")
    actor_ref: Optional[str] = Field(None, description="Operator ID or service ref")


class AdjudicationResponse(BaseModel):
    entry_id: str
    timestamp: str
    s_score: float
    ds_de: float
    d2s_de2: float
    horizon_events: Optional[int]
    ces_state: str
    membrane_decision: str
    rationale: str
    operating_mode: str
    nsl_backend: str          # "llm" | "grammar" | "json"
    nsl_confidence: float
    entry_hash: str
    merkle_epoch: int


class TransitionRequest(BaseModel):
    operator_id: str
    target_mode: str = Field(..., pattern="^(SHADOW_OFF|SHADOW_PASSIVE|SHADOW_CRITICAL|ENFORCE)$")
    oath_text: str
    oath_signature: str
    nonce: str
    timestamp: str


class LedgerVerifyResponse(BaseModel):
    valid: bool
    entry_count: int
    broken_links: int
    epoch_count: int
    sealed_epochs: int
    epoch_roots: List[dict]


class EpochValidateRequest(BaseModel):
    """Peer consensus validation endpoint payload."""
    epoch_id: int
    merkle_root: str
    entry_count: int
    broadcaster: str


# ─── ADJUDICATE ───────────────────────────────────────────────────────────────

@app.post("/api/v1/adjudicate", response_model=AdjudicationResponse)
async def adjudicate(req: AdjudicationRequest):
    """
    R5 main adjudication endpoint.

    Pipeline:
      1. NSL inference (LLM → JSON → Grammar fallback)
      2. Trajectory kernel computation
      3. CES state evaluation
      4. Distributed ledger append (Merkle + hash chain)
      5. Neo4j graph persist
      6. WebSocket broadcast to all VPS clients
    """
    nsl_engine = get_inference_engine()
    ledger = get_ledger()
    hub = get_hub()

    # ── Step 1: NSL Inference ─────────────────────────────────────────────────
    record = nsl_engine.infer(req.raw_input)

    if not record.validation_passed or not record.compiled_params:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "NSL_PARSE_FAILURE",
                "message": record.error or "Could not parse input",
                "input": req.raw_input[:200],
                "backend": record.inference_backend,
            }
        )

    params = record.compiled_params
    pressure = float(params.get("pressure", 0.5))
    velocity = float(params.get("velocity", 0.5))
    capacity = float(params.get("capacity", 0.85))
    option_space = float(params.get("option_space", 0.75))

    # ── Step 2: Trajectory Kernel ─────────────────────────────────────────────
    trajectory = app.state.kernel.compute(pressure, velocity, capacity)

    # ── Step 3: CES State + Shared Policy Membrane ───────────────────────────
    ces_state = _scalar_to_ces_state(trajectory.s_score, trajectory.ds_de, option_space)
    membrane_decision, policy_rationale, alert, intended_block = app.state.policy.evaluate_membrane(
        s_score=trajectory.s_score,
        ds_de=trajectory.ds_de,
        d2s_de2=trajectory.d2s_de2,
        option_space=option_space,
        projected_events_to_zero=trajectory.horizon_events,
        operating_mode=_policy_mode(app.state.mode),
    )
    rationale = _build_rationale(trajectory, ces_state, membrane_decision, record, policy_rationale)

    # ── Step 4: Distributed Ledger Append ────────────────────────────────────
    nsl_ast_hash = hashlib.sha256(
        json.dumps(record.final_ast or {}, sort_keys=True).encode()
    ).hexdigest() if record.final_ast else "NO_AST"

    workflow_id = req.case_id or record.final_ast.get("entity_identifier") or "WF-UNKNOWN" if record.final_ast else "WF-UNKNOWN"

    entry = await ledger.append(
        tenant_id=app.state.tenant_id,
        workflow_id=str(workflow_id),
        membrane_decision=membrane_decision,
        operating_mode=app.state.mode,
        s_score=trajectory.s_score,
        ds_de=trajectory.ds_de,
        ces_state=ces_state,
        rationale=rationale,
        nsl_ast_hash=nsl_ast_hash,
        policy_version=app.state.policy.version,
    )

    # Broadcast Merkle root if epoch just sealed
    current_epoch = ledger.current_epoch
    sealed_epoch = None
    for ep in ledger._epochs[:-1]:
        if ep.sealed and ep.epoch_id == current_epoch.epoch_id - 1:
            sealed_epoch = ep
            break
    if sealed_epoch and hasattr(app.state, "consensus"):
        import asyncio
        asyncio.create_task(
            app.state.consensus.broadcast_epoch_root(
                sealed_epoch.epoch_id,
                sealed_epoch.merkle_root,
                len(sealed_epoch.entries),
            )
        )

    # ── Step 5: Neo4j Persist ─────────────────────────────────────────────────
    try:
        db = await get_db()
        await db.append_ledger_entry(
            event_id=str(uuid4()),
            s_score=trajectory.s_score,
            ds_de=trajectory.ds_de,
            d2s_de2=trajectory.d2s_de2,
            horizon_events=trajectory.horizon_events,
            operating_mode=app.state.mode,
            membrane_decision=membrane_decision,
            rationale=rationale,
            previous_entry_id=None,
            previous_hash=entry.previous_hash,
            ces_state=ces_state,
        )
    except Exception as e:
        logger.warning(f"[R5] Neo4j persist failed (non-fatal): {e}")

    # ── Step 6: WebSocket Broadcast ───────────────────────────────────────────
    await hub.broadcast_trajectory(
        s_score=trajectory.s_score,
        ds_de=trajectory.ds_de,
        d2s_de2=trajectory.d2s_de2,
        horizon=trajectory.horizon_events,
        ces_state=ces_state,
        operating_mode=app.state.mode,
        membrane_decision=membrane_decision,
        workflow_id=str(workflow_id),
    )

    # Also broadcast ledger append
    await hub.broadcast_ledger_append(
        entry_id=entry.entry_id,
        entry_hash=entry.entry_hash,
        previous_hash=entry.previous_hash,
        membrane_decision=membrane_decision,
    )

    return AdjudicationResponse(
        entry_id=entry.entry_id,
        timestamp=entry.timestamp,
        s_score=trajectory.s_score,
        ds_de=trajectory.ds_de,
        d2s_de2=trajectory.d2s_de2,
        horizon_events=trajectory.horizon_events,
        ces_state=ces_state,
        membrane_decision=membrane_decision,
        rationale=rationale,
        operating_mode=app.state.mode,
        nsl_backend=record.inference_backend,
        nsl_confidence=record.confidence,
        entry_hash=entry.entry_hash,
        merkle_epoch=entry.epoch,
    )


# ─── MODE TRANSITION ─────────────────────────────────────────────────────────

@app.post("/api/v1/transition")
async def request_transition(req: TransitionRequest):
    """
    R5 sovereign transition endpoint.
    Validates oath signature via KeyCeremony before allowing mode transitions.
    """
    ceremony = get_ceremony()
    ledger = get_ledger()
    hub = get_hub()
    current_epoch = ledger.current_epoch.epoch_id

    # Verify oath
    valid = ceremony.verify_oath(
        oath_text=req.oath_text,
        signature=req.oath_signature,
        epoch_id=current_epoch,
        operator_id=req.operator_id,
        nonce=req.nonce,
        timestamp=req.timestamp,
    )

    if not valid:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "OATH_VERIFICATION_FAILED",
                "message": "The oath signature is invalid or expired.",
            }
        )

    from_mode = app.state.mode
    app.state.mode = req.target_mode
    transition_id = str(uuid4())

    logger.info(
        f"[R5] Mode transition: {from_mode} → {req.target_mode} "
        f"by operator={req.operator_id}"
    )

    transition_entry = ledger.append_control_transition(
        transition=f"{from_mode}_TO_{req.target_mode}",
        from_mode=from_mode,
        to_mode=req.target_mode,
        operator_id=req.operator_id,
        reason="operator-authenticated oath transition",
        policy_version=app.state.policy.version,
        override_signature=req.oath_signature,
        override_operator=req.operator_id,
        nonce=req.nonce,
    )

    # Persist transition to Neo4j
    try:
        db = await get_db()
        await db.run(
            """
            CREATE (t:ControlTransition {
              transition_id: $tid, timestamp: $ts,
              from_mode: $fm, to_mode: $tm,
              operator_id: $op, oath_hash: $oh
            })
            """,
            tid=transition_id,
            ts=datetime.now(timezone.utc).isoformat(),
            fm=from_mode, tm=req.target_mode,
            op=req.operator_id,
            oh=hashlib.sha256(req.oath_text.encode()).hexdigest(),
        )
    except Exception as e:
        logger.warning(f"[R5] Neo4j transition persist failed: {e}")

    # Broadcast to VPS
    await hub.broadcast_mode_transition(
        from_mode=from_mode,
        to_mode=req.target_mode,
        operator_id=req.operator_id,
        transition_id=transition_id,
    )

    return {
        "status": "success",
        "transition_id": transition_id,
        "from_mode": from_mode,
        "to_mode": req.target_mode,
        "epoch": current_epoch,
        "ledger_entry_id": transition_entry.get("entry_id"),
        "ledger_entry_hash": transition_entry.get("entry_hash"),
    }


# ─── OATH SIGNING ─────────────────────────────────────────────────────────────

@app.post("/api/v1/oath/sign")
async def sign_oath(operator_id: str):
    """Signs an oath for the current epoch. Call before requesting a transition."""
    ceremony = get_ceremony()
    ledger = get_ledger()
    epoch_id = ledger.current_epoch.epoch_id
    signed = ceremony.sign_oath(
        oath_text=KeyCeremony.OATH_TEXT,
        epoch_id=epoch_id,
        operator_id=operator_id,
    )
    return {"oath_text": KeyCeremony.OATH_TEXT, **signed}


# ─── LEDGER ENDPOINTS ─────────────────────────────────────────────────────────

@app.get("/api/v1/ledger/verify", response_model=LedgerVerifyResponse)
async def verify_ledger():
    ledger = get_ledger()
    return ledger.verify_full_chain()


@app.get("/api/v1/ledger/recent")
async def get_recent_entries(n: int = 50):
    ledger = get_ledger()
    return ledger.get_recent_entries(min(n, 200))


@app.get("/api/v1/ledger/proof/{entry_id}")
async def get_merkle_proof(entry_id: str):
    ledger = get_ledger()
    proof = ledger.get_merkle_proof(entry_id)
    if not proof:
        raise HTTPException(404, "Entry not found in a sealed epoch")
    return proof


@app.post("/api/v1/ledger/validate_epoch")
async def validate_epoch(req: EpochValidateRequest):
    """Peer consensus endpoint — validates a claimed Merkle root against local ledger."""
    ledger = get_ledger()
    for epoch in ledger._epochs:
        if epoch.epoch_id == req.epoch_id and epoch.sealed:
            agrees = epoch.merkle_root == req.merkle_root
            return {"agrees": agrees, "local_root": epoch.merkle_root}
    return {"agrees": False, "reason": "Epoch not found or not yet sealed"}


# ─── GRAPH ENDPOINTS ──────────────────────────────────────────────────────────

@app.get("/api/v1/graph/cp_net")
async def get_cp_net():
    """Returns the live CP-Net graph for VPS 3D rendering."""
    try:
        db = await get_db()
        return await db.get_cp_net_state()
    except Exception as e:
        return {"edges": [], "conflicts": [], "error": str(e)}


@app.get("/api/v1/graph/fragility")
async def get_fragility():
    """Returns structural fragility scores per domain."""
    try:
        db = await get_db()
        return await db.run(
            """
            MATCH (d:Domain)-[:CONTAINS]->(c:CaseObject)
            RETURN d.name AS domain, d.domain_type, count(c) AS case_count
            ORDER BY case_count DESC
            """
        )
    except Exception as e:
        return {"error": str(e)}


# ─── STATE ────────────────────────────────────────────────────────────────────

@app.get("/api/v1/state")
async def get_state():
    ledger = get_ledger()
    hub = get_hub()
    recent = ledger.get_recent_entries(1)
    last = recent[0] if recent else {}
    return {
        "mode": app.state.mode,
        "s_score": last.get("s_score", 1.0),
        "ds_de": last.get("ds_de", 0.0),
        "ces_state": last.get("ces_state", "REST"),
        "entry_count": ledger.entry_count,
        "current_epoch": ledger.current_epoch.epoch_id,
        "websocket_clients": hub.connection_count,
        "chain_valid": ledger.verify_full_chain()["valid"],
    }


# ─── WEBSOCKET ────────────────────────────────────────────────────────────────

@app.websocket("/ws/vps")
async def vps_websocket(ws: WebSocket):
    """
    Live WebSocket endpoint for the 3D VPS engine.
    All kernel events are streamed here in real-time.
    """
    hub = get_hub()
    await handle_client(ws, hub)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _scalar_to_ces_state(s: float, ds_de: float, option_space: float) -> str:
    if s >= 1.5 and abs(ds_de) < 0.05:
        return "REST"
    if s >= 0.8:
        return "TENSION" if abs(ds_de) > 0.1 else "METABOLIZING"
    if s >= 0.4:
        return "METABOLIZING"
    if option_space < 0.1 and ds_de < -0.2:
        return "COLLAPSE"
    return "SCHIZOPHRENIA" if ds_de > 0.3 and s < 0.6 else "COLLAPSE"


def _policy_mode(mode: str) -> str:
    if mode == "ENFORCE":
        return OperatingModeNames.ENFORCE
    if mode == "SHADOW_CRITICAL":
        return OperatingModeNames.SHADOW_CRITICAL
    if mode == "SHADOW_OFF":
        return OperatingModeNames.SHADOW_OFF
    return OperatingModeNames.SHADOW_PASSIVE



def _build_rationale(trajectory, ces_state: str, decision: str, record, policy_rationale: str) -> str:
    parts = [
        f"S={trajectory.s_score:.3f}",
        f"dS/de={trajectory.ds_de:.3f}",
        f"CES={ces_state}",
        f"Decision={decision}",
        f"Policy={policy_rationale}",
        f"NSL={record.inference_backend}(conf={record.confidence:.2f})",
    ]
    if trajectory.horizon_events:
        parts.append(f"Horizon={trajectory.horizon_events}ev")
    return " | ".join(parts)
