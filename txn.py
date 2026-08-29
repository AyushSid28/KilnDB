from typing import Optional
from enum import Enum


class TxnState(Enum):
    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"

class WriteOp:
    """One write operation buffered in a transaction's write set"""

    def __init__(self,key: bytes,value: Optional[bytes],is_delete: bool = False):
        self.key = key
        self.value = value 
        self.is_delete = is_delete


class Transaction:
    """
    A single transaction.

    Writes go into a private write set (dict in RAM)
    Only at COMMIT do they hit the WAL
    ABORT just throws the dict away.
    """

    def __init__(self,txn_id: int, start_ts: int):
        self.txn_id = txn_id
        self.start_ts = start_ts
        self.commit_ts: Optional[int] = None
        self.state = TxnState.ACTIVE
        self.write_set: dict[bytes, WriteOp] = {}


    def put(self, key: bytes, value: bytes):
        """Buffer a PUT in write set."""
        if self.state != TxnState.ACTIVE:
            raise RuntimeError(f"Transaction {self.txn_id} is {self.state.value}, cannot PUT")


        self.write_set[key] = WriteOp(key=key, value=value, is_delete=False)


    def delete(self,key : bytes):
        """Buffer a DELETE in the write set."""
        if self.state != TxnState.ACTIVE:
            raise RuntimeError(f"Transaction {self.txn_id} is {self.state.value}, cannot DEL")

        self.write_set[key] = WriteOp(key=key, value=None, is_delete=True)

    def read_from_write_set(self, key: bytes) -> tuple[bool,Optional[bytes]]:
        """
        Check if key is in this txn's write set (read-your-own-writes)
        
        Returns (found, value)
        If found=True and value=None, the key was DEL in this txn.
        """

        if key in self.write_set:
            op = self.write_set[key]
            return (True, op.value)
        return (False, None)


    def abort(self):
        """
        Discard the write set. Nothing from this txn becomes visible
        """

        self.state = TxnState.ABORTED
        self.write_set.clear()