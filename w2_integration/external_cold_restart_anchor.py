"""JW-10 independent *cold-restart* anchor verification — laboratory interface.

This module VERIFYs a separately trusted, live-current Ed25519 anchor. It
DOES NOT mint anchors, update a remote monotonic store, protect live writes,
make Kernel and Witness transactions atomic, or grant any dispatch right.

A signed anchor replayed by a dishonest/stale reader is NOT freshness proof.
The reader must be a separately operated monotonic authority with an
independent key and live-current/freshness semantics. In-memory stub tests
prove only the local comparison, not the existence of such infrastructure.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SCHEMA="tenir.jw10.external-current-anchor.v1"


class ExternalAnchorHold(RuntimeError):
    """Missing, forged, stale, or mismatching external checkpoint: HOLD."""


class CurrentAnchorReader(Protocol):
    """Trusted external provider; read_current MUST be fresh and non-rollbackable."""

    def read_current(self, subject: str) -> dict:
        """Return latest signed {payload, signature_hex} or raise on outage."""


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",",":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _cell(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    if isinstance(value,(str,int,float)):
        return value
    raise ExternalAnchorHold("unsupported SQLite cell in checkpoint")


def sqlite_logical_fingerprint(path: str) -> str:
    """Read-only logical snapshot fingerprint; no WAL backup or user table writes.

    Intended for a QUIESCENT single-host cold-restart check, not a live
    multi-database atomic snapshot. A concurrent writer invalidates the
    out-of-band quiescence precondition.
    """
    if not isinstance(path,str) or not os.path.isfile(path):
        raise ExternalAnchorHold("SQLite database absent: HOLD")
    uri="file:"+quote(os.path.abspath(path),safe="/")+ "?mode=ro"
    try:
        with sqlite3.connect(uri,uri=True,timeout=3) as conn:
            conn.execute("PRAGMA query_only=ON")
            names=[r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            rows=[]
            for name in names:
                quoted='"'+name.replace('"','""')+'"'
                info=conn.execute("PRAGMA table_info("+quoted+")").fetchall()
                columns=[i[1] for i in info]
                all_rows=[
                    [_cell(v) for v in row]
                    for row in conn.execute("SELECT * FROM "+quoted)
                ]
                all_rows.sort(key=lambda x:_canonical(x))
                rows.append([name,columns,all_rows])
        return hashlib.sha256(_canonical(rows)).hexdigest()
    except (sqlite3.Error,ValueError,TypeError,OSError) as exc:
        raise ExternalAnchorHold("cannot fingerprint SQLite snapshot") from exc


@dataclass(frozen=True)
class ExternalAnchorReceipt:
    subject: str
    generation: int
    kernel_sha256: str
    witness_sha256: str
    state: str = "COLD_RESTART_MATCHED_ONLY"
    execution_authorized: bool = False
    normative_verified: bool = False


class ExternalColdRestartAnchorGate:
    """Check a *separately trusted CURRENT anchor* against two local DBs."""

    def __init__(self, *, reader: CurrentAnchorReader,
                 trusted_public_key: bytes,
                 required_topology: str):
        if reader is None or not callable(getattr(reader,"read_current",None)):
            raise ExternalAnchorHold("independent live-current anchor reader required")
        if (not isinstance(trusted_public_key,bytes)
                or len(trusted_public_key)!=32):
            raise ExternalAnchorHold("independently provisioned Ed25519 root required")
        if not isinstance(required_topology,str) or not required_topology:
            raise ExternalAnchorHold("explicit topology required")
        self.reader=reader
        self.key=Ed25519PublicKey.from_public_bytes(trusted_public_key)
        self.topology=required_topology

    def verify_cold_restart(self, *, subject: str, kernel_db: str,
                            witness_db: str, min_generation: int = 1
                            ) -> ExternalAnchorReceipt:
        if (not isinstance(subject,str) or not subject
                or type(min_generation) is not int or min_generation<1):
            raise ExternalAnchorHold("invalid checkpoint subject/generation")
        try:
            envelope=self.reader.read_current(subject)
        except Exception as exc:
            raise ExternalAnchorHold("external current anchor unavailable") from exc
        if not isinstance(envelope,dict) or set(envelope)!={"payload","signature_hex"}:
            raise ExternalAnchorHold("invalid anchor envelope")
        payload=envelope["payload"]
        if not isinstance(payload,dict) or set(payload)!={
                "schema","subject","topology_id","generation",
                "kernel_sha256","witness_sha256"}:
            raise ExternalAnchorHold("invalid anchor payload")
        if (payload["schema"]!=SCHEMA or payload["subject"]!=subject
                or payload["topology_id"]!=self.topology
                or type(payload["generation"]) is not int
                or payload["generation"]<min_generation):
            raise ExternalAnchorHold("anchor identity, topology or generation mismatch")
        for key in ("kernel_sha256","witness_sha256"):
            val=payload[key]
            if (not isinstance(val,str) or len(val)!=64
                    or any(char not in "0123456789abcdef" for char in val)):
                raise ExternalAnchorHold("invalid anchor digest")
        try:
            sig=bytes.fromhex(envelope["signature_hex"])
            self.key.verify(sig,_canonical(payload))
        except (ValueError,TypeError,InvalidSignature) as exc:
            raise ExternalAnchorHold("external current anchor signature invalid") from exc
        current_kernel=sqlite_logical_fingerprint(kernel_db)
        current_witness=sqlite_logical_fingerprint(witness_db)
        if (payload["kernel_sha256"]!=current_kernel
                or payload["witness_sha256"]!=current_witness):
            raise ExternalAnchorHold("JW-10 joint database rollback or drift: HOLD")
        return ExternalAnchorReceipt(
            subject=subject,generation=payload["generation"],
            kernel_sha256=current_kernel,witness_sha256=current_witness,
        )
