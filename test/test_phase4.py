import pytest
from engine import Engine, ConflictError

def test_first_committer_wins(tmp_path):
    """
    Two txns both read bal=1000, both PUT balance.
    First to COMMIT wins. Second gets ERR conflict.
    """

    db = Engine(str(tmp_path / "kiln-data"))

    #Setup: balance=1000
    setup = db.begin()
    db.put(setup, b"balance", b"1000")
    db.commit(setup)

    #Two concurrent transactions both read balance
    txn_a = db.begin()
    txn_b = db.begin()

    val_a = db.get(txn_a, b"balance")
    val_b = db.get(txn_b, b"balance")

    assert val_a == b"1000"
    assert val_b == b"1000"

    #Both try to write balance
    db.put(txn_a, b"balance",b"300")
    db.put(txn_b, b"balance",b"500")

    #A commits first - wins
    db.commit(txn_a)

    # B tries to commit - loses(first-committer-wins)
    with pytest.raises(ConflictError):
        db.commit(txn_b)

    # Final Value is A's write
    txn_check = db.begin()
    assert db.get(txn_check, b"balance") == b"300"

    db.close()


def test_no_conflict_on_different_keys(tmp_path):
    """Two txns writing DIFFERENT keys should both commit successfully."""
    db = Engine(str(tmp_path / "kiln-data"))
    txn_a = db.begin()
    txn_b = db.begin()

    db.put(txn_a, b"x", b"1")
    db.put(txn_b, b"y", b"2")

    db.commit(txn_a)
    db.commit(txn_b)

    txn_check = db.begin()
    assert db.get(txn_check, b"x") == b"1"
    assert db.get(txn_check, b"y") == b"2"

    db.close()

def test_snapshot_isolation_no_dirty_reads(tmp_path):
    """
    A txn started BEFORE another txn's commit should NOT see the committed data
    Snapshots freeze time.
    """

    db = Engine(str(tmp_path / "kiln-data"))

    #Setup x=100
    setup = db.begin()
    db.put(setup, b"x", b"100")
    db.commit(setup)

    #txn_old starts - snapshot is BEFORE any future commits
    txn_old=db.begin()

    #txn_new commits x=200
    txn_new = db.begin()
    db.put(txn_new, b"x", b"200")
    db.commit(txn_new)

    #txn_old should still see x=100 (its snapshot is frozen)
    assert db.get(txn_old, b"x") == b"100"

    #A brand new txn sees x = 200
    txn_latest= db.begin()
    assert db.get(txn_latest, b"x") == b"200"

    db.close()


def test_del_tombstone_old_snapshot(tmp_path):
    """
    DEL is a versioned tombstone  that started 
    before the DEL should still see the prior values
    """

    db = Engine(str(tmp_path / "kiln-data"))

    #Setup: key = "alive"
    setup = db.begin()
    db.put(setup, b"key", b"alive")
    db.commit(setup)

    #txn_old starts - sees key = "alive"
    txn_old = db.begin()
    assert db.get(txn_old, b"key") == b"alive"

    #txn_del deletes the key
    txn_del = db.begin()
    db.delete(txn_del, b"key")
    db.commit(txn_del)

    #txn_old still sees the old value (its snapshot is before the delete)
    assert db.get(txn_old, b"key") == b"alive"

    #A new txn sees NOTFOUND
    txn_new = db.begin()
    assert db.get(txn_new, b"key") is None

    db.close()


def test_conflict_detection_at_commit_not_at_put(tmp_path):
    """
    PUT into the write set always succeeds.
    Conflict is only checked at COMMIT time.
    """

    db = Engine(str(tmp_path / "kiln-data"))

    setup=db.begin()
    db.put(setup, b"x", b"original")
    db.commit(setup)

    txn_a = db.begin()
    txn_b = db.begin()

    #Both PUTs succeed - no error at PUT time
    db.put(txn_a, b"x", b"from_a")
    db.put(txn_b, b"x", b"from_b")

    #A commits first
    db.commit(txn_a)

    #B's PUT was fine, but COMMIT fails
    with pytest.raises(ConflictError):
        db.commit(txn_b)


    db.close()



def test_write_skew_is_allowed(tmp_path):
    """
    WRITE SKEW - this is ALLOWED under snapshot isolation.
    we do NOT claim serializable We name this as known gap.
    
    Scenario (the classic doctors example):
      - Doctor A is on_call, Doctor B is on_call
      - Rule: at least one doctor must be on call
      - Txn 1 reads both, sees both on_call, sets A = off_call
      - Txn 2 reads both, sees both on_call, sets B = off_call
      - Both commit (they wrote DIFFERENT keys — no conflict!)
      - Result: BOTH are off_call — the invariant is broken
    This is expected behavior for snapshot isolation.
    Serializable isolation would catch this, but we explicitly don't implement it.
    """

    db = Engine(str(tmp_path / "kiln-data"))

    #Setup: both doctors on call
    setup = db.begin()
    db.put(setup, b"doctor_a", b"on_call")
    db.put(setup, b"doctor_b", b"on_call")
    db.commit(setup)


    #Txn 1:reads both, decides to take A off call
    txn1 = db.begin()
    a1 = db.get(txn1, b"doctor_a")
    b1 = db.get(txn1, b"doctor_b")
    assert a1 == b"on_call"
    assert b1 == b"on_call"

    #B is still on call, so its safe to take A off
    db.put(txn1, b"doctor_a", b"off_call")

    #Txn2: reads both, decides to take B off call
    txn2 = db.begin()
    a2 = db.get(txn2, b"doctor_a")
    b2 = db.get(txn2, b"doctor_b")
    assert a2 == b"on_call" #Sanpshot does not see txn1's uncommitted write
    assert b2 == b"on_call"


    # A is still on call so its sade to take B off
    db.put(txn2, b"doctor_b", b"off_call")


    #Both commit- no conflict because they wrote DIFFERENT keys
    db.commit(txn1)
    db.commit(txn2)

    #Result: Both doctors are off call, Invariant broken
    #This is write skew and it is allowed under snapshot isolation
    check = db.begin()
    assert db.get(check, b"doctor_a") == b"off_call"
    assert db.get(check, b"doctor_b") == b"off_call"


    db.close()


