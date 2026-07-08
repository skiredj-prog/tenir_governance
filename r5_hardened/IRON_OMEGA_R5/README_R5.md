# IRON OMEGA R5 — Implementation Package
## TENIR 2C: Neuro-Symbolic Governance Platform

**Status:** R5 Implementation Complete  
**Date:** April 2026  
**Author:** A. Skiredj — TENIR Architecte-Stratège  

---

## What R5 Implements

| Component | V42CIron (MVP) | R5 (This Package) |
|:---|:---|:---|
| **NSL Parsing** | Regex keyword matching | Formal LALR(1) grammar + LLM fine-tuning pipeline |
| **State Storage** | In-memory Python lists | Neo4j graph database with full ontology |
| **VPS Engine** | Standalone browser simulation | Live WebSocket-driven Three.js 3D topography |
| **Cryptography** | UUID mock hashes | Merkle tree epochs + hash chain + KeyCeremony |

---

## File Map

```
R5_IRON_OMEGA/
├── r5_neuro_symbolic/
│   ├── grammar/
│   │   └── nsl_grammar.py          # LALR(1) NSL parser, AST, compiler
│   ├── training/
│   │   └── dataset_generator.py    # QLoRA training data + Axolotl config
│   └── inference/
│       └── nsl_inference.py        # Ollama wrapper + grammar fallback
│
├── r5_graph_ontology/
│   └── neo4j_graph.py              # Schema, Cypher library, async driver, seed
│
├── r5_websocket/
│   ├── hub/
│   │   └── ws_hub.py               # WebSocket hub, frame protocol, VPS coord mapping
│   └── vps_live/
│       └── vps_r5_live.html        # Three.js live VPS engine
│
├── r5_distributed_crypto/
│   └── merkle/
│       └── distributed_ledger.py   # Merkle tree, hash chain, peer consensus, KeyCeremony
│
├── r5_server.py                    # Unified FastAPI server (replaces api/server.py)
├── tests/
│   └── test_r5_all.py              # Full test suite (56 tests)
└── requirements_r5.txt
```

---

## Deployment

### 1. Prerequisites

```bash
# Neo4j (local or Docker)
docker run -d --name neo4j-tenir \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/tenir_password \
  neo4j:5.17

# Ollama (for LLM inference, optional)
curl -fsSL https://ollama.com/install.sh | sh
# Load the fine-tuned NSL model after training:
# ollama create tenir-nsl -f Modelfile
```

### 2. Install

```bash
pip install -r requirements_r5.txt
```

### 3. Environment

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=tenir_password
export NEO4J_DATABASE=tenir
export NEO4J_SEED=true          # Seed partner_a/partner_b ontology on first run
export OLLAMA_MODEL=grammar     # Use "grammar" until fine-tune is done
export LEDGER_PATH=ledger/tenir_ledger.jsonl
export OATH_SECRET=<change_in_production>
export PEER_NODES=              # Leave empty for single-node mode
```

### 4. Run

```bash
uvicorn r5_server:app --host 0.0.0.0 --port 8000
```

### 5. VPS Engine

Open `r5_websocket/vps_live/vps_r5_live.html` in a browser.  
The Three.js engine will automatically connect to `ws://localhost:8000/ws/vps`.

---

## API Reference

### `POST /api/v1/adjudicate`

Accepts **natural language or JSON**:

```bash
# Natural language (routed through NSL engine)
curl -X POST http://localhost:8000/api/v1/adjudicate \
  -H "Content-Type: application/json" \
  -d '{"raw_input": "Accelerate the partner_b-H2-2026 R&D project urgently due to budget risk"}'

# Strict JSON (legacy V42CIron format)
curl -X POST http://localhost:8000/api/v1/adjudicate \
  -H "Content-Type: application/json" \
  -d '{"raw_input": "{\"workflow_id\":\"WF-001\",\"workflow_type\":\"rnd\",\"pressure\":0.8,\"velocity\":0.7,\"capacity\":0.6,\"option_space\":0.4,\"source_ref\":\"erp\",\"actor_ref\":\"op-01\"}"}'
```

### `POST /api/v1/oath/sign`

```bash
curl -X POST "http://localhost:8000/api/v1/oath/sign?operator_id=op-aziz"
```

### `POST /api/v1/transition`

```bash
# Use the signature from /api/v1/oath/sign
curl -X POST http://localhost:8000/api/v1/transition \
  -H "Content-Type: application/json" \
  -d '{
    "operator_id": "op-aziz",
    "target_mode": "ENFORCE",
    "oath_text": "J'\''assume la friction. Je préserve le TAU.",
    "oath_signature": "<from sign endpoint>",
    "nonce": "<from sign endpoint>",
    "timestamp": "<from sign endpoint>"
  }'
```

### `GET /api/v1/ledger/verify`

Returns chain integrity report.

### `GET /api/v1/ledger/proof/{entry_id}`

Returns Merkle inclusion proof for an entry in a sealed epoch.

### `WS ws://localhost:8000/ws/vps`

Live WebSocket stream consumed by the VPS Three.js engine.

---

## NSL Fine-Tuning Guide

### Generate Training Data

```bash
python -m r5_neuro_symbolic.training.dataset_generator
# Creates: r5_neuro_symbolic/training/data/nsl_train.jsonl
# Creates: r5_neuro_symbolic/training/data/axolotl_nsl_config.yaml
```

### Fine-Tune (GPU required, 24GB+ VRAM for 8B model)

```bash
pip install axolotl
accelerate launch -m axolotl.cli.train \
  r5_neuro_symbolic/training/data/axolotl_nsl_config.yaml
```

### Load into Ollama

```bash
# Create Modelfile
echo "FROM ./nsl_lora_model\nSYSTEM \"$(cat r5_neuro_symbolic/training/data/system_prompt.txt)\"" > Modelfile
ollama create tenir-nsl -f Modelfile

# Update env
export OLLAMA_MODEL=tenir-nsl:latest
```

### Without Fine-Tuning (Grammar-Only Mode)

Set `OLLAMA_MODEL=grammar`. The LALR(1) grammar parser handles all inputs
deterministically. No GPU, no Ollama, no network required.

---

## Distributed Multi-Node Deployment

```bash
# Node 1
export PEER_NODES=http://node2:8000,http://node3:8000
uvicorn r5_server:app --host 0.0.0.0 --port 8000

# Node 2
export PEER_NODES=http://node1:8000,http://node3:8000
uvicorn r5_server:app --host 0.0.0.0 --port 8000

# Node 3
export PEER_NODES=http://node1:8000,http://node2:8000
uvicorn r5_server:app --host 0.0.0.0 --port 8000
```

When an epoch (100 entries) is sealed, all nodes automatically broadcast
the Merkle root to peers. Disagreement triggers a `LedgerIntegrityError`.

---

## Running Tests

```bash
python -m pytest tests/test_r5_all.py -v
```

Expected: **56 tests passing**

---

## Architecture Decisions

### Why LALR(1) Grammar + LLM Dual Backend?

The grammar provides deterministic, auditable, zero-hallucination parsing.
The LLM (when fine-tuned) provides broader coverage of edge cases.
The system **always falls back to grammar** if the LLM output fails JSON validation.
This means governance decisions are **never blocked by LLM failure**.

### Why Merkle Epochs (not rolling)?

Epoch-based Merkle trees allow:
- O(1) append performance within an epoch
- O(log n) inclusion proofs after epoch sealing
- Efficient peer broadcast (one root per epoch, not per entry)
- Independent epoch verification without downloading full chain

### Why Neo4j for the Ontology?

The CP-Net structure (who governs whom) is a directed acyclic graph.
SQL is inefficient for multi-hop graph queries.
Neo4j GDS algorithms (centrality, fragility scoring) run natively on the same store.

### TAU Floor and SHADOW→ENFORCE

The KeyCeremony epoch-binds oath signatures.
After every 100 events (one epoch), the operator must re-sign.
This implements the Jubilee Cryptographic Protocol from the Genesis Artifact:
*"forces each generation to physically and morally re-sign the founding pact."*

---

*TENIR 2C — L'architecture est prête.*  
*"Le Kernel gouverne les agents ; l'Humain gouverne le Kernel."*
