"""
crash_worker.py — Subprocess helper for crash simulation.

Called by app.py to simulate crashes at specific fault points.
Runs in a separate process so os._exit() doesn't kill the Streamlit app.

Usage:
    python3 crash_worker.py <data_dir> <fault_name> commit
    python3 crash_worker.py <data_dir> <fault_name> checkpoint
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Engine
from fault import faults


def do_commit_with_fault(data_dir, fault_name):
    """Open engine, set fault, attempt commit, crash."""
    faults.set_fault(fault_name)
    db = Engine(data_dir)
    txn = db.begin()
    db.put(txn, b"crash_key", b"crash_value")
    try:
        db.commit(txn)
    except:
        pass
    try:
        db.close()
    except:
        pass
    os._exit(0)


def do_checkpoint_with_fault(data_dir, fault_name):
    """Open engine, commit data, then crash during checkpoint."""
    db = Engine(data_dir)
    txn = db.begin()
    db.put(txn, b"checkpoint_data", b"should_survive")
    db.commit(txn)
    faults.set_fault(fault_name)
    try:
        db.checkpoint()
    except:
        pass
    os._exit(1)


if __name__ == "__main__":
    data_dir = sys.argv[1]
    fault_name = sys.argv[2]
    action = sys.argv[3]

    if action == "commit":
        do_commit_with_fault(data_dir, fault_name)
    elif action == "checkpoint":
        do_checkpoint_with_fault(data_dir, fault_name)
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(2)
