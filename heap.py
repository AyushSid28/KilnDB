import os
import struct
import zlib
from fault import faults

PAGE_SIZE = 4096

HEADER_FORMAT = "<IIQI HH"
HEADER_FORMAT = "<IIQHH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT) #20 bytes

SLOT_FORMAT = "<HH"
SLOT_SIZE = struct.calcsize(SLOT_FORMAT)

class CorruptPageError(Exception):
    """Raised when a page's checksum doesn't match.
    kiln fails closed."""

    pass

class PageFullError(Exception):
    """Raised when a record doesnt fit on a page"""
    pass

class Page:
    """
    A single 4096-byte slotted page.


    Layout:
    [ HEADER (20 bytes) ]
    [ record data growing from byte 20 upwards ... ]
    [ ... free space ... ]
    [ ... slot entries growing backward from byte 4095]

    Record grow forward (after the header).
    Slots grow backward (from the end of page).
    when they meet page is full.
    """
 
    #Constructor
    def __init__(self, page_id: int, data: bytes=None):
        self.page_id = page_id
        self.page_lsn = 0
        self.dirty = False

        if data is not None:
            #Loading an existing page from disk
            self._deserialize(data)
        else:
            #Brand new Empty page
            self.slot_count = 0
            
            # free_end starts at very end of the page(slots will eat into this)
            self.free_end = PAGE_SIZE
            #The actual page buffer
            self.data = bytearray(PAGE_SIZE)


    def _compute_checksum(self, raw:bytes) -> int:
        """
        Compute CRC32 over everything after the checksum field (bytes 8 onward)
        """
        #Checksum covers:page_lsn + slot_count + free_end + all data/slots
        return zlib.crc32(raw[8:]) & 0xFFFFFFFF



    def _deserialize(self, raw: bytes):
        """
        Parse a raw 4096 byte page from disk.
        """
        if len(raw) != PAGE_SIZE:
            raise CorruptPageError(f"Page {self.page_id}: expected {PAGE_SIZE} bytes, got {len(raw)}")



        #verify checksum FIRST - fail closed if bad
        stored_page_id, stored_checksum, self.page_lsn, self.slot_count, self.free_end = \
            struct.unpack_from(HEADER_FORMAT, raw, 0)

        calculated_checksum = self._compute_checksum(raw)
        if calculated_checksum != stored_checksum:
            raise CorruptPageError(
                f"Page {self.page_id}: checksum mismatch "
                f"stored={stored_checksum}, calculated={calculated_checksum}"
                f"Refusing to serve corrupt data."
            )

        self.data = bytearray(raw)


    def serialize(self) -> bytes:
            """
            Pack this page into exactly 4096 bytes for writing to disk
            """
            #write the header (checksum=0 placeholder first)

            struct.pack_into(HEADER_FORMAT, self.data, 0,
                            self.page_id, 0, self.page_lsn,
            self.slot_count, self.free_end)

            #Now compute real checksum and write it into checksum field (bytes 4-7)
            checksum = self._compute_checksum(bytes(self.data))

            struct.pack_into("<I", self.data , 4, checksum)

            return bytes(self.data)


    def _record_end(self) -> int:
            """
            Byte offset where the next record would start (right after existing records)
            """

            if self.slot_count == 0:
                 return HEADER_SIZE

            last_slot_pos = PAGE_SIZE - (self.slot_count * SLOT_SIZE)
            offset, length = struct.unpack_from(SLOT_FORMAT,self.data ,last_slot_pos)

            return offset + length

    def insert(self, record_data: bytes)-> int:
            """Insert a record into the page. Returns the slot index(0-based)
            Raises PageFullError if there's no room.
            """

            record_len = len(record_data)
            record_start = self._record_end()

            #The new slot entry will be placed at:
            new_slot_pos = PAGE_SIZE - ((self.slot_count + 1) * SLOT_SIZE)
            
            #Check if record + slot would collide
            if record_start + record_len > new_slot_pos:
                raise PageFullError(f"Page {self.page_id} is full. "
                                    f"Need {record_len} bytes but only {new_slot_pos - record_start} free.")


            #write the record data
            self.data[record_start:record_start + record_len] = record_data

            #write the slot entry (at the end, growing backward)
            struct.pack_into(SLOT_FORMAT, self.data, new_slot_pos, record_start, record_len)

            self.slot_count += 1
            self.free_end = new_slot_pos
            self.dirty = True
            return self.slot_count - 1


    def get_record(self, slot_index: int) -> bytes:
            """Read a record by its slot index"""
            if slot_index < 0 or slot_index >= self.slot_count:
                raise IndexError(f"Slot {slot_index} out of range (page has {self.slot_count} slots)")


            slot_pos = PAGE_SIZE - ((slot_index + 1) * SLOT_SIZE)
            offset, length = struct.unpack_from(SLOT_FORMAT, self.data, slot_pos)

            return bytes(self.data[offset:offset+ length])


    def update_record(self, slot_index: int, new_data: bytes):
            """
            Update a record in-place. The new data must be same size or smaller.
            (v1 does not support growing records - allocate a new slot instead)
            """

            if slot_index < 0 or slot_index >= self.slot_count:
                raise IndexError(f"Slot {slot_index} out of range")

            slot_pos = PAGE_SIZE - ((slot_index + 1) * SLOT_SIZE )
            offset,length = struct.unpack_from(SLOT_FORMAT, self.data , slot_pos)

            if len(new_data) > length:
                raise ValueError(f"New data ({len(new_data)} bytes) is larger than slot ({length} bytes)."
                f"v1 does not support in-place growth.")

            self.data[offset:offset + len(new_data)] = new_data
            #Update slot length if new data is smaller
            struct.pack_into(SLOT_FORMAT, self.data, slot_pos, offset, len(new_data))
            self.dirty = True



class HeapFile:
    """
    Manages reading/writing Page objects to/from the heap.db file.
    Each page lives at offset page_id * PAGE_SIZE
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.fd = os.open(filepath, os.O_RDWR | os.O_CREAT)


    def read_page(self, page_id: int) -> Page:
        """Read a page from disk. Raise a CorruptPageError if checksum fails"""

        offset = page_id * PAGE_SIZE
        os.lseek(self.fd, offset, os.SEEK_SET)
        raw= os.read(self.fd, PAGE_SIZE)

        if len(raw) < PAGE_SIZE:
          #Page doesn't exist on disk yet - return a fresh one
          return Page(page_id)

        return Page(page_id, raw)

    def write_page(self,page:Page):
        """Write a page to disk (does NOT fsync)."""
        raw = page.serialize()
        offset = page.page_id * PAGE_SIZE
        os.lseek(self.fd, offset, os.SEEK_SET)


        if faults.active_fault == 'during_heap_page_write':
            #write only half the page,then crash.
            #This simulates a powerloss mid-page-write
            #This page on disk will have garbage in secondhalf

            #Checksum will catch it.WAL has the truth
            half = len(raw) //2
            os.write(self.fd, raw[:half])
            os._exit(1)
        os.write(self.fd, raw)
        page.dirty = False


    def sync(self):
        """Force all written pages to disk"""
        if hasattr(os, 'fdatasync'):
            os.fdatasync(self.fd)

        else:
            os.fsync(self.fd)


    def close(self):
        os.close(self.fd)

    