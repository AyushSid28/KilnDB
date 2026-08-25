import os
import pytest
from wal import WAL, RecordType

def test_wal_append_and_truncate(tmp_path):

    wal_file = tmp_path / "wal.log"
    wal_path = str(wal_file)

    #1.Open the wal and write 1000 records
    wal= WAL(wal_path)
    for i in range(1000):
        payload=f"value_{i}".encode("utf-8")
        wal.append(RecordType.REDO_PUT, payload)

    #sync and close it safely
    wal.sync()
    wal.close()

    #2.Check the size and verify we can read all 1000 back correctly
    wal= WAL(wal_path)
    records = list(wal.read_all())
    assert len(records) == 1000

    #Check the last record just to be sure
    _, rec_type, payload = records[-1]
    assert rec_type == RecordType.REDO_PUT
    assert payload == b"value_999"
    wal.close()


    #3. Simulate a torn tail by chopping off the last 7 bytes
    file_size= os.path.getsize(wal_path)
    with open(wal_path,"a") as f:
        os.ftruncate(f.fileno(), file_size-7)


    #4 Reopen the WAL. It should detect the torn,tail,truncate the file back
    #to the 999th record, and successfully recover 999 records.
    wal= WAL(wal_path)
    records = list(wal.read_all())

    assert len(records) == 999
    #Check the new last record
    _, rec_type, payload = records[-1]
    assert payload == b"value_998"
    wal.close()