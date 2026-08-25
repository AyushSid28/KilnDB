import multiprocessing #we need this to create child process so that main process of pytest isn't effected
import os #we need this for os._exit(1) 
import pytest #testing framework from where we run our test
from wal import WAL, RecordType #we need this for wal
from fault import faults #we need this for faults


#This is the child process which will die
def run_child_and_crash(wal_path, fault_name):
    faults.set_fault(fault_name) #Remember that this point basically says to die when you reach this point
    wal= WAL(wal_path) #This opens wal.log and constructor recover the file even before writing check existing wal and repair it


    #This is the new record we write which will crash us so if everything good this record should not be found in the end
    payload = b"this record will crash"
    wal.append(RecordType.REDO_PUT, payload)

 

    # we should not reach here if the fault triggers an os._exit(1)


    wal.sync()
    wal.close()
    os._exit(0)


def test_phase1_crash_during_append(tmp_path):
    wal_file = tmp_path / "wal.log"
    wal_path = str(wal_file)

    #1.Write the good record first to ensure the file isn't empty

    wal = WAL(wal_path)

    #Start by creating a good record then sync it to make it durable (crash after sync won't harm it)
    wal.append(RecordType.REDO_PUT, b"good record")
    wal.sync()
    wal.close()


    #2. Spawn a child process to simulate the crash during the next append
    p = multiprocessing.Process(target=run_child_and_crash,
    args=(wal_path, 'during_wal_append'))

    #start the child process
    p.start()

    #waits for the child process to finish
    p.join()


    #The child process should have exited with code 1 because of os._exit(1)
    
    assert p.exitcode == 1, f"Expected exit code 1, got {p.exitcode}"

    #3. Reopen the WAL to simulate recovery
    #The _recover and truncate method should handle the torn record
    #without going into an infinite loop or creating bogus records
    wal = WAL(wal_path)
    records = list(wal.read_all())
     
    #4. Verify only the 'good record' is present and the torn  one was discarded 
    assert len(records) == 1
    _, rec_type, payload = records[0]
    
    assert rec_type== RecordType.REDO_PUT

    #Check the data so to confirm that crashed record didn't survived
    assert payload == b"good record"

    wal.close()