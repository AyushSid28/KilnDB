import os
import struct
import threading
from typing import Optional

from wal import WAL, RecordType
from heap import HeapFile
from buffer import BufferPool
from meta import Meta
from catalog import Catalog, Version
from txn import Transaction, TxnState
from fault import faults

class ConflictError(Exception):
    """Raised when a transaction loses the first-committer-wins check"""
    pass

def encode_redo_put(txn_id: int, commit_ts: int, key: bytes, value: bytes) -> bytes:
    return struct.pack("<QQH", txn_id, commit_ts, len(key)) + key + struct.pack("<I",len(value)) + value


def decode_redo_put(payload: bytes):
    txn_id, commit_ts, key_len = struct.unpack_from("<QQH",payload, 0)
    offset = 18
    key = payload[offset:offset + key_len]
    offset += key_len
    value_len = struct.unpack_from("<I", payload, offset)[0]
    offset +=4
    value = payload[offset:offset + value_len]
    return txn_id, commit_ts, key, value
    
def encode_redo_del(txn_id: int, commit_ts: int, key: bytes) -> bytes:
    return struct.pack("<QQH", txn_id, commit_ts, len(key))+ key


def decode_redo_del(payload: bytes):
    txn_id, commit_ts, key_len = struct.unpack_from("<QQH",
    payload, 0)
    key = payload[18:18 + key_len]
    return txn_id, commit_ts, key

def encode_commit(txn_id: int, commit_ts: int) -> bytes:
    return struct.pack("<QQ", txn_id, commit_ts)

def decode_commit(payload: bytes):
    return struct.unpack("<QQ", payload)

#The Engine
class Engine:
    """
    The kiln database Engine
    
    Lifecycle(data_dir) -> recovers from WAl -> ready for txns 
    begin() -> get/zput/Delete -> commit() or abort()
    close()
    """

    def __init__(self, data_dir:str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)


        self.wal = WAL(os.path.join(data_dir, "wal.log"))
        self.heap_file = HeapFile(os.path.join(data_dir, "heap.db"))

        self.pool = BufferPool(self.heap_file, max_pages=120)
        self.meta = Meta(data_dir)
        self.catalog = Catalog()


        #v1 one commit at a time
        self.commit_lock = threading.Lock()


        #Monotone counters
        self.next_ts = 1
        self.next_txn_id = 1

        self._recover()

    def _recover(self):
        """
        Recovery 
        1.Load meta to get checkpoint_lsn
        2.Read WAL from checkpoint_lsn onward.
        3. Find which txns have COMMIT records.
        4. Replay only commited txns into the catalog
        """
        loaded = self.meta.load()
        if loaded:
            self.next_txn_id = self.meta.next_txn_id
            self.next_ts = self.meta.next_ts

        checkpoint_lsn = self.meta.checkpoint_lsn

        # Two-pass approach:
        # Pass 1: find all committed txn_ids
        committed_txns = {}
        for lsn, rec_type, payload in self.wal.read_all():
            if lsn < checkpoint_lsn:
                continue
            if rec_type == RecordType.COMMIT:
                txn_id, commit_ts = decode_commit(payload)
                committed_txns[txn_id] = commit_ts

        # Pass 2: replay their redo records
        for lsn, rec_type, payload in self.wal.read_all():
            if lsn < checkpoint_lsn:
                continue
            
            if rec_type == RecordType.REDO_PUT:
                txn_id, commit_ts, key, value = decode_redo_put(payload)
                if txn_id in committed_txns:
                    version = Version(begin_ts=commit_ts, value=value, is_tombstone=False)
                    self.catalog.install_version(key, version)
            
            elif rec_type == RecordType.REDO_DEL:
                txn_id, commit_ts, key = decode_redo_del(payload)
                if txn_id in committed_txns:
                    version = Version(begin_ts=commit_ts, value=None, is_tombstone=True)
                    self.catalog.install_version(key, version)

        # Advance counters past anything we replayed
        for commit_ts in committed_txns.values():
            if commit_ts >= self.next_ts:
                self.next_ts = commit_ts + 1

        for txn_id in committed_txns:
            if txn_id >= self.next_txn_id:
                self.next_txn_id = txn_id + 1
    
    #Transaction API

    def begin(self) -> Transaction:
        """
        Start a new transaction. Allocates txn_id and start_ts
        """

        txn_id = self.next_txn_id
        self.next_txn_id += 1
        start_ts = self.next_ts - 1
        return Transaction(txn_id, start_ts)


    def get(self, txn: Transaction, key: bytes) -> Optional[bytes]:
        """
        GET path

        1.If key is in this txn's write set -> return that value (read-your-own-writes)
        2.Else check catalog for version visible at start_ts
        3.Else return None (NOT FOUND)
        """
        if txn.state != TxnState.ACTIVE:
            raise RuntimeError(f"Transaction {txn.txn_id} is {txn.state.value}")
        # Step 1: read-your-own-writes
        found, value = txn.read_from_write_set(key)

        if found:
            return value

        #Step 2:Catalog lookup
        version = self.catalog.get_visible(key, txn.start_ts)
        if version is None or version.is_tombstone:
            return None
        
        return version.value


    
    def put(self,txn: Transaction, key: bytes,value: bytes):
        """PUT into the private write set. Does NOT touch WAL or heap."""
        txn.put(key, value)

    def delete(self, txn:Transaction, key: bytes):
        """DEL into the private write set. Does NOT touch WAL or heap."""

        txn.delete(key)


    def commit(self, txn: Transaction) -> bool:
        """
        COMMIT 
        1. Acquire commit lock
        2. Conflict check here
        3. Append redo records + COMMIT record to WAL
        4. fdatasync(WAL) - durable
        5. ACK OK
        6. Install versions in catalog
        """

        if txn.state != TxnState.ACTIVE:
            raise RuntimeError(f"Transaction {txn.txn_id} is {txn.state.value}")


        #Read only txn - nothing to write
        if not txn.write_set:
            txn.state = TxnState.COMMITTED
            return True

        with self.commit_lock:
            #Conflict check
            for key in txn.write_set:
                latest_ts = self.catalog.get_latest_commit_ts(key)
                if latest_ts > txn.start_ts:
                    txn.abort()
                    raise ConflictError(
                        f"Conflict on key {key!r}"
                        f"Committed version at ts={latest_ts}> start_ts={txn.start_ts}"
                    )

            #Allocate commit timestamp
            commit_ts = self.next_ts
            self.next_ts += 1
            txn.commit_ts = commit_ts

            #Write redo records to WAL
            for key, op in txn.write_set.items():
                if op.is_delete:
                    payload = encode_redo_del(txn.txn_id, commit_ts, key)
                    self.wal.append(RecordType.REDO_DEL, payload)
                else:
                    payload= encode_redo_put(txn.txn_id, commit_ts, key, op.value)
                    self.wal.append(RecordType.REDO_PUT, payload)

            #write the COMMIT record
            commit_payload = encode_commit(txn.txn_id, commit_ts)
            self.wal.append(RecordType.COMMIT, commit_payload)

            #DURABLE fsync the WAL
            self.wal.sync()

            faults.check("after_wal_sync_before_ack")

            #Install versions in catalog (in-memory)
            for key, op in txn.write_set.items():
                if op.is_delete:
                    version = Version(begin_ts=commit_ts, value=None, is_tombstone=True)
                else:
                    version = Version(begin_ts=commit_ts, value=op.value, is_tombstone=False)

                self.catalog.install_version(key, version)

            txn.state = TxnState.COMMITTED

            faults.check('after_ack_before_heap')

            return True

    def checkpoint(self):
        """
        Checkpoint
        1.Commit lock ( no commits mid checkpoint)
        2. Flush dirty pages -> heap.db
        3. fdatasync(heap.db)
        4. Write meta (checkpoint_lsn)
        5. fdatasync(meta)

        """

        with self.commit_lock:
            dirty_count = self.pool.get_dirty_count()
            if dirty_count == 0:
                return

            self.pool.flush_all_dirty()
            self.heap_file.sync()

            wal_size = os.path.getsize(os.path.join(self.data_dir, "wal.log"))

            self.meta.checkpoint_lsn = wal_size
            self.meta.next_txn_id = self.next_txn_id
            self.meta.next_ts = self.next_ts
            self.meta.save()

    def close(self):
        self.wal.close()
        self.heap_file.close()






