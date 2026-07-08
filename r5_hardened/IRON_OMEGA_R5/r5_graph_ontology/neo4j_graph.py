"""
R5 GRAPH ONTOLOGY: Neo4j Schema & Integration Layer
=====================================================
Replaces the V42CIron MVP's in-memory Python dictionaries with a
persistent, distributed Neo4j graph database.

Graph Model:
  (:Institution)─[:HAS_DOMAIN]→(:Domain)─[:CONTAINS]→(:CaseObject)
  (:CaseObject)─[:GENERATES]→(:EventSample)─[:PRODUCES]→(:LedgerEntry)
  (:LedgerEntry)─[:CHAINS_TO]→(:LedgerEntry)
  (:Operator)─[:AUTHORIZED_TRANSITION]→(:ControlTransition)
  (:CASENode)─[:DEPENDS_ON]→(:CASENode)         # CP-Net structure
  (:PolicyRule)─[:GOVERNS]→(:Domain)
  (:PolicyRule)─[:SETS_THRESHOLD]→(:ThresholdConfig)

Why Graph?
  - Multi-dimensional entity relationships (R&D ↔ Procurement ↔ Legal ↔ Budget)
    cannot be represented efficiently in flat tables.
  - CP-Net traversal (who governs whom under what condition) maps directly
    to graph traversal — Neo4j Cypher is the native query language for this.
  - The ledger chain (each entry pointing to previous) is a linked-list graph.
  - Real-time GDS (Graph Data Science) algorithms can detect structural fragility
    across entity clusters before they cascade.
"""

from __future__ import annotations
import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, AsyncGenerator
from uuid import uuid4

logger = logging.getLogger("r5.graph_ontology")

# ─── CYPHER SCHEMA INITIALIZATION ─────────────────────────────────────────────
# Run once on database startup to create indexes and constraints.

SCHEMA_CYPHER: List[str] = [
    # Uniqueness constraints
    "CREATE CONSTRAINT institution_id IF NOT EXISTS FOR (n:Institution) REQUIRE n.institution_id IS UNIQUE",
    "CREATE CONSTRAINT domain_id IF NOT EXISTS FOR (n:Domain) REQUIRE n.domain_id IS UNIQUE",
    "CREATE CONSTRAINT case_id IF NOT EXISTS FOR (n:CaseObject) REQUIRE n.case_id IS UNIQUE",
    "CREATE CONSTRAINT event_id IF NOT EXISTS FOR (n:EventSample) REQUIRE n.event_id IS UNIQUE",
    "CREATE CONSTRAINT ledger_id IF NOT EXISTS FOR (n:LedgerEntry) REQUIRE n.entry_id IS UNIQUE",
    "CREATE CONSTRAINT operator_id IF NOT EXISTS FOR (n:Operator) REQUIRE n.operator_id IS UNIQUE",
    "CREATE CONSTRAINT policy_id IF NOT EXISTS FOR (n:PolicyRule) REQUIRE n.policy_id IS UNIQUE",
    "CREATE CONSTRAINT ces_node_id IF NOT EXISTS FOR (n:CESNode) REQUIRE n.node_id IS UNIQUE",
    # Indexes for query performance
    "CREATE INDEX ledger_timestamp IF NOT EXISTS FOR (n:LedgerEntry) ON (n.timestamp)",
    "CREATE INDEX event_workflow IF NOT EXISTS FOR (n:EventSample) ON (n.workflow_id)",
    "CREATE INDEX case_status IF NOT EXISTS FOR (n:CaseObject) ON (n.status)",
    "CREATE INDEX domain_type IF NOT EXISTS FOR (n:Domain) ON (n.domain_type)",
    # Full-text index for NSL context search
    "CREATE FULLTEXT INDEX ledger_rationale IF NOT EXISTS FOR (n:LedgerEntry) ON EACH [n.rationale]",
]

# ─── CYPHER QUERY LIBRARY ─────────────────────────────────────────────────────

class CypherQueries:
    """
    Canonical Cypher query library for the TENIR ontology.
    All queries are parameterized — no string interpolation, safe against injection.
    """

    # ── Institution & Domain ──────────────────────────────────────────────────

    UPSERT_INSTITUTION = """
    MERGE (i:Institution {institution_id: $institution_id})
    ON CREATE SET
      i.name = $name,
      i.created_at = $created_at,
      i.tau_floor = $tau_floor
    ON MATCH SET
      i.name = $name,
      i.tau_floor = $tau_floor
    RETURN i
    """

    UPSERT_DOMAIN = """
    MERGE (d:Domain {domain_id: $domain_id})
    ON CREATE SET
      d.domain_type = $domain_type,
      d.name = $name,
      d.description = $description,
      d.created_at = $created_at
    WITH d
    MATCH (i:Institution {institution_id: $institution_id})
    MERGE (i)-[:HAS_DOMAIN]->(d)
    RETURN d
    """

    # ── Case Objects ──────────────────────────────────────────────────────────

    CREATE_CASE = """
    MERGE (c:CaseObject {case_id: $case_id})
    ON CREATE SET
      c.case_reference = $case_reference,
      c.case_title = $case_title,
      c.status = $status,
      c.created_at = $created_at,
      c.tenant_id = $tenant_id
    WITH c
    MATCH (d:Domain {domain_id: $domain_id})
    MERGE (d)-[:CONTAINS]->(c)
    RETURN c
    """

    UPDATE_CASE_STATUS = """
    MATCH (c:CaseObject {case_id: $case_id})
    SET c.status = $status, c.updated_at = $updated_at
    RETURN c
    """

    # ── Event Samples ─────────────────────────────────────────────────────────

    CREATE_EVENT = """
    MERGE (e:EventSample {event_id: $event_id})
    ON CREATE SET
      e.workflow_id = $workflow_id,
      e.workflow_type = $workflow_type,
      e.observed_at = $observed_at,
      e.pressure = $pressure,
      e.velocity = $velocity,
      e.capacity = $capacity,
      e.option_space = $option_space,
      e.source_ref = $source_ref,
      e.actor_ref = $actor_ref,
      e.metadata_json = $metadata_json
    WITH e
    MATCH (c:CaseObject {case_id: $case_id})
    MERGE (c)-[:GENERATES]->(e)
    RETURN e
    """

    # ── Ledger Entries ────────────────────────────────────────────────────────

    APPEND_LEDGER_ENTRY = """
    MATCH (e:EventSample {event_id: $event_id})
    CREATE (l:LedgerEntry {
      entry_id:         $entry_id,
      timestamp:        $timestamp,
      s_score:          $s_score,
      ds_de:            $ds_de,
      d2s_de2:          $d2s_de2,
      horizon_events:   $horizon_events,
      operating_mode:   $operating_mode,
      membrane_decision: $membrane_decision,
      rationale:        $rationale,
      previous_hash:    $previous_hash,
      entry_hash:       $entry_hash,
      ces_state:        $ces_state
    })
    MERGE (e)-[:PRODUCES]->(l)
    WITH l
    OPTIONAL MATCH (prev:LedgerEntry {entry_id: $previous_entry_id})
    FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
      MERGE (l)-[:CHAINS_TO]->(prev)
    )
    RETURN l
    """

    GET_LEDGER_CHAIN = """
    MATCH path = (head:LedgerEntry)-[:CHAINS_TO*]->(genesis:LedgerEntry)
    WHERE NOT (genesis)-[:CHAINS_TO]->()
    AND head.entry_id = $from_entry_id
    RETURN [node IN nodes(path) | {
      entry_id: node.entry_id,
      timestamp: node.timestamp,
      s_score: node.s_score,
      ds_de: node.ds_de,
      operating_mode: node.operating_mode,
      membrane_decision: node.membrane_decision,
      previous_hash: node.previous_hash,
      entry_hash: node.entry_hash
    }] AS chain
    """

    VERIFY_CHAIN_INTEGRITY = """
    MATCH (l:LedgerEntry)
    WHERE l.tenant_id = $tenant_id
    WITH l ORDER BY l.timestamp ASC
    WITH collect(l) AS entries
    RETURN [i IN range(0, size(entries)-2) |
      entries[i+1].previous_hash = entries[i].entry_hash
    ] AS integrity_checks
    """

    GET_TRAJECTORY_WINDOW = """
    MATCH (e:EventSample)-[:PRODUCES]->(l:LedgerEntry)
    WHERE e.workflow_id = $workflow_id
    RETURN l.s_score, l.ds_de, l.d2s_de2, l.timestamp, l.operating_mode
    ORDER BY l.timestamp DESC
    LIMIT $window_size
    """

    GET_LATEST_STATE = """
    MATCH (l:LedgerEntry)
    WHERE l.entry_id <> 'GENESIS'
    RETURN l ORDER BY l.timestamp DESC LIMIT 1
    """

    # ── CP-Net Graph ──────────────────────────────────────────────────────────

    UPSERT_CES_NODE = """
    MERGE (n:CESNode {node_id: $node_id})
    ON CREATE SET
      n.name = $name,
      n.is_constant = $is_constant,
      n.state = $state,
      n.domain_type = $domain_type
    ON MATCH SET
      n.state = $state
    RETURN n
    """

    CREATE_CES_DEPENDENCY = """
    MATCH (parent:CESNode {node_id: $parent_id})
    MATCH (child:CESNode {node_id: $child_id})
    MERGE (parent)-[r:DEPENDS_ON {
      preferred_parent_state: $preferred_parent_state,
      preferred_child_state:  $preferred_child_state,
      weight:                 $weight
    }]->(child)
    RETURN r
    """

    GET_CP_NET_SUBGRAPH = """
    MATCH (n:CESNode)-[r:DEPENDS_ON]->(m:CESNode)
    RETURN n.node_id AS from_id, n.name AS from_name, n.state AS from_state,
           m.node_id AS to_id, m.name AS to_name, m.state AS to_state,
           r.preferred_parent_state, r.preferred_child_state, r.weight
    """

    DETECT_SCHIZOPHRENIA = """
    // Detect conflicting preference signals in the CP-Net
    MATCH (a:CESNode)-[r1:DEPENDS_ON]->(b:CESNode),
          (c:CESNode)-[r2:DEPENDS_ON]->(b)
    WHERE a <> c
      AND r1.preferred_child_state <> r2.preferred_child_state
    RETURN b.name AS conflicted_node,
           a.name AS conflicting_parent_1,
           c.name AS conflicting_parent_2,
           r1.preferred_child_state AS pref_1,
           r2.preferred_child_state AS pref_2
    """

    # ── Operator & Transitions ────────────────────────────────────────────────

    UPSERT_OPERATOR = """
    MERGE (o:Operator {operator_id: $operator_id})
    ON CREATE SET
      o.name = $name,
      o.role = $role,
      o.institution_id = $institution_id,
      o.public_key_fingerprint = $public_key_fingerprint,
      o.created_at = $created_at
    RETURN o
    """

    CREATE_TRANSITION = """
    CREATE (t:ControlTransition {
      transition_id:    $transition_id,
      timestamp:        $timestamp,
      from_mode:        $from_mode,
      to_mode:          $to_mode,
      rationale:        $rationale,
      oath_hash:        $oath_hash,
      operator_id:      $operator_id,
      nonce:            $nonce,
      signature:        $signature,
      policy_version:   $policy_version
    })
    WITH t
    MATCH (o:Operator {operator_id: $operator_id})
    MERGE (o)-[:AUTHORIZED_TRANSITION]->(t)
    RETURN t
    """

    GET_TRANSITION_HISTORY = """
    MATCH (o:Operator)-[:AUTHORIZED_TRANSITION]->(t:ControlTransition)
    WHERE t.timestamp >= $from_timestamp
    RETURN t ORDER BY t.timestamp DESC
    """

    # ── Analytics (GDS-ready) ─────────────────────────────────────────────────

    FIND_HIGH_PRESSURE_CLUSTERS = """
    // Find domains with average pressure > threshold
    MATCH (d:Domain)-[:CONTAINS]->(c:CaseObject)-[:GENERATES]->(e:EventSample)
    WHERE e.observed_at >= $since
    WITH d, avg(e.pressure) AS avg_pressure, avg(e.velocity) AS avg_velocity,
         count(e) AS event_count
    WHERE avg_pressure > $pressure_threshold
    RETURN d.name AS domain, d.domain_id,
           round(avg_pressure, 4) AS avg_pressure,
           round(avg_velocity, 4) AS avg_velocity,
           event_count
    ORDER BY avg_pressure DESC
    """

    STRUCTURAL_FRAGILITY_SCORE = """
    // Computes a fragility score per domain based on cascade potential
    MATCH (d:Domain)-[:CONTAINS]->(c:CaseObject)-[:GENERATES]->(e:EventSample)-[:PRODUCES]->(l:LedgerEntry)
    WHERE l.s_score < $tau_floor
    WITH d, count(l) AS tau_violations, avg(l.ds_de) AS mean_drift
    MATCH (d2:Domain)
    WHERE d2.domain_id <> d.domain_id
    OPTIONAL MATCH (d)-[:INTERDEPENDENT_WITH]-(d2)
    RETURN d.name AS domain, tau_violations, mean_drift,
           count(d2) AS interdependent_domains,
           tau_violations * 1.0 / (1 + count(d2)) AS fragility_score
    ORDER BY fragility_score DESC
    """


# ─── ASYNC DRIVER WRAPPER ─────────────────────────────────────────────────────

class Neo4jGraphDB:
    """
    Async Neo4j driver wrapper for TENIR R5.
    Uses the official neo4j Python driver (v5.x).
    Install: pip install neo4j
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        auth: tuple = ("neo4j", "tenir_password"),
        database: str = "tenir",
    ):
        self.uri = uri
        self.auth = auth
        self.database = database
        self._driver = None

    async def connect(self) -> None:
        try:
            from neo4j import AsyncGraphDatabase  # type: ignore
            self._driver = AsyncGraphDatabase.driver(
                self.uri, auth=self.auth
            )
            await self._driver.verify_connectivity()
            logger.info(f"[R5-Graph] Connected to Neo4j @ {self.uri}")
            await self._init_schema()
        except ImportError:
            logger.error("[R5-Graph] neo4j package not installed. Run: pip install neo4j")
            raise
        except Exception as e:
            logger.error(f"[R5-Graph] Connection failed: {e}")
            raise

    async def _init_schema(self) -> None:
        """Creates constraints and indexes idempotently."""
        async with self._driver.session(database=self.database) as session:
            for cypher in SCHEMA_CYPHER:
                try:
                    await session.run(cypher)
                except Exception as e:
                    logger.warning(f"[R5-Graph] Schema stmt warning: {e}")
        logger.info("[R5-Graph] Schema initialized")

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()

    async def run(self, query: str, **params) -> List[Dict]:
        """Execute a read/write query, return list of record dicts."""
        async with self._driver.session(database=self.database) as session:
            result = await session.run(query, **params)
            return [dict(record) async for record in result]

    # ── High-level domain operations ──────────────────────────────────────────

    async def append_ledger_entry(
        self,
        event_id: str,
        s_score: float,
        ds_de: float,
        d2s_de2: float,
        horizon_events: Optional[int],
        operating_mode: str,
        membrane_decision: str,
        rationale: str,
        previous_entry_id: Optional[str],
        previous_hash: str,
        ces_state: str,
    ) -> str:
        """
        Appends a new ledger entry and chains it to the previous.
        Returns the new entry_id.
        """
        entry_id = str(uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()

        # SHA-256 hash of the entry payload
        payload = json.dumps({
            "event_id": event_id, "s_score": s_score, "ds_de": ds_de,
            "operating_mode": operating_mode, "membrane_decision": membrane_decision,
            "previous_hash": previous_hash, "timestamp": timestamp,
        }, sort_keys=True)
        entry_hash = hashlib.sha256(payload.encode()).hexdigest()

        await self.run(
            CypherQueries.APPEND_LEDGER_ENTRY,
            entry_id=entry_id,
            timestamp=timestamp,
            s_score=s_score,
            ds_de=ds_de,
            d2s_de2=d2s_de2,
            horizon_events=horizon_events,
            operating_mode=operating_mode,
            membrane_decision=membrane_decision,
            rationale=rationale,
            previous_hash=previous_hash,
            entry_hash=entry_hash,
            ces_state=ces_state,
            previous_entry_id=previous_entry_id or "",
        )
        return entry_id

    async def verify_chain_integrity(self, tenant_id: str) -> Dict[str, Any]:
        """
        Traverses the full ledger chain and verifies every hash link.
        Returns {valid: bool, broken_links: int, total_links: int}.
        """
        records = await self.run(
            CypherQueries.VERIFY_CHAIN_INTEGRITY,
            tenant_id=tenant_id,
        )
        if not records:
            return {"valid": True, "broken_links": 0, "total_links": 0}
        checks = records[0].get("integrity_checks", [])
        broken = sum(1 for c in checks if not c)
        return {
            "valid": broken == 0,
            "broken_links": broken,
            "total_links": len(checks),
        }

    async def get_cp_net_state(self) -> Dict[str, Any]:
        """Returns the full CP-Net subgraph for VPS rendering."""
        edges = await self.run(CypherQueries.GET_CP_NET_SUBGRAPH)
        conflicts = await self.run(CypherQueries.DETECT_SCHIZOPHRENIA)
        return {"edges": edges, "conflicts": conflicts}

    async def seed_um6p_ocp_ontology(self) -> None:
        """
        Seeds the database with the partner_a + partner_b institutional ontology.
        Called once during deployment initialization.
        """
        now = datetime.now(timezone.utc).isoformat()
        um6p_id = "inst-partner_a-001"
        ocp_id = "inst-partner_b-001"

        # Institutions
        for inst_id, name, tau in [
            (um6p_id, "Université Mohammed VI Polytechnique", 0.35),
            (ocp_id, "partner_b Group", 0.40),
        ]:
            await self.run(
                CypherQueries.UPSERT_INSTITUTION,
                institution_id=inst_id, name=name,
                created_at=now, tau_floor=tau,
            )

        # Domains
        domains = [
            ("dom-partner_a-rnd", um6p_id, "RND", "partner_a Research & Development"),
            ("dom-partner_a-legal", um6p_id, "LEGAL", "partner_a Legal & Compliance"),
            ("dom-partner_a-budget", um6p_id, "BUDGET", "partner_a Financial Resource"),
            ("dom-partner_b-procurement", ocp_id, "PROCUREMENT", "partner_b Industrial Procurement"),
            ("dom-partner_b-rnd", ocp_id, "RND", "partner_b Applied Research"),
            ("dom-partner_b-legal", ocp_id, "LEGAL", "partner_b Regulatory Affairs"),
        ]
        for domain_id, inst_id, domain_type, name in domains:
            await self.run(
                CypherQueries.UPSERT_DOMAIN,
                domain_id=domain_id, institution_id=inst_id,
                domain_type=domain_type, name=name,
                description=name, created_at=now,
            )

        # CES Nodes (CP-Net vertices)
        ces_nodes = [
            ("ces-tau", "TAU_INVARIANT", True, True, "GOVERN"),
            ("ces-rnd-velocity", "RND_VELOCITY", False, False, "RND"),
            ("ces-procurement-block", "PROCUREMENT_BLOCK", False, False, "PROCUREMENT"),
            ("ces-budget-capacity", "BUDGET_CAPACITY", False, True, "BUDGET"),
            ("ces-legal-gate", "LEGAL_GATE", False, True, "LEGAL"),
        ]
        for node_id, name, is_constant, state, domain_type in ces_nodes:
            await self.run(
                CypherQueries.UPSERT_CES_NODE,
                node_id=node_id, name=name,
                is_constant=is_constant, state=state,
                domain_type=domain_type,
            )

        # CP-Net dependencies (who governs whom)
        deps = [
            ("ces-tau",         "ces-rnd-velocity",      True,  False, 1.0),
            ("ces-tau",         "ces-procurement-block", True,  False, 1.0),
            ("ces-legal-gate",  "ces-rnd-velocity",      False, False, 0.8),
            ("ces-budget-capacity", "ces-procurement-block", False, True, 0.7),
        ]
        for parent, child, pref_p, pref_c, weight in deps:
            await self.run(
                CypherQueries.CREATE_CES_DEPENDENCY,
                parent_id=parent, child_id=child,
                preferred_parent_state=pref_p,
                preferred_child_state=pref_c,
                weight=weight,
            )

        logger.info("[R5-Graph] partner_a/partner_b ontology seeded")


# ─── DEPENDENCY INJECTION ─────────────────────────────────────────────────────
# FastAPI lifespan-managed singleton

_db: Optional[Neo4jGraphDB] = None


async def get_db() -> Neo4jGraphDB:
    if _db is None:
        raise RuntimeError("Neo4j DB not initialized. Call startup() first.")
    return _db


async def startup(
    uri: str = "bolt://localhost:7687",
    auth: tuple = ("neo4j", "tenir_password"),
    database: str = "tenir",
    seed: bool = False,
) -> Neo4jGraphDB:
    global _db
    _db = Neo4jGraphDB(uri=uri, auth=auth, database=database)
    await _db.connect()
    if seed:
        await _db.seed_um6p_ocp_ontology()
    return _db


async def shutdown() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None
