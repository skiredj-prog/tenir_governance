"""Portable source-level guards for replicated command determinism."""
import ast
from pathlib import Path


def test_replicated_commands_have_no_local_time_or_randomness():
    source_path = Path(__file__).parents[1] / "tenir_governance/operations/raft_backend.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    state_machine = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_RaftStateMachine"
    )
    forbidden = {
        "time", "monotonic", "perf_counter", "now", "utcnow", "uuid4",
        "random", "randint", "choice", "urandom", "token_bytes",
    }
    for method in state_machine.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(
            isinstance(decorator, ast.Name) and decorator.id == "replicated"
            for decorator in method.decorator_list
        ):
            continue
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute)
            else node.func.id if isinstance(node.func, ast.Name) else ""
            for node in ast.walk(method) if isinstance(node, ast.Call)
        }
        assert not (calls & forbidden), (
            f"non-deterministic call(s) in replicated {method.name}: {sorted(calls & forbidden)}"
        )


def test_raft_backend_requires_shared_authentication_secret():
    source_path = Path(__file__).parents[1] / "tenir_governance/operations/raft_backend.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    namespace = {"__name__": "raft_backend_auth_static_test"}
    # Extract just the pure validator so this guard runs without pysyncobj.
    validator = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_peer_auth_key"
    )
    code = compile(ast.fix_missing_locations(ast.Module(body=[
        ast.Assign(targets=[ast.Name(id="MIN_PEER_AUTH_KEY_LENGTH", ctx=ast.Store())], value=ast.Constant(32)),
        validator,
    ], type_ignores=[])), str(source_path), "exec")
    exec(code, namespace)
    import pytest
    for value in (None, "short", b"x" * 40):
        with pytest.raises(ValueError):
            namespace["_validate_peer_auth_key"](value)
    key = "x" * 32
    assert namespace["_validate_peer_auth_key"](key) == key
