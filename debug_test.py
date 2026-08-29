import tempfile, os, multiprocessing
from engine import Engine
from fault import faults

def child_checkpoint_with_fault(data_dir, fault_name):
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

d = tempfile.mkdtemp()
data_dir = os.path.join(d, "kiln-data")

# Parent commits
db = Engine(data_dir)
txn = db.begin()
db.put(txn, b"safe_key", b"safe_value")
db.commit(txn)
db.close()

print("WAL size after parent:", os.path.getsize(os.path.join(data_dir, "wal.log")))

# Child crashes during checkpoint
p = multiprocessing.Process(target=child_checkpoint_with_fault, args=(data_dir, "during_heap_page_write"))
p.start()
p.join()
print("Child exit code:", p.exitcode)

print("WAL size after child:", os.path.getsize(os.path.join(data_dir, "wal.log")))
print("Meta exists:", os.path.exists(os.path.join(data_dir, "meta")))

# Parent re-opens
db3 = Engine(data_dir)
txn3 = db3.begin()
r = db3.get(txn3, b"safe_key")
print("safe_key after recovery:", r)
db3.close()
