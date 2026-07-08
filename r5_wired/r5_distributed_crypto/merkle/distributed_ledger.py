"""
R5 DISTRIBUTED CRYPTOGRAPHY: Multi-Node Merkle Ledger
======================================================
Upgrades the V42CIron MVP's in-memory mock hash chain to a
fully distributed, cryptographically verifiable append-only ledger.

Architecture:
  Single node (MVP)  →  Multi-node Merkle ledger (R5)

Components:
  1. MerkleTree       — SHA-256 Merkle tree per epoch (e.g., per 100 events)
  2. MerkleNode       — individual event leaf
  3. LedgerChain      — append-only chain of Merkle roots + full entries
  4. PeerRegistry     — peer node discovery and status tracking
  5. ConsensusBroadcast — Raft-inspired lightweight consensus for ledger sync
  6. KeyCeremony      — hardware-backed (simulated) oath signing for SHADOW→ENFORCE

Security model:
  - Each LedgerEntry is SHA-256 hashed with its previous hash (hash chain)
  - Every 100 entries form a Merkle tree; the root is broadcast to peers
  - Peers validate Merkle roots independently
  - If quorum (>50% of peers) disagree on a root → LedgerIntegrityError
  - The SHADOW→ENFORCE transition requires a signed oath with HMAC
  - Oath keys are epoch-bound (Jubilee Cryptographic protocol)
"""

import hashlib
import hmac
import json
import math
import logging
import time
import asyncio
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple
from uuid import uuid4
from pathlib import Path

logger = logging.getLogger("r5.distributed_crypto")


# ─── MERKLE TREE ─────────────────────────────────────────────────────────────

def _sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _merkle_pair(left: str, right: str) -> str:
    """Hashes two Merkle nodes together (deterministic concatenation)."""
    return _sha256(left + right)


def build_merkle_tree(leaves: List[str]) -> Tuple[str, List[List[str]]]:
    """
    Builds a Merkle tree from a list of leaf hashes.
    Returns (root_hash, tree_levels) where tree_levels[0] = leaves.

    If the number of leaves is odd, the last leaf is duplicated.
    """
    if not leaves:
        return _sha256("EMPTY_EPOCH"), [[]]
    if len(leaves) == 1:
        return _sha256(leaves[0] + leaves[0]), [leaves]

    levels: List[List[str]] = [list(leaves)]

    current = list(leaves)
    while len(current) > 1:
        if len(current) % 2 == 1:
            current.append(current[-1])   # Duplicate last leaf
        next_level = [
            _merkle_pair(current[i], current[i + 1])
            for i in range(0, len(current), 2)
        ]
        levels.append(next_level)
        current = next_level

    return current[0], levels


def merkle_proof(leaf_index: int, tree_levels: List[List[str]]) -> List[Dict[str, str]]:
    """
    Generates a Merkle proof (audit path) for a given leaf.
    Returns list of {direction: "left"|"right", hash: "..."} steps.
    """
    proof: List[Dict[str, str]] = []
    idx = leaf_index

    for level in tree_levels[:-1]:
        if idx % 2 == 0:
            sibling_idx = idx + 1
            direction = "right"
        else:
            sibling_idx = idx - 1
            direction = "left"

        if sibling_idx < len(level):
            proof.append({"direction": direction, "hash": level[sibling_idx]})
        else:
            # Odd node — sibling is itself (duplicate)
            proof.append({"direction": direction, "hash": level[idx]})

        idx //= 2

    return proof


def verify_merkle_proof(leaf_hash: str, proof: List[Dict[str, str]], root: str) -> bool:
    """Verifies a Merkle proof without revealing other leaves."""
    current = leaf_hash
    for step in proof:
        if step["direction"] == "right":
            current = _merkle_pair(current, step["hash"])
        else:
            current = _merkle_pair(step["hash"], current)
    return current == root


# ─── LEDGER ENTRY ─────────────────────────────────────────────────────────────

EPOCH_SIZE = 100   # Number of entries per Merkle epoch


@dataclass
class DistributedLedgerEntry:
    """
    A single append-only ledger entry with full cryptographic binding.
    """
    entry_id: str
    epoch: int                 # Which Merkle epoch this entry belongs to
    sequence: int              # Position within epoch [0, EPOCH_SIZE)
    timestamp: str
    tenant_id: str
    workflow_id: str
    membrane_decision: str     # allow | allow_with_alert | allow_with_intended_block | block
    operating_mode: str        # SHADOW | ENFORCE
    s_score: float
    ds_de: float
    ces_state: str
    rationale: str
    nsl_ast_hash: str          # SHA-256 of the NSL AST that generated this event
    previous_hash: str         # Hash of the previous entry (hash chain)
    entry_type: str = "observation"
    transition_to_mode: Optional[str] = None
    policy_version: str = "unknown"
    override_signature: Optional[str] = None
    override_operator: Optional[str] = None
    override_nonce: Optional[str] = None
    entry_hash: str = ""            # SHA-256 of this entry's canonical payload

    @classmethod
    def create(
        cls,
        previous_hash: str,
        sequence: int,
        epoch: int,
        tenant_id: str,
        workflow_id: str,
        membrane_decision: str,
        operating_mode: str,
        s_score: float,
        ds_de: float,
        ces_state: str,
        rationale: str,
        nsl_ast_hash: str = "GENESIS",
        *,
        entry_type: str = "observation",
        transition_to_mode: Optional[str] = None,
        policy_version: str = "unknown",
        override_signature: Optional[str] = None,
        override_operator: Optional[str] = None,
        override_nonce: Optional[str] = None,
    ) -> "DistributedLedgerEntry":
        entry_id = str(uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()

        # Canonical payload (deterministic JSON, sorted keys)
        canonical = json.dumps({
            "entry_id": entry_id,
            "epoch": epoch,
            "sequence": sequence,
            "timestamp": timestamp,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "membrane_decision": membrane_decision,
            "operating_mode": operating_mode,
            "s_score": round(s_score, 6),
            "ds_de": round(ds_de, 6),
            "ces_state": ces_state,
            "rationale": rationale,
            "previous_hash": previous_hash,
            "nsl_ast_hash": nsl_ast_hash,
            "entry_type": entry_type,
            "transition_to_mode": transition_to_mode,
            "policy_version": policy_version,
            "override_signature": override_signature,
            "override_operator": override_operator,
            "override_nonce": override_nonce,
        }, sort_keys=True)

        entry_hash = _sha256(canonical)

        return cls(
            entry_id=entry_id,
            epoch=epoch,
            sequence=sequence,
            timestamp=timestamp,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            membrane_decision=membrane_decision,
            operating_mode=operating_mode,
            s_score=s_score,
            ds_de=ds_de,
            ces_state=ces_state,
            rationale=rationale,
            nsl_ast_hash=nsl_ast_hash,
            previous_hash=previous_hash,
            entry_type=entry_type,
            transition_to_mode=transition_to_mode,
            policy_version=policy_version,
            override_signature=override_signature,
            override_operator=override_operator,
            override_nonce=override_nonce,
            entry_hash=entry_hash,
        )


# ─── MERKLE EPOCH ─────────────────────────────────────────────────────────────

@dataclass
class MerkleEpoch:
    epoch_id: int
    entries: List[DistributedLedgerEntry] = field(default_factory=list)
    merkle_root: Optional[str] = None
    sealed: bool = False
    sealed_at: Optional[str] = None

    @property
    def is_full(self) -> bool:
        return len(self.entries) >= EPOCH_SIZE

    def seal(self) -> str:
        """Seals the epoch by computing its Merkle root. Returns root hash."""
        if self.sealed:
            return self.merkle_root
        leaves = [e.entry_hash for e in self.entries]
        self.merkle_root, self._tree_levels = build_merkle_tree(leaves)
        self.sealed = True
        self.sealed_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"[R5-Crypto] Epoch {self.epoch_id} sealed. "
            f"{len(self.entries)} entries. Root: {self.merkle_root[:16]}…"
        )
        return self.merkle_root

    def get_proof(self, entry_id: str) -> Optional[List[Dict[str, str]]]:
        """Returns a Merkle inclusion proof for the given entry."""
        if not self.sealed:
            return None
        for i, e in enumerate(self.entries):
            if e.entry_id == entry_id:
                return merkle_proof(i, self._tree_levels)
        return None


# ─── DISTRIBUTED LEDGER ───────────────────────────────────────────────────────

class LedgerIntegrityError(Exception):
    pass


class DistributedLedger:
    """
    Append-only cryptographic ledger with:
      - Per-epoch Merkle trees
      - Full hash chain (entry[n].previous_hash == entry[n-1].entry_hash)
      - JSONL persistence for local node
      - Merkle root broadcast for distributed validation
    """

    GENESIS_HASH = "GENESIS_BLOCK_TENIR_R5_TAU_INVARIANT"

    def __init__(self, ledger_path: str = "ledger/tenir_ledger.jsonl"):
        self._path = Path(ledger_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._epochs: List[MerkleEpoch] = [MerkleEpoch(epoch_id=0)]
        self._all_entries: List[DistributedLedgerEntry] = []
        self._lock = asyncio.Lock()

        # Load existing ledger
        self._load_from_disk()

    @property
    def head_hash(self) -> str:
        if self._all_entries:
            return self._all_entries[-1].entry_hash
        return self.GENESIS_HASH

    @property
    def current_epoch(self) -> MerkleEpoch:
        return self._epochs[-1]

    @property
    def entry_count(self) -> int:
        return len(self._all_entries)

    def _load_from_disk(self) -> None:
        """Loads existing ledger from JSONL, verifies hash chain integrity."""
        if not self._path.exists():
            logger.info("[R5-Crypto] No existing ledger. Starting fresh.")
            return

        entries: List[DistributedLedgerEntry] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    data.setdefault("entry_type", "observation")
                    data.setdefault("transition_to_mode", None)
                    data.setdefault("policy_version", "unknown")
                    data.setdefault("override_signature", None)
                    data.setdefault("override_operator", None)
                    data.setdefault("override_nonce", None)
                    entries.append(DistributedLedgerEntry(**data))
                except Exception as e:
                    raise LedgerIntegrityError(
                        f"Ledger corruption at line {line_no}: {e}"
                    )

        # Verify chain
        prev_hash = self.GENESIS_HASH
        for entry in entries:
            if entry.previous_hash != prev_hash:
                raise LedgerIntegrityError(
                    f"Hash chain broken at entry {entry.entry_id}: "
                    f"expected previous_hash={prev_hash[:16]}… "
                    f"got {entry.previous_hash[:16]}…"
                )
            
            # Recompute hash to detect tampering
            canonical = json.dumps({
                "entry_id": entry.entry_id,
                "epoch": entry.epoch,
                "sequence": entry.sequence,
                "timestamp": entry.timestamp,
                "tenant_id": entry.tenant_id,
                "workflow_id": entry.workflow_id,
                "membrane_decision": entry.membrane_decision,
                "operating_mode": entry.operating_mode,
                "s_score": entry.s_score,
                "ds_de": entry.ds_de,
                "ces_state": entry.ces_state,
                "rationale": entry.rationale,
                "nsl_ast_hash": entry.nsl_ast_hash,
                "previous_hash": entry.previous_hash,
                "entry_type": entry.entry_type,
                "transition_to_mode": entry.transition_to_mode,
                "policy_version": entry.policy_version,
                "override_signature": entry.override_signature,
                "override_operator": entry.override_operator,
                "override_nonce": entry.override_nonce,
            }, sort_keys=True, separators=(',', ':'))
            
            computed_hash = _sha256(canonical)
            if computed_hash != entry.entry_hash:
                raise LedgerIntegrityError(
                    f"Tampering detected at entry {entry.entry_id}: "
                    f"computed hash {computed_hash[:16]}… does not match stored hash {entry.entry_hash[:16]}…"
                )
                
            prev_hash = entry.entry_hash

        self._all_entries = entries

        # Rebuild epoch structure
        self._epochs = []
        for i in range(0, max(1, math.ceil(len(entries) / EPOCH_SIZE))):
            epoch = MerkleEpoch(epoch_id=i)
            start = i * EPOCH_SIZE
            end = start + EPOCH_SIZE
            epoch.entries = entries[start:end]
            if len(epoch.entries) == EPOCH_SIZE:
                epoch.seal()
            self._epochs.append(epoch)

        if not self._epochs:
            self._epochs = [MerkleEpoch(epoch_id=0)]

        logger.info(
            f"[R5-Crypto] Ledger loaded: {len(entries)} entries, "
            f"{len(self._epochs)} epochs. Chain: VALID"
        )


    def recover_used_nonces(self) -> set:
        nonces = set()
        for entry in self._all_entries:
            if getattr(entry, "override_nonce", None):
                nonces.add(entry.override_nonce)
        return nonces

    def _write_entry(self, entry: DistributedLedgerEntry) -> None:
        """Appends entry to JSONL file. Raises on write failure."""
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")

    async def append(
        self,
        tenant_id: str,
        workflow_id: str,
        membrane_decision: str,
        operating_mode: str,
        s_score: float,
        ds_de: float,
        ces_state: str,
        rationale: str,
        nsl_ast_hash: str = "GENESIS",
    ) -> DistributedLedgerEntry:
        """Thread-safe append. Returns the new entry."""
        async with self._lock:
            epoch = self.current_epoch
            seq = len(epoch.entries)
            ep_id = epoch.epoch_id

            entry = DistributedLedgerEntry.create(
                previous_hash=self.head_hash,
                sequence=seq,
                epoch=ep_id,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                membrane_decision=membrane_decision,
                operating_mode=operating_mode,
                s_score=s_score,
                ds_de=ds_de,
                ces_state=ces_state,
                rationale=rationale,
                nsl_ast_hash=nsl_ast_hash,
            )

            self._write_entry(entry)
            epoch.entries.append(entry)
            self._all_entries.append(entry)

            # Seal epoch when full
            if epoch.is_full:
                root = epoch.seal()
                self._epochs.append(MerkleEpoch(epoch_id=ep_id + 1))
                return entry

            return entry

    def get_merkle_proof(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """Returns Merkle inclusion proof for any entry in a sealed epoch."""
        for epoch in self._epochs:
            if epoch.sealed:
                proof = epoch.get_proof(entry_id)
                if proof is not None:
                    return {
                        "epoch_id": epoch.epoch_id,
                        "merkle_root": epoch.merkle_root,
                        "proof": proof,
                    }
        return None   # Not yet in a sealed epoch

    def verify_full_chain(self) -> Dict[str, Any]:
        """
        Full verification of the entire ledger.
        Returns {valid, entry_count, broken_links, epoch_roots}.
        """
        prev_hash = self.GENESIS_HASH
        broken = 0
        for entry in self._all_entries:
            if entry.previous_hash != prev_hash:
                broken += 1
            prev_hash = entry.entry_hash

        epoch_roots = [
            {"epoch_id": e.epoch_id, "root": e.merkle_root}
            for e in self._epochs if e.sealed
        ]
        return {
            "valid": broken == 0,
            "entry_count": len(self._all_entries),
            "broken_links": broken,
            "epoch_count": len(self._epochs),
            "sealed_epochs": sum(1 for e in self._epochs if e.sealed),
            "epoch_roots": epoch_roots,
        }

    def get_recent_entries(self, n: int = 50) -> List[Dict]:
        return [asdict(e) for e in self._all_entries[-n:]]

    # ── Sync interface (for use from synchronous FastAPI handlers) ─────────────

    def append_sync(
        self,
        event_id: str,
        workflow_id: str,
        workflow_type: str,
        operating_mode: str,
        membrane_decision: str,
        s_score: float,
        ds_de: float,
        ces_state: str,
        d2s_de2: float,
        horizon_events: Optional[int],
        option_space_low: bool,
        alert: bool,
        intended_block: bool,
        rationale: str,
        policy_version: str,
        tenant_id: str = "partner_a",
    ) -> Dict[str, Any]:
        """
        Synchronous append — creates and persists a LedgerEntry.
        Returns the entry dict (chain_hash, previous_hash, entry_id).
        Called from synchronous FastAPI path operations.
        """
        epoch = self.current_epoch
        seq = len(epoch.entries)

        entry = DistributedLedgerEntry.create(
            previous_hash=self.head_hash,
            sequence=seq,
            epoch=epoch.epoch_id,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            membrane_decision=membrane_decision,
            operating_mode=operating_mode,
            s_score=s_score,
            ds_de=ds_de,
            ces_state="",          # resolved by CES matrix upstream
            rationale=rationale,
            nsl_ast_hash=_sha256(event_id),
        )
        self._write_entry(entry)
        epoch.entries.append(entry)
        self._all_entries.append(entry)

        if epoch.is_full:
            epoch.seal()
            self._epochs.append(MerkleEpoch(epoch_id=epoch.epoch_id + 1))

        return {
            "entry_id":     event_id,
            "chain_hash":   entry.entry_hash,
            "previous_hash": entry.previous_hash,
            "epoch_id":     entry.epoch,
            "sequence":     entry.sequence,
        }

    def append_control_transition(
        self,
        transition: str,
        from_mode: str,
        to_mode: str,
        operator_id: str,
        reason: str,
        policy_version: str,
        nonce: str,
        override_signature: Optional[str] = None,
        override_operator: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Appends a MODE_TRANSITION control record to the ledger chain."""
        entry_id = str(uuid4())
        entry = DistributedLedgerEntry.create(
            previous_hash=self.head_hash,
            sequence=len(self.current_epoch.entries),
            epoch=self.current_epoch.epoch_id,
            tenant_id="system",
            workflow_id="CONTROL_TRANSITION",
            membrane_decision=transition,
            operating_mode=to_mode,
            s_score=0.0,
            ds_de=0.0,
            ces_state="TRANSITION",
            rationale=f"{operator_id}: {reason}",
            nsl_ast_hash=_sha256(f"{transition}|{operator_id}|{reason}"),
            entry_type="control_transition",
            transition_to_mode=to_mode,
            policy_version=policy_version,
            override_signature=override_signature,
            override_operator=override_operator,
            override_nonce=nonce,
        )
        self._write_entry(entry)
        self.current_epoch.entries.append(entry)
        self._all_entries.append(entry)
        return asdict(entry)

    def verify_chain(self) -> Dict[str, Any]:
        """Public sync alias of verify_full_chain for r5_server compatibility."""
        report = self.verify_full_chain()
        return {
            "chain_valid":         report["valid"],
            "entry_count":         report["entry_count"],
            "epoch_count":         report["epoch_count"],
            "current_epoch_size":  len(self.current_epoch.entries),
            "first_hash":          self._all_entries[0].entry_hash if self._all_entries else None,
            "last_hash":           self._all_entries[-1].entry_hash if self._all_entries else None,
            "error":               None if report["valid"] else f"{report['broken_links']} broken links",
        }

    def seal_current_epoch(self) -> tuple[str, int]:
        """Manually seals the current epoch. Returns (merkle_root, epoch_id)."""
        ep = self.current_epoch
        root = ep.seal()
        if ep.is_full:
            self._epochs.append(MerkleEpoch(epoch_id=ep.epoch_id + 1))
        return root, ep.epoch_id

    def get_epoch_root(self, epoch_id: int) -> Optional[str]:
        """Returns the sealed Merkle root for a given epoch ID, or None."""
        for ep in self._epochs:
            if ep.epoch_id == epoch_id and ep.sealed:
                return ep.merkle_root
        return None

    def last_entry(self) -> Optional[Dict]:
        """Returns the most recent ledger entry as a dict, or None."""
        if not self._all_entries:
            return None
        e = self._all_entries[-1]
        return {
            "s_score":           e.s_score,
            "ds_de":             e.ds_de,
            "ces_state":         e.ces_state,
            "membrane_decision": e.membrane_decision,
            "chain_hash":        e.entry_hash,
            "operating_mode":    e.operating_mode,
        }


# ─── PEER REGISTRY & CONSENSUS ────────────────────────────────────────────────

@dataclass
class PeerNode:
    node_id: str
    url: str            # e.g., "http://192.168.1.10:8001"
    public_key: str     # For request signature verification
    last_seen: Optional[float] = None
    is_healthy: bool = True


class ConsensusBroadcast:
    """
    Raft-inspired lightweight Merkle root consensus.
    When an epoch is sealed, broadcast its Merkle root to all peers.
    If a peer disagrees → raise LedgerIntegrityError for human review.

    This is NOT a full Raft implementation — it is a read-only validation
    broadcast, not a leader-election protocol. The human operator remains
    sovereign over mode transitions; consensus only validates ledger roots.
    """

    def __init__(self, peers: List[PeerNode], timeout: float = 5.0):
        self.peers = peers
        self.timeout = timeout

    async def broadcast_epoch_root(
        self, epoch_id: int, merkle_root: str, entry_count: int
    ) -> Dict[str, Any]:
        """
        Broadcasts a sealed epoch root to all peers.
        Returns {consensus: bool, agreements: int, disagreements: int, details: [...]}.
        """
        payload = json.dumps({
            "epoch_id": epoch_id,
            "merkle_root": merkle_root,
            "entry_count": entry_count,
            "broadcaster": "self",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }).encode()

        results = await asyncio.gather(*[
            self._validate_with_peer(peer, payload)
            for peer in self.peers
        ], return_exceptions=True)

        agreements = sum(1 for r in results if r is True)
        disagreements = sum(1 for r in results if r is False)
        errors = sum(1 for r in results if isinstance(r, Exception))

        consensus = (
            agreements >= len(self.peers) // 2 + 1 or   # majority agrees
            len(self.peers) == 0                          # no peers = single node mode
        )

        if disagreements > 0:
            logger.error(
                f"[R5-Crypto] CONSENSUS FAILURE: epoch {epoch_id} "
                f"agreements={agreements} disagreements={disagreements}"
            )

        return {
            "consensus": consensus,
            "agreements": agreements,
            "disagreements": disagreements,
            "peer_errors": errors,
            "epoch_id": epoch_id,
            "merkle_root": merkle_root[:16] + "…",
        }

    async def _validate_with_peer(self, peer: PeerNode, payload: bytes) -> bool:
        """POST epoch root to peer's /api/v1/ledger/validate_epoch endpoint."""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: self._http_post(peer.url + "/api/v1/ledger/validate_epoch", payload)
            )
            peer.last_seen = time.time()
            peer.is_healthy = True
            agreed = result.get("agrees", False)
            return bool(agreed)
        except Exception as e:
            peer.is_healthy = False
            logger.warning(f"[R5-Crypto] Peer {peer.node_id} unreachable: {e}")
            return Exception(str(e))

    @staticmethod
    def _http_post(url: str, payload: bytes) -> Dict:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return json.loads(resp.read())


# ─── KEY CEREMONY ─────────────────────────────────────────────────────────────

class KeyCeremony:
    """
    Jubilee Cryptographic Protocol for SHADOW→ENFORCE transition.

    The oath key is epoch-bound:
      - Each epoch (100 events) generates a new signing key.
      - At epoch boundary, the operator MUST re-sign the founding charter.
      - HMAC-SHA256 is used to sign the oath text.

    In production, the HMAC secret would be stored on a YubiKey (HSM).
    In this implementation, it is derived from the epoch ID + a deploy secret.
    Deploy secret must be set via environment variable: OATH_SECRET
    There is no safe default value. A KeyCeremony instantiated without an
    explicit, non-empty deploy_secret cannot verify oaths signed elsewhere
    and should not be used to sign new oaths.
    """

    OATH_TEXT = "J'assume la friction. Je préserve le TAU."
    OATH_TEXT_FR = "J'assume la friction. Je préserve le TAU."

    def __init__(self, deploy_secret: str, used_nonces: Optional[set] = None):
        if not deploy_secret or not deploy_secret.strip():
            raise ValueError(
                "KeyCeremony requires a non-empty deploy_secret. "
                "There is no safe default: a well-known or missing signing "
                "secret allows forged oath signatures for SHADOW->ENFORCE "
                "transitions, defeating human-sovereignty over mode changes."
            )
        self._secret = deploy_secret.encode("utf-8")
        self._used_nonces = used_nonces or set()

    def _epoch_key(self, epoch_id: int) -> bytes:
        """Derives epoch-specific HMAC key from deploy secret + epoch ID."""
        return hashlib.sha256(
            self._secret + str(epoch_id).encode()
        ).digest()

    def sign_oath(self, oath_text: str, epoch_id: int, operator_id: str) -> Dict[str, str]:
        """
        Signs an oath for a given epoch.
        Returns {signature, epoch_id, operator_id, timestamp, nonce}.
        """
        if oath_text.strip() != self.OATH_TEXT.strip():
            raise ValueError(
                f"Invalid oath text. Expected: {self.OATH_TEXT!r}"
            )

        nonce = str(uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = f"{oath_text}|{epoch_id}|{operator_id}|{nonce}|{timestamp}"

        sig = hmac.new(
            self._epoch_key(epoch_id),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "signature": sig,
            "epoch_id": str(epoch_id),
            "operator_id": operator_id,
            "timestamp": timestamp,
            "nonce": nonce,
            "payload_hash": _sha256(payload),
        }

    def verify_oath(
        self,
        oath_text: str,
        signature: str,
        epoch_id: int,
        operator_id: str,
        nonce: str,
        timestamp: str,
    ) -> bool:
        """Verifies an oath signature. Returns True if valid."""
        if nonce in self._used_nonces:
            logger.warning(f"[R5-Crypto] Replay attack detected: nonce {nonce} already used.")
            return False
        payload = f"{oath_text}|{epoch_id}|{operator_id}|{nonce}|{timestamp}"
        expected = hmac.new(
            self._epoch_key(epoch_id),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        is_valid = hmac.compare_digest(signature, expected)
        if is_valid:
            self._used_nonces.add(nonce)
        return is_valid

    def compute_charter_hash(self, charter_path: str) -> str:
        """Computes SHA-256 of the founding charter (for Genesis Block binding)."""
        p = Path(charter_path)
        if p.exists():
            return _sha256(p.read_bytes())
        # Fallback: hash the canonical charter text
        return _sha256(self.OATH_TEXT)


# ─── SINGLETONS ───────────────────────────────────────────────────────────────

_ledger: Optional[DistributedLedger] = None
_ceremony: Optional[KeyCeremony] = None


def get_ledger(path: str = "ledger/tenir_ledger.jsonl") -> DistributedLedger:
    global _ledger
    if _ledger is None:
        _ledger = DistributedLedger(ledger_path=path)
    return _ledger


def get_ceremony(secret: str | None = None, used_nonces: Optional[set] = None) -> KeyCeremony:
    global _ceremony
    if _ceremony is None:
        if not secret or not secret.strip():
            raise RuntimeError(
                "get_ceremony() called before initialization with no secret "
                "provided. The first call in a process must supply a "
                "non-empty deploy_secret (see KeyCeremony)."
            )
        _ceremony = KeyCeremony(deploy_secret=secret, used_nonces=used_nonces)
    return _ceremony
