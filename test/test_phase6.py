import os
import random
import multiprocessing
import pytest
from engine import Engine, ConflictError
from fault import faults
from checker import (
    Checker, Begin, Read, Write, Delete, Commit, Abort, Crash, Recovered,
)

FAULT_POINTS = [
    'before_wal_append',
    'during_wal_append',
    'after_wal_sync_before_ack',
    'after_ack_before_heap',
    'during_heap_page_write',
]


# ─── Child process helpers ────────────────────────

def _child_commit_with_fault(data_dir, fault_name):
    """
    Child process: opens engine, sets fault, tries to commit.
    The fault triggers os._exit(1) at the named point.
    """
    faults.set_fault(fault_name)
    db = Engine(data_dir)

    txn = db.begin()
    db.put(txn, b"child_key", b"child_value")

    try:
        db.commit(txn)
    except:
        pass

    try:
        db.close()
    except:
        pass
    os._exit(0)


def _child_checkpoint_with_fault(data_dir, fault_name):
    """
    Child process: commits data, then crashes during checkpoint.
    Used for the during_heap_page_write fault (page writes happen
    at checkpoint, not at commit time in no-steal).
    """
    db = Engine(data_dir)

    # Commit real data first (WAL is durable)
    txn = db.begin()
    db.put(txn, b"checkpoint_data", b"should_survive")
    db.commit(txn)

    # Now set the fault and trigger checkpoint
    faults.set_fault(fault_name)
    try:
        db.checkpoint()
    except:
        pass

    os._exit(1)


# ─── Test 1: Each fault point individually ────────

@pytest.mark.parametrize("fault_name", FAULT_POINTS)
def test_recovery_after_each_fault(tmp_path, fault_name):
    """
    For each of the 5 fault points:
    1. Parent commits known data
    2. Child crashes at that fault point
    3. Recovery must NOT crash (no panic, no infinite loop)
    4. Parent's committed data must survive
    """
    data_dir = str(tmp_path / "kiln-data")

    # Step 1: Parent commits baseline
    db = Engine(data_dir)
    txn = db.begin()
    db.put(txn, b"safe_key", b"safe_value")
    db.commit(txn)
    db.close()

    # Step 2: Child crashes at fault point
    if fault_name == 'during_heap_page_write':
        target = _child_checkpoint_with_fault
    else:
        target = _child_commit_with_fault

    p = multiprocessing.Process(target=target, args=(data_dir, fault_name))
    p.start()
    p.join()

    # Step 3: Recovery (Engine.__init__ calls _recover)
    db = Engine(data_dir)

    # Step 4: Parent's committed data must survive
    txn = db.begin()
    result = db.get(txn, b"safe_key")
    assert result == b"safe_value", (
        f"Fault '{fault_name}': parent data lost! "
        f"Expected b'safe_value', got {result}"
    )

    db.close()


# ─── Test 2: Multiple crashes in a row ────────────

def test_repeated_crashes_same_dir(tmp_path):
    """
    Crash multiple times on the same data dir.
    Recovery must be idempotent — each restart rebuilds correctly.
    """
    data_dir = str(tmp_path / "kiln-data")

    # Commit baseline
    db = Engine(data_dir)
    txn = db.begin()
    db.put(txn, b"base", b"value")
    db.commit(txn)
    db.close()

    # Crash 5 times with different faults
    for fault_name in FAULT_POINTS:
        if fault_name == 'during_heap_page_write':
            target = _child_checkpoint_with_fault
        else:
            target = _child_commit_with_fault

        p = multiprocessing.Process(target=target, args=(data_dir, fault_name))
        p.start()
        p.join()

    # After all crashes, recovery must still work
    db = Engine(data_dir)
    txn = db.begin()
    assert db.get(txn, b"base") == b"value"
    db.close()


# ─── Test 3: Random chaos loop ────────────────────

def _random_key():
    return f"k{random.randint(0, 7)}".encode()

def _random_value():
    return f"v{random.randint(0, 999)}".encode()


def _child_random_workload(data_dir, fault_name, seed):
    """
    Child: opens engine, runs random txns, crashes at fault
    during the last commit attempt.
    """
    random.seed(seed)
    db = Engine(data_dir)

    # Run a few successful txns first
    for _ in range(random.randint(1, 5)):
        txn = db.begin()
        for _ in range(random.randint(1, 3)):
            if random.random() < 0.8:
                db.put(txn, _random_key(), _random_value())
            else:
                db.delete(txn, _random_key())
        try:
            db.commit(txn)
        except ConflictError:
            db.abort(txn)

    # Last txn: set fault and crash
    faults.set_fault(fault_name)
    txn = db.begin()
    db.put(txn, _random_key(), _random_value())

    try:
        db.commit(txn)
    except:
        pass

    os._exit(1)


def test_random_chaos_loop(tmp_path):
    """
    N random runs with random faults.
    Each run: commit baseline → child does random work + crash → recover → verify.
    """
    NUM_RUNS = 20

    for run in range(NUM_RUNS):
        data_dir = str(tmp_path / f"run-{run}")
        seed = run * 42
        random.seed(seed)

        # Parent commits baseline
        db = Engine(data_dir)
        baseline = {}
        for i in range(3):
            txn = db.begin()
            key = f"base{i}".encode()
            value = f"val{i}".encode()
            db.put(txn, key, value)
            db.commit(txn)
            baseline[key] = value
        db.close()

        # Child does random work then crashes
        fault = random.choice(FAULT_POINTS)

        if fault == 'during_heap_page_write':
            target = _child_checkpoint_with_fault
        else:
            target = _child_random_workload

        args = (data_dir, fault, seed) if target == _child_random_workload else (data_dir, fault)
        p = multiprocessing.Process(target=target, args=args)
        p.start()
        p.join()

        # Recovery — must not crash
        db = Engine(data_dir)
        txn = db.begin()

        # All baseline data must survive
        for key, expected in baseline.items():
            result = db.get(txn, key)
            assert result == expected, (
                f"Run {run}, fault '{fault}': "
                f"key {key!r} expected {expected!r}, got {result!r}"
            )

        db.close()


# ─── Test 4: Chaos + Checker ──────────────────────

def test_chaos_with_checker(tmp_path):
    """
    Full story: commit data, crash, recover, run the checker.
    Validates all 5 invariants on a real crash history.
    """
    data_dir = str(tmp_path / "kiln-data")
    history = []

    # Phase 1: Commit known data
    db = Engine(data_dir)
    committed_keys = {}

    for i in range(5):
        txn = db.begin()
        history.append(Begin(txn.txn_id, txn.start_ts))
        key = f"k{i}".encode()
        value = f"v{i}".encode()
        db.put(txn, key, value)
        history.append(Write(txn.txn_id, key, value))
        db.commit(txn)
        history.append(Commit(txn.txn_id, txn.commit_ts))
        committed_keys[key] = value

    db.close()

    # Phase 2: Child crashes at a random fault
    fault = random.choice(FAULT_POINTS)

    if fault == 'during_heap_page_write':
        target = _child_checkpoint_with_fault
    else:
        target = _child_commit_with_fault

    p = multiprocessing.Process(target=target, args=(data_dir, fault))
    p.start()
    p.join()

    history.append(Crash())

    # Phase 3: Recovery + collect Recovered events
    db = Engine(data_dir)
    txn = db.begin()

    for key in committed_keys:
        result = db.get(txn, key)
        history.append(Recovered(key, result))

    db.close()

    # Phase 4: Run the checker
    violations = Checker(history).check()
    assert violations == [], f"Checker found violations: {violations}"
