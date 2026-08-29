import os
import multiprocessing
import pytest
from engine import Engine

def test_single_txn_put_get_commit(tmp_path):
    """
    BEGIN -> PUT -> GET -> COMMIT. DATA visible after commit
    """

    db = Engine(str(tmp_path / "kiln-data"))
    txn = db.begin()

    db.put(txn, b"user:42", b"Ayush")

    #Read your own writes: visible before commit 
    assert db.get(txn, b"user:42") == b'Ayush'

    #Missing key return None, not an error
    assert db.get(txn, b"missing") is None

    db.commit(txn)

    #After commit, a NEW txn should see the data
    txn2 = db.begin()
    assert db.get(txn2, b"user:42") == b"Ayush"


    db.close()

def test_get_missing_returns_none(tmp_path):
    """
    GET of a missing key returns None (NOTFOUND), not an error.
    """

    db = Engine(str(tmp_path / "kiln-data"))
    txn =db.begin()
    assert db.get(txn, b"nonexistent") is None
    db.close()


def test_your_own_writes(tmp_path):
    """Uncommitted writes visible ONLY to writing txn."""

    db = Engine(str(tmp_path))
    txn = db.begin()
    db.put(txn, b"x", b"100")


    assert db.get(txn, b"x") == b"100"

    #A Different txn should NOT see it (its uncommitted)
    txn2 = db.begin()
    assert db.get(txn2, b"x") is None
    db.close()

def test_abort_discards_writes(tmp_path):
    """
    ABORT discards the private write set.Nothing becomes visible.
    """

    db = Engine(str(tmp_path / "kiln-data"))

    txn = db.begin()
    db.put(txn, b"temp", b"should diappear")
    txn.abort()

    txn2 = db.begin()
    assert db.get(txn2, b"temp") is None

    db.close()


def test_del_is_tombstone(tmp_path):
    """
    DEL makes a key return NOTFOUND. Older snapshots may still see the old value
    """

    db = Engine(str(tmp_path/ "kiln-data"))

    #Commit a value first
    txn = db.begin()
    db.put(txn, b"key", b"value")
    db.commit(txn)


    #Now delete it
    txn2 = db.begin()
    assert db.get(txn2, b"key") == b"value"
    db.delete(txn2, b"key")

    assert db.get(txn2, b"key") is None
    db.commit(txn2)

    txn3 = db.begin()
    assert db.get(txn3, b"key") is None
    

    db.close()


def test_multiple_puts_dame_key(tmp_path):
    """Multiple PUTs to the same Key in one txn - last write wins."""

    db = Engine(str(tmp_path / "kiln-data"))


    txn = db.begin()
    db.put(txn, b"x", b"first")
    db.put(txn, b"x", b"second")
    db.put(txn, b"x", b"third")

    assert db.get(txn, b"x") == b"third"
    db.commit(txn)

    txn2 = db.begin()
    assert db.get(txn2, b"x") == b"third"

    db.close()


#BIG CRASH TEST

def _child_commit_and_die(data_dir):
    """
    Chile process: commit a key, then crash AFTER the WAL is fsynced
    """

    db = Engine(data_dir)

    txn = db.begin()
    db.put(txn, b"survivor", b"i lived")
    db.commit(txn) # WAL is fsynced here The promise is made

    os._exit(1) #Crash. Heap may be incomplete Catalog is gone. But WAL is durable



def test_crash_after_commit_data_survives(tmp_path):
    """
    The BIG ONE: crash after commit ACK ->restart -> commited data survives

    THIS IS what makes kiln a real db and not just a dict
    """

    data_dir = str(tmp_path / "kiln-data")

    #Spawn a child that commits and then dies
    p = multiprocessing.Process(target=_child_commit_and_die, args=(data_dir,))
    p.start()
    p.join()


    assert p.exitcode==1 #child crashed


    #restart engine on the same data directory
    db = Engine(data_dir)

    #Recovery replayed the commited txn from WAL into the catalog

    txn = db.begin()
    result = db.get(txn, b"survivor")
    assert result == b"i lived", f"expected b'i lived' got {result}"


    db.close()
