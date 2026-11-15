import pytest

from tenir_governance.quickstart import run_demo, verify_demo


@pytest.mark.parametrize(("receipt_available", "final_state"), [(True, "confirmed"), (False, "unknown")])
def test_quickstart_reconciles_only_with_a_receipt(tmp_path, receipt_available, final_state):
    output = tmp_path / "walkthrough"
    run_demo(output, receipt_available=receipt_available)

    verified = verify_demo(output)
    assert verified["final_state"] == final_state
    assert len(verified["evidence"]["provider"]["effects"]) == 1

    with (output / "evidence.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="File digest mismatch"):
        verify_demo(output)
