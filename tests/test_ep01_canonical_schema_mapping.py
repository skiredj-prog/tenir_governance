"""EP-01 static canonical-schema anchoring tests; not documentary conformance."""
import json
from pathlib import Path


SCHEMA = Path(__file__).resolve().parents[1] / "tenir_kernel/schemas/tenir_1_2.schema.json"


def canonical():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_canonical_epistemic_status_is_explicit():
    ep = canonical()["properties"]["epistemic"]
    assert "status" in ep["required"]
    assert set(ep["properties"]["status"]["enum"]) == {
        "ok", "contested", "invalid"}
    assert "evidence_quality" in ep["required"]
    assert "confidence_ceiling" in ep["required"]


def test_canonical_geometry_references_authority_and_context_sources():
    coords = canonical()["properties"]["constraint_geometry"]["properties"]["coordinates"]
    assert {"authority_source", "context_source"}.issubset(
        set(coords["required"]))
    assert coords["properties"]["authority_source"]["minLength"] >= 1
    assert coords["properties"]["context_source"]["minLength"] >= 1


def test_no_document_precedence_contract_is_claimed_by_schema():
    """Canonical epistemic presence is not issuer/supersession authorization."""
    schema = canonical()
    ep = schema["properties"]["epistemic"]["properties"]
    assert "issuer_scope" not in ep
    assert "supersedes" not in ep
    assert "effective_at" not in ep
