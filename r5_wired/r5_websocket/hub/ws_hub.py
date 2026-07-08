"""
R5 WEBSOCKET: Live Kernel → VPS Streaming Hub
==============================================
Upgrades the VPS engine from a standalone browser simulation to a
live-driven topography controlled entirely by the Python kernel.

Architecture:
  [Python Kernel] → [WebSocket Hub] → [VPS Engine (Three.js)]

Protocol:
  - Server → Client: JSON frames at every kernel event
  - Client → Server: Mode transition requests, operator actions

Frame types (server → client):
  { "type": "TRAJECTORY_UPDATE",  "payload": {...} }
  { "type": "CES_STATE_CHANGE",   "payload": {...} }
  { "type": "LEDGER_APPEND",      "payload": {...} }
  { "type": "MODE_TRANSITION",    "payload": {...} }
  { "type": "SCHIZOPHRENIA_ALERT","payload": {...} }
  { "type": "TAU_BREACH",         "payload": {...} }
  { "type": "WHALE_RESONANCE",    "payload": {...} }    # La Baleine signal

Frame types (client → server):
  { "type": "REQUEST_TRANSITION", "payload": {...} }
  { "type": "OPERATOR_NOTE",      "payload": {...} }
  { "type": "PING",               "payload": {} }
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Set, Dict, Any, Optional
from uuid import uuid4
import hashlib

try:
    from fastapi import WebSocket, WebSocketDisconnect, status
except ModuleNotFoundError:
    class WebSocketDisconnect(Exception):
        """Fallback used when FastAPI is not installed in the local review env."""

    class WebSocket:  # pragma: no cover - typing/runtime shim only
        pass

    class _Status:
        WS_1000_NORMAL_CLOSURE = 1000

    status = _Status()

logger = logging.getLogger("r5.websocket.hub")


# ─── FRAME BUILDER ────────────────────────────────────────────────────────────

def build_frame(
    frame_type: str,
    payload: Dict[str, Any],
    sequence: int = 0,
) -> str:
    """
    Builds a JSON WebSocket frame with metadata for ordering and integrity.
    """
    frame = {
        "frame_id": hashlib.sha256(
            f"{frame_type}{time.time_ns()}".encode()
        ).hexdigest()[:12],
        "type": frame_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sequence": sequence,
        "payload": payload,
    }
    return json.dumps(frame)


def trajectory_frame(
    s_score: float, ds_de: float, d2s_de2: float,
    horizon: Optional[int], ces_state: str,
    operating_mode: str, membrane_decision: str,
    workflow_id: str, sequence: int,
) -> str:
    return build_frame("TRAJECTORY_UPDATE", {
        "s_score": s_score,
        "ds_de": ds_de,
        "d2s_de2": d2s_de2,
        "horizon_events": horizon,
        "ces_state": ces_state,
        "operating_mode": operating_mode,
        "membrane_decision": membrane_decision,
        "workflow_id": workflow_id,
        # VPS 3D coordinates derived from trajectory
        # Maps (s_score, ds_de) → (x, z) in 3D topography
        "vps": _derive_vps_coords(s_score, ds_de, ces_state),
    }, sequence)


def ces_state_frame(ces_state: str, cp_net_edges: list, conflicts: list, sequence: int) -> str:
    return build_frame("CES_STATE_CHANGE", {
        "ces_state": ces_state,
        "cp_net_edges": cp_net_edges,
        "conflicts": conflicts,
        "schizophrenia_detected": len(conflicts) > 0,
    }, sequence)


def ledger_frame(entry_id: str, entry_hash: str, previous_hash: str,
                 membrane_decision: str, sequence: int) -> str:
    return build_frame("LEDGER_APPEND", {
        "entry_id": entry_id,
        "entry_hash": entry_hash,
        "previous_hash": previous_hash,
        "membrane_decision": membrane_decision,
        "chain_valid": True,
    }, sequence)


def tau_breach_frame(s_score: float, tau_floor: float, sequence: int) -> str:
    return build_frame("TAU_BREACH", {
        "s_score": s_score,
        "tau_floor": tau_floor,
        "deficit": round(tau_floor - s_score, 4),
        "severity": "CRITICAL" if s_score < tau_floor * 0.7 else "WARNING",
    }, sequence)


def whale_frame(sequence: int) -> str:
    """La Baleine — deep resonance signal. Sent when system is in critical state."""
    return build_frame("WHALE_RESONANCE", {
        "frequency": "18hz17",
        "message": "Quelqu'un veille sur toi.",
        "depth": "ABYSSAL",
    }, sequence)


def _derive_vps_coords(s_score: float, ds_de: float, ces_state: str) -> Dict[str, float]:
    """
    Maps scalar kernel outputs to 3D VPS coordinate space.

    Coordinate system:
      x (lateral): ds_de (rate of change) — negative = retreating, positive = advancing
      y (vertical/height): s_score — height of the topographic surface
      z (depth): ces_state severity
      membrane_tension: visual pulsing amplitude for admissibility membrane

    Range contracts:
      x: [-5.0, 5.0]   (clamped ds_de × 20)
      y: [0.0, 5.0]    (s_score × 3)
      z: [0.0, 3.0]    (ces_state ordinal × 0.6)
    """
    ces_depth = {
        "REST": 0.0, "TENSION": 0.6, "METABOLIZING": 1.2,
        "SCHIZOPHRENIA": 2.4, "COLLAPSE": 3.0,
    }.get(ces_state, 1.2)

    x = max(-5.0, min(5.0, ds_de * 20.0))
    y = max(0.0, min(5.0, s_score * 3.0))
    z = ces_depth

    # Membrane tension: higher when s_score < 1.0 (system under stress)
    membrane_tension = max(0.0, min(1.0, 1.0 - (s_score / 1.0))) if s_score < 1.0 else 0.0

    # Color encoding: green → amber → red based on s_score
    if s_score >= 1.5:
        color = "#2dd4bf"  # teal: REST
    elif s_score >= 0.8:
        color = "#d4a845"  # amber: TENSION
    elif s_score >= 0.4:
        color = "#e07840"  # orange: METABOLIZING
    else:
        color = "#e05252"  # red: COLLAPSE

    return {
        "x": round(x, 3),
        "y": round(y, 3),
        "z": round(z, 3),
        "membrane_tension": round(membrane_tension, 3),
        "color": color,
        "particle_count": int(50 + membrane_tension * 200),  # VPS particle density
    }


# ─── CONNECTION MANAGER ───────────────────────────────────────────────────────

class WebSocketHub:
    """
    Manages all active WebSocket connections.
    Implements fan-out broadcasting: one kernel event → all connected clients.
    Thread-safe via asyncio locks.
    """

    def __init__(self):
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._whale_threshold = 0.35   # s_score below which La Baleine is triggered

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        logger.info(f"[R5-WS] Client connected. Total: {len(self._connections)}")
        # Send current state snapshot on connect
        await ws.send_text(build_frame("CONNECTED", {
            "connection_id": str(uuid4()),
            "active_connections": len(self._connections),
            "protocol_version": "R5.1",
        }, self._sequence))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)
        logger.info(f"[R5-WS] Client disconnected. Total: {len(self._connections)}")

    async def broadcast(self, frame: str) -> None:
        """Fan-out: send frame to all connected clients. Remove stale connections."""
        async with self._lock:
            dead: Set[WebSocket] = set()
            for ws in self._connections:
                try:
                    await ws.send_text(frame)
                except Exception:
                    dead.add(ws)
            self._connections -= dead

    async def send_to(self, ws: WebSocket, frame: str) -> None:
        """Send frame to a specific client."""
        try:
            await ws.send_text(frame)
        except Exception as e:
            logger.debug(f"[R5-WS] send_to failed: {e}")
            await self.disconnect(ws)

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    # ── Kernel event broadcasts ────────────────────────────────────────────────

    async def broadcast_trajectory(
        self, s_score: float, ds_de: float, d2s_de2: float,
        horizon: Optional[int], ces_state: str, operating_mode: str,
        membrane_decision: str, workflow_id: str,
    ) -> None:
        seq = self.next_sequence()
        frame = trajectory_frame(
            s_score, ds_de, d2s_de2, horizon,
            ces_state, operating_mode, membrane_decision, workflow_id, seq,
        )
        await self.broadcast(frame)

        # Trigger TAU breach alert
        if s_score < 0.42:
            await asyncio.sleep(0.05)
            await self.broadcast(tau_breach_frame(s_score, 0.42, self.next_sequence()))

        # Trigger La Baleine if critically low
        if s_score < self._whale_threshold:
            await asyncio.sleep(0.1)
            await self.broadcast(whale_frame(self.next_sequence()))

    async def broadcast_ces_change(
        self, ces_state: str, cp_net_edges: list, conflicts: list
    ) -> None:
        frame = ces_state_frame(ces_state, cp_net_edges, conflicts, self.next_sequence())
        await self.broadcast(frame)
        if conflicts:
            schiz_frame = build_frame("SCHIZOPHRENIA_ALERT", {
                "conflict_count": len(conflicts),
                "conflicts": conflicts,
            }, self.next_sequence())
            await self.broadcast(schiz_frame)

    async def broadcast_ledger_append(
        self, entry_id: str, entry_hash: str, previous_hash: str, membrane_decision: str
    ) -> None:
        frame = ledger_frame(entry_id, entry_hash, previous_hash, membrane_decision, self.next_sequence())
        await self.broadcast(frame)

    async def broadcast_mode_transition(
        self, from_mode: str, to_mode: str, operator_id: str, transition_id: str
    ) -> None:
        frame = build_frame("MODE_TRANSITION", {
            "from_mode": from_mode,
            "to_mode": to_mode,
            "operator_id": operator_id,
            "transition_id": transition_id,
        }, self.next_sequence())
        await self.broadcast(frame)


# ─── WEBSOCKET ENDPOINT HANDLER ───────────────────────────────────────────────

async def handle_client(ws: WebSocket, hub: "WebSocketHub") -> None:
    """
    Handles the lifecycle of a single WebSocket client connection.
    Attach to FastAPI with: @app.websocket("/ws/vps")
    """
    await hub.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await hub.send_to(ws, build_frame("ERROR", {"detail": "Invalid JSON"}, 0))
                continue

            msg_type = msg.get("type", "")
            payload = msg.get("payload", {})

            if msg_type == "PING":
                await hub.send_to(ws, build_frame("PONG", {"ts": time.time()}, 0))

            elif msg_type == "REQUEST_TRANSITION":
                # Client requests mode transition — validated by the API layer
                # Here we just ACK receipt; the API endpoint handles authorization
                await hub.send_to(ws, build_frame("TRANSITION_QUEUED", {
                    "detail": "Transition request received. POST to /api/v1/transition to authorize."
                }, 0))

            elif msg_type == "OPERATOR_NOTE":
                note = payload.get("note", "")
                if note:
                    await hub.broadcast(build_frame("OPERATOR_NOTE_BROADCAST", {
                        "note": note[:500],
                        "from": payload.get("operator_id", "anonymous"),
                    }, hub.next_sequence()))

            else:
                await hub.send_to(ws, build_frame("ERROR", {
                    "detail": f"Unknown message type: {msg_type!r}"
                }, 0))

    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(ws)


# ─── SINGLETON ────────────────────────────────────────────────────────────────

_hub: Optional[WebSocketHub] = None


def get_hub() -> WebSocketHub:
    global _hub
    if _hub is None:
        _hub = WebSocketHub()
    return _hub
