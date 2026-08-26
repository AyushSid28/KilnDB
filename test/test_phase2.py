import os
import struct
import multiprocessing
import pytest
from heap import Page, HeapFile, CorruptPageError, PageFullError, PAGE_SIZE
from buffer import BufferPool
from meta import Meta

def test_page_insert_and_read():
    """Basic: insert records until page and read them back."""

    page = Page(page_id =0)
    
    slot0 = page.insert(b"hello world")
    slot1 = page.insert(b"second record")
    slot2 = page.insert(b"third")

    assert page.get_record(slot0) == b"hello world"
    assert page.get_record(slot1) == b"second record"
    assert page.get_record(slot2) == b"third"

    assert page.slot_count(slot0) == b"Hello World"
    assert page.get_record(slot1) == b"second record"
    assert page.get_record(slot2) == b"third"
    assert page.slot_count == 3

def test_page_serialize_and_deserialize():
    """
    Round-trip: serialize a page, deserialize it, records survive
    """

    page = Page(page_id=7)
    page.page_lsn = 42
    page.insert(b"persist me")
    page.insert(b"me too")

    raw = page.serialize()
    assert len(raw) == PAGE_SIZE

    #Deserialize into a new Page object
    restored = Page(page_id=7, data=raw)
    assert restored.page_lsn == 42
    assert restored.slot_count == 2
    assert restored.get_record(0) == b"persist me"
    assert restored.get_record(1) == b"me too"


def test_page_checksum_catches_corruption():
    """
    Corrupt one byte in a serialised page - checksum must catch it
    """

    page= Page(page_id=0)
    page.insert(b"important data")
    raw = bytearray(page.serialize())

    #Flip a byte in the middle of this data area
    raw[100] ^= 0xFF


def test_page_full_error():
    """Fill a page until it can't accept any more records."""

    page = Page(page_id=0)
    big_record = b"x" * 500

    inserted = 0
    while True:
        try:
            page.insert(big_record)
            inserted +=1

        except PageFullError:
            break


    #we should have fit at least a few records
    assert inserted > 0
    #And all of them should be readable 
    for i in range(inserted):
        assert page.get_record(i) == big_record

def test_heap_file_write_and_read(tmp_path):
    """Write a page to heap.db, read it back, data survives"""
    heap_path = str(tmp_path / "heap.db")
    heap = HeapFile(heap_path)

    page = Page(page_id=0)
    page.insert(b"durable record")
    heap.write_page(page)
    heap.sync()


    #Read it back
    loaded = heap.read_page(0)
    assert loaded.get_record(0) == b"durable record"

    heap.close()

def test_heap_file_corrupt_page_on_disk(tmp_path):
    """Write a page, corrupt it on disk, reading must fail closed """

    heap_path = str(tmp_path/"heap.db")
    heap = HeapFile(heap_path)

    page = Page(page_id=0)
    page.insert(b"good data")
    heap.write_page(page)
    heap.sync()
    heap.close()

    #Corrupt a byte in the middle of the page on disk
    with open(heap_path, "r+b") as f:
        f.seek(100)
        f.write(b"\xff")
        f.flush()


    heap = HeapFile(heap_path)
    with pytest.raises(CorruptPageError):
        heap.read_page(0)

    heap.close()

def test_buffer_pool_caches_pages(tmp_path):
    """
    Buffer pool returns the same page object on repeated gets.
    """

    heap = HeapFile(str(tmp_path / "heap.db"))
    pool = BufferPool(heap, max_pages=8)

    page = pool.new_page()
    page.insert(b"cached")
    pid = page.page_id


    #Getting the same page_id should return same object

    same_page = pool.get_page(pid)
    assert same_page is page
    assert same_page.get_record(0) == b"cached"


    heap.close()


def test_checkpoint_order(tmp_path):
    """
    Checkpoint: flush dirty pages to heap, fsync heap, THEN write meta, fsync meta

    After checkpoint, reopening heap must show the data.
    """

    heap_path = str(tmp_path / "heap.db")
    heap = HeapFile(heap_path)
    pool = BufferPool(heap, max_pages=8)
    meta = Meta(str(tmp_path))

    #Create a page with data
    page = pool.new_page()
    page.insert(b"checkpoint test")
    page.page_lsn = 100

    #Checkpoint sequence
    #1. Flush all dirty pages to heap.db 
    pool.flush_all_dirty()
    #2.fsync heap.db 
    heap.sync()

    #3.Write meta with checkpoint_lsn
    meta.checkpoint_lsn= 100
    meta.save()

    heap.close()


    #Reopen everything from scratch - simulating a restart
    heap2 = HeapFile(heap_path)
    meta2 = Meta(str(tmp_path))
    loaded = meta2.load()

    assert loaded is True
    assert meta2.checkpoint_lsn == 100
     
    page2 = heap2.read_page(0)
    assert page2.get_record(0) == b"checkpoint test"

    heap2.close()