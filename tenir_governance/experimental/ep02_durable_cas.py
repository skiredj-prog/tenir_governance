"""Experimental durable EP-02 request outcomes on a local SQLite authority.

The checkpoint and immutable request outcome commit in one SQLite transaction.
This backend is opt-in, single-host and detached from the governor. SQLite WAL
does not provide inter-host consensus, external anti-rollback or atomicity with
irreversible effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_rotation import (
    TrustCheckpoint,
    validate_successor,
)
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
    verify_bound_snapshot,
)


SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ENVELOPE_BYTES = 1_048_576


class DurableCASResult(str, Enum):
    APPLIED = "APPLIED"
    UNCHANGED = "UNCHANGED"
    CONFLICT = "CONFLICT"
    NOT_INITIALIZED = "NOT_INITIALIZED"
    REJECTED = "REJECTED"


class LookupStatus(str, Enum):
    RECORDED = "RECORDED"
    NOT_RECORDED = "NOT_RECORDED"


class RequestIdConflict(RuntimeError):
    """A durable request ID was reused with different immutable context."""


class StoreNotProvisioned(RuntimeError):
    """The authority database is missing or has not been provisioned."""


class StoreIntegrityError(RuntimeError):
    """The authority database, schema or durable settings fail closed."""


class UnsupportedLegacyWriter(RuntimeError):
    """The unconditional legacy writer is excluded from this authority."""


@dataclass(frozen=True)
class DurableCASResponse:
    request_id: str
    request_digest: str
    operation: str
    result: DurableCASResult
    reason: str
    observed_checkpoint: TrustCheckpoint | None
    result_checkpoint: TrustCheckpoint | None
    current_checkpoint: TrustCheckpoint | None
    sequence: int
    replayed: bool = False


@dataclass(frozen=True)
class LookupResponse:
    status: LookupStatus
    current_checkpoint: TrustCheckpoint | None
    response: DurableCASResponse | None


def _canonical_json(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _validate_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _validate_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _checkpoint(revision, fingerprint) -> TrustCheckpoint | None:
    if revision is None and fingerprint is None:
        return None
    if type(revision) is not int or revision < 1:
        raise StoreIntegrityError("invalid stored checkpoint revision")
    try:
        fingerprint = _validate_digest(fingerprint, "stored checkpoint fingerprint")
    except ValueError as error:
        raise StoreIntegrityError("invalid stored checkpoint fingerprint") from error
    return TrustCheckpoint(revision, fingerprint)


def _validate_checkpoint(value: TrustCheckpoint) -> TrustCheckpoint:
    if not isinstance(value, TrustCheckpoint):
        raise TypeError("expected TrustCheckpoint")
    if type(value.revision) is not int or value.revision < 1:
        raise ValueError("invalid checkpoint revision")
    _validate_digest(value.snapshot_fingerprint, "checkpoint fingerprint")
    return TrustCheckpoint(value.revision, value.snapshot_fingerprint)


def _freeze_snapshot(value: SignedTrustState) -> SignedTrustState:
    if not isinstance(value, SignedTrustState):
        raise TypeError("expected SignedTrustState")
    state = value.state
    if not isinstance(state, TrustState):
        raise TypeError("invalid trust state")
    if type(state.revision) is not int or state.revision < 1:
        raise ValueError("invalid trust revision")
    if not isinstance(state.policy_version, str) or not state.policy_version:
        raise ValueError("invalid policy version")
    mandates = tuple(tuple(item) for item in state.mandates)
    revoked = tuple(state.revoked)
    if any(len(item) != 2 or not all(isinstance(x, str) and x for x in item)
           for item in mandates):
        raise ValueError("invalid mandate")
    if len(set(mandates)) != len(mandates):
        raise ValueError("duplicate mandate")
    if any(not isinstance(item, str) or not item for item in revoked):
        raise ValueError("invalid revocation")
    if len(set(revoked)) != len(revoked):
        raise ValueError("duplicate revocation")
    if not isinstance(value.signer_id, str) or not _ID.fullmatch(value.signer_id):
        raise ValueError("invalid signer identity")
    if not isinstance(value.signature, bytes) or len(value.signature) != 64:
        raise ValueError("invalid Ed25519 signature")
    entries = tuple((name, bytes(raw)) for name, raw in value.issuer_keys)
    frozen = SignedTrustState(
        TrustState(state.revision, state.policy_version, mandates, revoked),
        bytes(value.signature), value.signer_id, entries,
    )
    payload = bound_snapshot_bytes(frozen)
    if len(payload) + len(frozen.signature) > _MAX_ENVELOPE_BYTES:
        raise ValueError("snapshot envelope too large")
    return frozen


def _root_records(roots: Mapping[str, Ed25519PublicKey]):
    if not isinstance(roots, Mapping) or not roots:
        raise ValueError("at least one root is required")
    records = []
    for name, key in roots.items():
        _validate_name(name, "root identity")
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("invalid root key")
        raw = key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        records.append((name, raw.hex()))
    if len({name for name, _ in records}) != len(records):
        raise ValueError("duplicate root identity")
    return sorted(records)


class DurableAnchorCAS:
    """Single-host request-idempotent CAS backed by one SQLite database."""

    def __init__(self, path, authority_id, profile_id, generation, roots, profile_digest):
        self.path = Path(path)
        self.authority_id = authority_id
        self.trust_profile_id = profile_id
        self.trust_generation = generation
        self._roots = roots
        self._profile_digest = profile_digest
        self._roots_json = _canonical_json(_root_records(roots)).decode("utf-8")

    @classmethod
    def provision(
        cls, path, *, authority_id: str, trust_profile_id: str,
        trust_generation: int, roots: Mapping[str, Ed25519PublicKey],
    ) -> "DurableAnchorCAS":
        path = Path(path)
        _validate_name(authority_id, "authority identity")
        _validate_name(trust_profile_id, "trust profile identity")
        if type(trust_generation) is not int or trust_generation < 1:
            raise ValueError("invalid trust generation")
        records = _root_records(roots)
        cls._validate_storage_path(path, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(path, flags, 0o600)
        os.close(fd)
        roots_json = _canonical_json(records).decode("utf-8")
        profile_digest = sha256(_canonical_json({
            "profile_id": trust_profile_id,
            "generation": trust_generation,
            "roots": records,
        })).hexdigest()
        connection = None
        try:
            connection = cls._connect_path(path, create=True)
            connection.executescript("""
                CREATE TABLE authority (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    authority_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    profile_generation INTEGER NOT NULL,
                    profile_digest TEXT NOT NULL,
                    roots_json TEXT NOT NULL
                );
                CREATE TABLE checkpoint (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    fingerprint TEXT NOT NULL
                );
                CREATE TABLE requests (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    result TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    observed_revision INTEGER,
                    observed_fingerprint TEXT,
                    result_revision INTEGER,
                    result_fingerprint TEXT,
                    successor_fingerprint TEXT NOT NULL,
                    envelope_json TEXT NOT NULL
                );
                CREATE TRIGGER authority_no_update
                BEFORE UPDATE ON authority
                BEGIN SELECT RAISE(ABORT, 'authority rows are immutable'); END;
                CREATE TRIGGER authority_no_delete
                BEFORE DELETE ON authority
                BEGIN SELECT RAISE(ABORT, 'authority rows are immutable'); END;
                CREATE TRIGGER requests_no_update
                BEFORE UPDATE ON requests
                BEGIN SELECT RAISE(ABORT, 'request outcomes are immutable'); END;
                CREATE TRIGGER requests_no_delete
                BEFORE DELETE ON requests
                BEGIN SELECT RAISE(ABORT, 'request outcomes are immutable'); END;
            """)
            connection.execute(
                "INSERT INTO authority VALUES (1, ?, ?, ?, ?, ?, ?)",
                (SCHEMA_VERSION, authority_id, trust_profile_id,
                 trust_generation, profile_digest, roots_json),
            )
            connection.commit()
        except Exception:
            if connection is not None:
                connection.close()
            raise
        finally:
            if connection is not None:
                connection.close()
        return cls.open(path)

    @classmethod
    def open(cls, path) -> "DurableAnchorCAS":
        path = Path(path)
        cls._validate_storage_path(path, must_exist=True)
        connection = cls._connect_path(path, create=False)
        try:
            # Keep every integrity read on one WAL snapshot.  Without an
            # explicit read transaction, a concurrent writer can commit
            # between the checkpoint read and the request-history scan and
            # make a healthy database appear internally inconsistent.
            connection.execute("BEGIN")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreIntegrityError("SQLite integrity check failed")
            row = connection.execute(
                "SELECT schema_version, authority_id, profile_id, "
                "profile_generation, profile_digest, roots_json "
                "FROM authority WHERE singleton = 1"
            ).fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                raise StoreIntegrityError("missing or unsupported authority schema")
            schema_version, authority_id, profile_id, generation, profile_digest, roots_json = row
            _validate_name(authority_id, "stored authority identity")
            _validate_name(profile_id, "stored trust profile identity")
            if type(generation) is not int or generation < 1:
                raise StoreIntegrityError("invalid stored trust generation")
            try:
                records = json.loads(roots_json)
                if not isinstance(records, list) or not records:
                    raise ValueError
                canonical = []
                roots = {}
                for item in records:
                    if (not isinstance(item, list) or len(item) != 2
                            or not isinstance(item[0], str)
                            or not isinstance(item[1], str)):
                        raise ValueError
                    name, raw_hex = item
                    _validate_name(name, "stored root identity")
                    raw = bytes.fromhex(raw_hex)
                    if len(raw) != 32 or raw_hex != raw_hex.lower():
                        raise ValueError
                    roots[name] = Ed25519PublicKey.from_public_bytes(raw)
                    canonical.append((name, raw_hex))
                if len(roots) != len(records) or canonical != sorted(canonical):
                    raise ValueError
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                raise StoreIntegrityError("invalid stored root profile") from error
            expected_profile = sha256(_canonical_json({
                "profile_id": profile_id,
                "generation": generation,
                "roots": canonical,
            })).hexdigest()
            if profile_digest != expected_profile:
                raise StoreIntegrityError("trust profile digest mismatch")
            cls._validate_schema_guards(connection)
            current = cls._read_checkpoint(connection)
            cls._validate_request_rows(
                connection,
                authority_id=authority_id,
                profile_id=profile_id,
                generation=generation,
                profile_digest=profile_digest,
                roots=roots,
                current=current,
            )
            connection.commit()
            return cls(path, authority_id, profile_id, generation, roots, profile_digest)
        except StoreIntegrityError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise StoreIntegrityError("authority database validation failed") from error
        finally:
            connection.close()

    @staticmethod
    def _validate_storage_path(path: Path, *, must_exist: bool):
        existing_parents = (parent for parent in path.parents if parent.exists())
        sidecars = (Path(f"{path}-wal"), Path(f"{path}-shm"))
        if (
            path.is_symlink()
            or any(parent.is_symlink() for parent in existing_parents)
            or any(sidecar.is_symlink() for sidecar in sidecars)
        ):
            raise StoreIntegrityError("symlinked authority storage is not allowed")
        if must_exist and (not path.exists() or not path.is_file()):
            raise StoreNotProvisioned("authority database is not provisioned")
        if not must_exist and path.exists():
            raise FileExistsError(path)

    @staticmethod
    def _connect_path(path: Path, *, create: bool):
        if not create and not path.exists():
            raise StoreNotProvisioned("authority database is not provisioned")
        mode = "rwc" if create else "rw"
        uri = path.resolve().as_uri() + f"?mode={mode}"
        try:
            connection = sqlite3.connect(
                uri, uri=True, timeout=5.0, isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            connection.execute("PRAGMA synchronous=FULL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
            if str(journal).lower() != "wal" or synchronous != 2:
                connection.close()
                raise StoreIntegrityError("required SQLite durability mode unavailable")
            return connection
        except sqlite3.Error as error:
            raise StoreIntegrityError("authority database unavailable") from error

    def _connect(self):
        self._validate_storage_path(self.path, must_exist=True)
        connection = self._connect_path(self.path, create=False)
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreIntegrityError("SQLite integrity check failed")
            row = connection.execute(
                "SELECT schema_version, authority_id, profile_id, "
                "profile_generation, profile_digest, roots_json "
                "FROM authority WHERE singleton = 1"
            ).fetchone()
            expected = (
                SCHEMA_VERSION,
                self.authority_id,
                self.trust_profile_id,
                self.trust_generation,
                self._profile_digest,
                self._roots_json,
            )
            if row != expected:
                raise StoreIntegrityError("authority identity changed after open")
            self._validate_schema_guards(connection)
            return connection
        except StoreIntegrityError:
            connection.close()
            raise
        except sqlite3.Error as error:
            connection.close()
            raise StoreIntegrityError("authority database validation failed") from error

    @staticmethod
    def _read_checkpoint(connection) -> TrustCheckpoint | None:
        row = connection.execute(
            "SELECT revision, fingerprint FROM checkpoint WHERE singleton = 1"
        ).fetchone()
        return None if row is None else _checkpoint(*row)

    @staticmethod
    def _validate_schema_guards(connection):
        required = {
            "authority_no_update", "authority_no_delete",
            "requests_no_update", "requests_no_delete",
        }
        actual = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        if not required.issubset(actual):
            raise StoreIntegrityError("immutable-row guards are missing")

    @staticmethod
    def _snapshot_from_envelope(envelope, roots):
        expected_keys = {
            "version", "domain", "operation", "authority_id", "profile_id",
            "profile_generation", "profile_digest", "expected", "successor",
        }
        if set(envelope) != expected_keys:
            raise ValueError("invalid request envelope fields")
        successor = envelope["successor"]
        if not isinstance(successor, dict) or set(successor) != {
            "signer_id", "signature", "bound_payload", "fingerprint",
        }:
            raise ValueError("invalid successor envelope")
        payload = bytes.fromhex(successor["bound_payload"])
        signature = bytes.fromhex(successor["signature"])
        if len(signature) != 64:
            raise ValueError("invalid stored signature")
        prefix = b"TENIR-EP02-SNAPSHOT-V2\\x00"
        if not payload.startswith(prefix):
            raise ValueError("invalid stored snapshot domain")
        body = json.loads(payload[len(prefix):])
        if not isinstance(body, dict) or set(body) != {"trust", "issuer_keys"}:
            raise ValueError("invalid stored snapshot body")
        trust = body["trust"]
        if not isinstance(trust, dict) or set(trust) != {
            "revision", "policy_version", "mandates", "revoked",
        }:
            raise ValueError("invalid stored trust state")
        issuer_keys = tuple(
            (item[0], bytes.fromhex(item[1])) for item in body["issuer_keys"]
        )
        snapshot = _freeze_snapshot(SignedTrustState(
            TrustState(
                trust["revision"], trust["policy_version"],
                tuple(tuple(item) for item in trust["mandates"]),
                tuple(trust["revoked"]),
            ),
            signature,
            successor["signer_id"],
            issuer_keys,
        ))
        if bound_snapshot_bytes(snapshot) != payload:
            raise ValueError("stored snapshot is not canonical")
        fingerprint = sha256(payload).hexdigest()
        if successor["fingerprint"] != fingerprint:
            raise ValueError("stored successor fingerprint mismatch")
        return snapshot, fingerprint

    @classmethod
    def _validate_request_rows(
        cls, connection, *, authority_id, profile_id, generation,
        profile_digest, roots, current,
    ):
        simulated = None
        for row in connection.execute(
            "SELECT sequence, request_id, request_digest, operation, result, "
            "reason, observed_revision, observed_fingerprint, result_revision, "
            "result_fingerprint, successor_fingerprint, envelope_json "
            "FROM requests ORDER BY sequence"
        ):
            try:
                sequence, request_id, request_digest, operation, result_value = row[:5]
                reason = row[5]
                observed = _checkpoint(row[6], row[7])
                result_checkpoint = _checkpoint(row[8], row[9])
                successor_fingerprint, envelope_json = row[10], row[11]
                if type(sequence) is not int or sequence < 1:
                    raise ValueError
                _validate_name(request_id, "stored request identity")
                _validate_digest(request_digest, "stored request digest")
                if operation not in {"BOOTSTRAP", "CAS"}:
                    raise ValueError
                result = DurableCASResult(result_value)
                _validate_digest(successor_fingerprint, "stored successor fingerprint")
                envelope = json.loads(envelope_json)
                if _canonical_json(envelope).decode("utf-8") != envelope_json:
                    raise ValueError("noncanonical request envelope")
                if sha256(envelope_json.encode("utf-8")).hexdigest() != request_digest:
                    raise ValueError("request digest mismatch")
                if (
                    envelope["version"] != 1
                    or envelope["domain"] != "TENIR-EP02-DURABLE-CAS"
                    or envelope["operation"] != operation
                    or envelope["authority_id"] != authority_id
                    or envelope["profile_id"] != profile_id
                    or envelope["profile_generation"] != generation
                    or envelope["profile_digest"] != profile_digest
                ):
                    raise ValueError("stored request authority mismatch")
                snapshot, derived_fingerprint = cls._snapshot_from_envelope(
                    envelope, roots,
                )
                if successor_fingerprint != derived_fingerprint:
                    raise ValueError("request row successor mismatch")
                expected_value = envelope["expected"]
                if expected_value is not None and (
                    not isinstance(expected_value, dict)
                    or set(expected_value) != {"revision", "fingerprint"}
                ):
                    raise ValueError("invalid stored expected checkpoint")
                expected = None if expected_value is None else _checkpoint(
                    expected_value.get("revision"), expected_value.get("fingerprint"),
                )
                if observed != simulated:
                    raise ValueError("request history observation mismatch")

                derived_checkpoint = simulated
                if operation == "BOOTSTRAP":
                    if expected is not None:
                        raise ValueError("bootstrap must bind uninitialized state")
                    if simulated is not None:
                        derived_result, derived_reason = (
                            DurableCASResult.CONFLICT, "ALREADY_INITIALIZED",
                        )
                    elif not verify_bound_snapshot(snapshot, root_keys=roots):
                        derived_result, derived_reason = (
                            DurableCASResult.REJECTED,
                            "UNVERIFIED_INITIAL_SNAPSHOT",
                        )
                    else:
                        derived_result, derived_reason = (
                            DurableCASResult.APPLIED, "BOOTSTRAPPED",
                        )
                        derived_checkpoint = TrustCheckpoint(
                            snapshot.state.revision, derived_fingerprint,
                        )
                elif simulated is None:
                    derived_result, derived_reason = (
                        DurableCASResult.NOT_INITIALIZED, "NOT_INITIALIZED",
                    )
                elif simulated != expected:
                    derived_result, derived_reason = (
                        DurableCASResult.CONFLICT, "EXPECTED_STATE_MISMATCH",
                    )
                else:
                    ok, derived_reason, candidate = validate_successor(
                        simulated, snapshot, roots=roots,
                    )
                    if not ok:
                        derived_result = DurableCASResult.CONFLICT
                    elif derived_reason == "UNCHANGED":
                        derived_result = DurableCASResult.UNCHANGED
                    else:
                        derived_result = DurableCASResult.APPLIED
                        derived_checkpoint = candidate
                if (
                    result != derived_result
                    or reason != derived_reason
                    or result_checkpoint != derived_checkpoint
                ):
                    raise ValueError("stored request outcome mismatch")
                if result == DurableCASResult.APPLIED:
                    simulated = derived_checkpoint
            except (ValueError, TypeError, KeyError, AttributeError,
                    json.JSONDecodeError) as error:
                raise StoreIntegrityError("invalid durable request row") from error
        if simulated != current:
            raise StoreIntegrityError("checkpoint is not justified by request history")

    def _request_context(self, operation, expected, successor):
        if operation not in {"BOOTSTRAP", "CAS"}:
            raise ValueError("invalid operation")
        if operation == "CAS" and expected is None:
            raise TypeError("CAS requires an explicit expected checkpoint")
        frozen = _freeze_snapshot(successor)
        if expected is not None:
            expected = _validate_checkpoint(expected)
        successor_fingerprint = sha256(bound_snapshot_bytes(frozen)).hexdigest()
        envelope = {
            "version": 1,
            "domain": "TENIR-EP02-DURABLE-CAS",
            "operation": operation,
            "authority_id": self.authority_id,
            "profile_id": self.trust_profile_id,
            "profile_generation": self.trust_generation,
            "profile_digest": self._profile_digest,
            "expected": None if expected is None else {
                "revision": expected.revision,
                "fingerprint": expected.snapshot_fingerprint,
            },
            "successor": {
                "signer_id": frozen.signer_id,
                "signature": frozen.signature.hex(),
                "bound_payload": bound_snapshot_bytes(frozen).hex(),
                "fingerprint": successor_fingerprint,
            },
        }
        encoded = _canonical_json(envelope)
        if len(encoded) > _MAX_ENVELOPE_BYTES:
            raise ValueError("request envelope too large")
        return expected, frozen, successor_fingerprint, encoded.decode("utf-8"), sha256(encoded).hexdigest()

    def cas_request_digest(self, expected, successor) -> str:
        return self._request_context("CAS", expected, successor)[4]

    def bootstrap_request_digest(self, successor) -> str:
        return self._request_context("BOOTSTRAP", None, successor)[4]

    def bootstrap(self, request_id, successor) -> DurableCASResponse:
        return self._execute("BOOTSTRAP", request_id, None, successor)

    def compare_and_swap(self, request_id, expected, successor) -> DurableCASResponse:
        return self._execute("CAS", request_id, expected, successor)

    def _execute(self, operation, request_id, expected, successor):
        request_id = _validate_name(request_id, "request identity")
        expected, successor, successor_fingerprint, envelope_json, digest = (
            self._request_context(operation, expected, successor)
        )
        self._fault("before_transaction")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._fault("after_begin")
            existing = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            self._fault("after_request_lookup")
            current = self._read_checkpoint(connection)
            if existing is not None:
                response = self._response_from_row(existing, current, replayed=True)
                if response.request_digest != digest:
                    connection.rollback()
                    raise RequestIdConflict(
                        "request ID is already bound to different context"
                    )
                connection.commit()
                return response

            observed = current
            result_checkpoint = current
            if operation == "BOOTSTRAP":
                if current is not None:
                    result, reason = DurableCASResult.CONFLICT, "ALREADY_INITIALIZED"
                elif not verify_bound_snapshot(successor, root_keys=self._roots):
                    result, reason = DurableCASResult.REJECTED, "UNVERIFIED_INITIAL_SNAPSHOT"
                else:
                    result, reason = DurableCASResult.APPLIED, "BOOTSTRAPPED"
                    result_checkpoint = TrustCheckpoint(
                        successor.state.revision, successor_fingerprint,
                    )
            elif current is None:
                result, reason = DurableCASResult.NOT_INITIALIZED, "NOT_INITIALIZED"
                result_checkpoint = None
            elif current != expected:
                result, reason = DurableCASResult.CONFLICT, "EXPECTED_STATE_MISMATCH"
            else:
                ok, reason, candidate = validate_successor(
                    current, successor, roots=self._roots,
                )
                if not ok:
                    result, result_checkpoint = DurableCASResult.CONFLICT, current
                elif reason == "UNCHANGED":
                    result, result_checkpoint = DurableCASResult.UNCHANGED, current
                else:
                    result, result_checkpoint = DurableCASResult.APPLIED, candidate

            if result == DurableCASResult.APPLIED:
                connection.execute(
                    "INSERT INTO checkpoint(singleton, revision, fingerprint) "
                    "VALUES(1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                    "revision=excluded.revision, fingerprint=excluded.fingerprint",
                    (result_checkpoint.revision,
                     result_checkpoint.snapshot_fingerprint),
                )
                self._fault("after_checkpoint_update")
            cursor = connection.execute(
                "INSERT INTO requests(request_id, request_digest, operation, result, "
                "reason, observed_revision, observed_fingerprint, result_revision, "
                "result_fingerprint, successor_fingerprint, envelope_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, digest, operation, result.value, reason,
                 None if observed is None else observed.revision,
                 None if observed is None else observed.snapshot_fingerprint,
                 None if result_checkpoint is None else result_checkpoint.revision,
                 None if result_checkpoint is None else result_checkpoint.snapshot_fingerprint,
                 successor_fingerprint, envelope_json),
            )
            sequence = cursor.lastrowid
            self._fault("after_outcome_insert")
            self._fault("before_commit")
            connection.commit()
            self._fault("after_commit_before_reply")
            current = self.get()
            return DurableCASResponse(
                request_id, digest, operation, result, reason, observed,
                result_checkpoint, current, sequence, False,
            )
        except Exception:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()
            raise
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _response_from_row(row, current, *, replayed):
        return DurableCASResponse(
            request_id=row[1], request_digest=row[2], operation=row[3],
            result=DurableCASResult(row[4]), reason=row[5],
            observed_checkpoint=_checkpoint(row[6], row[7]),
            result_checkpoint=_checkpoint(row[8], row[9]),
            current_checkpoint=current, sequence=row[0], replayed=replayed,
        )

    def lookup(self, request_id, request_digest) -> LookupResponse:
        request_id = _validate_name(request_id, "request identity")
        request_digest = _validate_digest(request_digest, "request digest")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            current = self._read_checkpoint(connection)
            connection.commit()
            if row is None:
                return LookupResponse(LookupStatus.NOT_RECORDED, current, None)
            response = self._response_from_row(row, current, replayed=True)
            if response.request_digest != request_digest:
                raise RequestIdConflict(
                    "request ID is already bound to different context"
                )
            return LookupResponse(LookupStatus.RECORDED, current, response)
        finally:
            connection.close()

    def get(self) -> TrustCheckpoint | None:
        connection = self._connect()
        try:
            return self._read_checkpoint(connection)
        finally:
            connection.close()

    def request_count(self) -> int:
        connection = self._connect()
        try:
            return connection.execute("SELECT count(*) FROM requests").fetchone()[0]
        finally:
            connection.close()

    def advance(self, *args, **kwargs):
        raise UnsupportedLegacyWriter(
            "durable authority requires an explicit request ID and expected state"
        )

    def close(self):
        """Connections are per operation; provided for explicit lifecycle symmetry."""

    def _fault(self, point: str):
        """No-op test seam for real process-exit fault injection."""
