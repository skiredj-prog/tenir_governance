"""A new budget reader must serialize with an in-flight writer transaction."""
from decimal import Decimal
from multiprocessing import get_context

import pytest

from tenir_governance.objects import Resource
from tenir_governance.operations.budget import DurableBudgetCoordinator
from tenir_governance.operations.file_lock import acquire_file_lock


def _construct_reader(store, io_entered, load_entered, completed, queue):
    from tenir_governance.operations import budget

    original_lock = budget.acquire_file_lock
    original_load = budget.DurableBudgetCoordinator._load

    def traced_lock(path):
        io_entered.set()
        return original_lock(path)

    def traced_load(self):
        load_entered.set()
        io_entered.set()
        return original_load(self)

    budget.acquire_file_lock = traced_lock
    budget.DurableBudgetCoordinator._load = traced_load
    try:
        coordinator = budget.DurableBudgetCoordinator(store)
        queue.put({"resources": coordinator._resources})
    except BaseException as exc:
        queue.put({"error": repr(exc)})
    finally:
        completed.set()


@pytest.mark.parametrize("existing_store", [False, True])
def test_constructor_waits_for_writer_and_reads_completed_state(tmp_path, existing_store):
    """The initial read cannot see an old or absent store during replacement."""
    store = str(tmp_path / "budget.json")
    writer = DurableBudgetCoordinator()
    writer.register(Resource(resource_id="budget", capacity=100.0))
    writer._store_path = store
    if existing_store:
        writer._persist()

    context = get_context("spawn")
    io_entered = context.Event()
    load_entered = context.Event()
    completed = context.Event()
    queue = context.Queue()
    reader = context.Process(
        target=_construct_reader,
        args=(store, io_entered, load_entered, completed, queue),
    )
    lock = acquire_file_lock(store + ".lock")
    try:
        reader.start()
        assert io_entered.wait(10), "constructor never reached its store access"
        assert not load_entered.is_set(), "constructor read while the writer owned the lock"
        assert not completed.is_set(), "constructor returned before the writer committed"
        writer._resources["budget"]["reserved"] = Decimal("20")
        writer._persist()
    finally:
        lock.close()
        if reader.pid is not None:
            reader.join(10)
            if reader.is_alive():
                reader.terminate()
                reader.join(5)

    assert reader.exitcode == 0
    assert load_entered.is_set()
    result = queue.get(timeout=5)
    assert "error" not in result, result
    assert result["resources"]["budget"] == {
        "capacity": Decimal("100.0"),
        "reserved": Decimal("20"),
        "consumed": Decimal("0.0"),
    }
    queue.close()
    queue.join_thread()


def test_corrupt_initial_store_fails_closed_and_releases_constructor_lock(tmp_path):
    """A failed initial read must neither reset accounting nor retain the gate."""
    store = tmp_path / "budget.json"
    store.write_text('{"unfinished":', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Corrupted durable budget store"):
        DurableBudgetCoordinator(str(store))
    assert store.read_text(encoding="utf-8") == '{"unfinished":'

    context = get_context("spawn")
    io_entered, load_entered, completed = (context.Event() for _ in range(3))
    queue = context.Queue()
    reader = context.Process(
        target=_construct_reader,
        args=(str(store), io_entered, load_entered, completed, queue),
    )
    reader.start()
    try:
        assert completed.wait(10), "failed constructor retained the shared file lock"
        result = queue.get(timeout=5)
        assert "Corrupted durable budget store" in result.get("error", ""), result
    finally:
        reader.join(5)
        if reader.is_alive():
            reader.terminate()
            reader.join(5)
        queue.close()
        queue.join_thread()
    assert reader.exitcode == 0
