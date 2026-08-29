import pytest
from checker import (
    Checker, Violation,
    Begin, Read, Write, Delete, Commit, Crash, Recovered,Abort
)

#PASS CASE
def test_valid_serial_history_passes():
    """A simple correct serial history -> PASS"""
    history = [
        Begin(1, start_ts=1),
        Write(1, b"x", b"100"),
        Commit(1, commit_ts=2),

        Begin(2, start_ts=3),
        Read(2, b"x", b"100"),
        Write(2, b"x", b"200"),
        Commit(2, commit_ts=4),
    ]

    violations = Checker(history).check()
    assert violations == [], f"Expected PASS,got: {violations}"


def test_valid_concurrent_history_passes():
    """Two concurrent txns on different keys -> PASS"""
    history = [
        Begin(1, start_ts=1),
        Begin(2, start_ts=1),
        Write(1, b"x", b"1"),
        Write(2, b"y", b"2"),
        Commit(1, commit_ts=2),
        Commit(2, commit_ts=3),
    ]

    violations = Checker(history).check()
    assert violations == [], f"Expected PASS, got: {violations}"

def test_valid_crash_recovery_recovery_passes():
    """Committed data survives crash -> PASS"""

    history = [
        Begin(1, start_ts=1),
        Write(1, b"x", b"100"),
        Commit(1, commit_ts=2),
        Crash(),
        Recovered(b"x", b'100'),
    ]

    violations = Checker(history).check()
    assert violations == [], f"Expected PASS, got: {violations}"

def test_write_skew_is_not_flagged():
    """
    Write skew is ALLOWED under snapshot isolation.
    The checker must NOT Flag it. We do not claim serializable.
    """

    history = [
        Begin(0, start_ts=1),
        Write(0, b"A", b"on"),
        Write(0, b"B", b"on"),
        Commit(0, commit_ts=2),
        

        Begin(1, start_ts=3),
        Read(1, b"A", b"on"),
        Read(1, b"B", b"on"),
        Write(1, b"A", b"off"),

        Begin(2, start_ts=3),
        Read(2, b"A", b"on"),
        Read(2, b"B", b"on"),
        Write(2, b"B", b"off"),

        Commit(1, commit_ts=4),
        Commit(2, commit_ts=5),
    ]

    violations = Checker(history).check()
    assert violations == [], f"Write skew should not be flagged: {violations}"



#FAIL cases 
def test_lost_update_both_commit_fails():
    """
    The KEY TEST: Two txns both see x=100, both write x,
    and BOTH commit. This is a lost update -> checker MUST catch it.
    """

    history= [
        Begin(0, start_ts=1),
        Write(0, b"x", b"100"),
        Commit(0, commit_ts=2),

        #Two txns start at the same snapshot
        Begin(1, start_ts=3),
        Begin(2, start_ts=3),

        Read(1,b"x", b"100"),
        Read(2,b"x", b"100"),

        Write(1, b"x", b"200"),
        Write(2, b"x", b"300"),

        #Both Commit on the same key - This is the BUG
        Commit(1, commit_ts=4),
        Commit(2, commit_ts=5),
    ]


    violations = Checker(history).check()
    lost = [v for v in violations if v.invariant == "lost_update"]
    assert len(lost) > 0, "Checker must catch lost update when both commit"


def test_durability_violation_missing_data():
    """Committed data missing after crash -> durability FAIL"""

    history = [
        Begin(1, start_ts=1),
        Write(1, b"x", b"100"),
        Commit(1, commit_ts=2),
        Crash(),
        Recovered(b"x", None), #BUG: committed data is gone

    ]

    violations = Checker(history).check()
    dur = [v for v in violations if v.invariant == "durability"]
    assert len(dur) > 0, "Missing committed data = durability violation"



def test_durability_violation_wrong_value():
    """Committed data has wrong value after crash -> durability FAIL"""
    
    history = [
        Begin(1, start_ts=1),
        Write(1, b"x", b"100"),
        Commit(1, commit_ts=2),
        Crash(),
        Recovered(b"x", b"GARBAGE"), #BUG-> WRONG VALUE

    ]

    violations = Checker(history).check()
    dur = [v for v in violations if v.invariant == "durability"]
    assert len(dur) > 0

def test_atomicity_violation_uncommitted_survives():
    """
    Uncommitted txn's writes visible after crash -> atomicity FAIL
    """

    history = [
        Begin(1, start_ts=1),
        Write(1, b"x", b"secret"),
        #NO COMMIT - txn1 was in-flight when crash happened 
        Crash(),
        Recovered(b"x", b"secret"),  #BUG uncommitted data survived
    ]

    violations = Checker(history).check()
    atom = [v for v in violations if v.invariant == "atomicity"]
    assert len(atom) > 0, "Uncommitted data after crash = atomicity violation"


def test_snapshot_read_violations():
    """Txn reads a value from a commit that happened after its start_ts -> FAIL"""
    history = [
        Begin(1, start_ts=1),
        Write(1, b"x", b"old"),
        Commit(1, commit_ts=2),

        #Txn2 starts - its snapshot should see x=old
        Begin(2, start_ts=3),

        #Txn3 commits x=new AFTER txn 2 started
        Begin(3, start_ts=3),
        Write(3, b"x", b"new"),
        Commit(3, commit_ts=4),

        #txn 2 reads x=new - BUG it should see x=old
        Read(2, b"x", b"new"),
    ]

    violations = Checker(history).check()
    snap = [v for v in violations if v.invariant == "snapshot_read"]

    assert len(snap) > 0, "Reading future commit = snapshot violation"


def test_no_dirty_read_violation():
    """Reading a value from an uncommitted txn - dirty read FAIL"""

    history = [

        Begin(1, start_ts=1),
        Write(1, b"x", b"uncommitted_secret"),

        #Txn 1 still ACTIVE - no commit

        Begin(2, start_ts=1),
        Read(2, b"x", b"uncommitted_secret"), #BUG dirty read
    ]
    violations= Checker(history).check()

    dirty = [v for v in violations if v.invariant == "no_dirty_read"]
    assert len(dirty) > 0, "Reading uncommitted data= dirty read violation"


def test_abort_then_read_is_dirty():
    """
    Reading a value written by an ABORTED txn -> dirty read FAIL
    """
    history = [
        Begin(1, start_ts=1),
        Write(1, b"x", b"aborted_value"),
        Abort(1),

        Begin(2, start_ts=1),
        Read(2, b"x", b"aborted_value"), #BUG sees aborted data
    ]
    violations = Checker(history).check()
    dirty = [v for v in violations if v.invariant == "no_dirty_read"]
    assert len(dirty)>0