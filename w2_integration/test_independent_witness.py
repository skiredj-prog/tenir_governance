import os
import tempfile

from independent_witness import IndependentWitness


def test_witness_is_monotone_and_signed():
    with tempfile.TemporaryDirectory() as d:
        witness = IndependentWitness(os.path.join(d, "witness.sqlite3"))
        first = witness.issue("kernel-a", "a" * 64, expected_revision=1)
        second = witness.issue("kernel-a", "b" * 64, expected_revision=2)
        assert witness.verify(first) and witness.verify(second)
        assert second["record"]["previous_fingerprint"] == first["record"]["snapshot_fingerprint"]


def test_witness_rejects_revision_conflict_and_tampering():
    with tempfile.TemporaryDirectory() as d:
        witness = IndependentWitness(os.path.join(d, "witness.sqlite3"))
        witness.issue("kernel-a", "a" * 64)
        try:
            witness.issue("kernel-a", "b" * 64, expected_revision=7)
            raise AssertionError("stale witness revision must be rejected")
        except RuntimeError as exc:
            assert "revision" in str(exc)
        bad = witness.issue("kernel-a", "b" * 64)
        bad["record"]["snapshot_fingerprint"] = "c" * 64
        assert witness.verify(bad) is False


if __name__ == "__main__":
    test_witness_is_monotone_and_signed()
    test_witness_rejects_revision_conflict_and_tampering()
    print("independent witness: PASS")
