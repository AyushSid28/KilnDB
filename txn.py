from typing import Optional #Optional means a variable can either have a value or None
from enum import Enum

#This is used to track the state of a transaction
#ACTIVE means the transaction is active and can write to the database
#COMMITTED means the transaction has committed and its write set has been written to the WAL
#ABORTED means the transaction has aborted and its write set has been discarded
class TxnState(Enum):
    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"

#THis is used to represent a write operation in a transaction's write set
#key: The key of the write operation
#value: The value of the write operation
#is_delete: True if the write operation is a delete
class WriteOp:
    """One write operation buffered in a transaction's write set"""

    def __init__(self,key: bytes,value: Optional[bytes],is_delete: bool = False):
        self.key = key
        self.value = value 
        self.is_delete = is_delete

#This is used for tracking a single transaction
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

   #This is used to abort a transaction and discard its write set
    def abort(self):
        """
        Discard the write set. Nothing from this txn becomes visible
        """

        self.state = TxnState.ABORTED
        self.write_set.clear()