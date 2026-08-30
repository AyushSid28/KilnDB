import os
import struct #Converts python objects into binary bytes
import zlib #Used for crc32 checksum it catches corrupted pages
from fault import faults

PAGE_SIZE = 4096 #Database storage unit is basically a page

#HEADER_FORMAT = "<IIQI HH" 
HEADER_FORMAT = "<IIQHH" #Page header layout
HEADER_SIZE = struct.calcsize(HEADER_FORMAT) #20 bytes
# 1) Page ID- I (4bytes)
# 2) Checksum (CRC32)- I (4bytes)
# 3) Page LSN- Q (8bytes)
# 4) Slot Count- H(2bytes)
# 5) Free Space Pointer- H(2bytes)

#Total header size is 20 bytes

SLOT_FORMAT = "<HH" #Offset- H(2bytes)
SLOT_SIZE = struct.calcsize(SLOT_FORMAT)

class CorruptPageError(Exception): #Raised when checkum fails means disk data cant be trusted 
    """Raised when a page's checksum doesn't match.
    kiln fails closed."""

    pass

class PageFullError(Exception): #When records and slots collide means the page is full no extra data can be added 
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

        #Create an empty new page or use an existing page
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
        #it starts after byte 8 because (0-3) page_id + (4-7) checksum
        return zlib.crc32(raw[8:]) & 0xFFFFFFFF


    #This loads page from disk
    def _deserialize(self, raw: bytes):
        """
        Parse a raw 4096 byte page from disk.
        """
        if len(raw) != PAGE_SIZE:
            raise CorruptPageError(f"Page {self.page_id}: expected {PAGE_SIZE} bytes, got {len(raw)}")



        #verify checksum FIRST - fail closed if bad
        stored_page_id, stored_checksum, self.page_lsn, self.slot_count, self.free_end = \
            struct.unpack_from(HEADER_FORMAT, raw, 0)

        #if calculated checksum is equal to stored checksum then load the page if not then simply throw an exception of CorruptPage Error
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

             #Finally return the full page data as bytes
            return bytes(self.data)

    #Slot directory is a tiny notebook that says where the record is stored on the page
    def _record_end(self) -> int:
            """
            Byte offset where the next record would start (right after existing records)
            """

            if self.slot_count == 0:
                 return HEADER_SIZE

            #we calculate the position of last slot in the page and then get the offset and length from notebook
            last_slot_pos = PAGE_SIZE - (self.slot_count * SLOT_SIZE)
            offset, length = struct.unpack_from(SLOT_FORMAT,self.data ,last_slot_pos)

            return offset + length

    #As we already have the offset and length data of slot 
    def insert(self, record_data: bytes)-> int:
            """Insert a record into the page. Returns the slot index(0-based)
            Raises PageFullError if there's no room.
            """
            #We get the length of the record and store it in record length
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

            #here we increase the count of slot in the page and free end pointer
            self.slot_count += 1
            self.free_end = new_slot_pos
            self.dirty = True
            return self.slot_count - 1

    #If the slot index is out of range, then it will raise an exception
    def get_record(self, slot_index: int) -> bytes:
            """Read a record by its slot index"""
            if slot_index < 0 or slot_index >= self.slot_count:
                raise IndexError(f"Slot {slot_index} out of range (page has {self.slot_count} slots)")

            #Find the record in the page using the slot index
            slot_pos = PAGE_SIZE - ((slot_index + 1) * SLOT_SIZE)
            offset, length = struct.unpack_from(SLOT_FORMAT, self.data, slot_pos)

            #Now return the record data from offset to length
            return bytes(self.data[offset:offset+ length])

    #Here we can update a record in-place if the new data is smaller than the old data
    def update_record(self, slot_index: int, new_data: bytes):
            """
            Update a record in-place. The new data must be same size or smaller.
            (v1 does not support growing records - allocate a new slot instead)
            """
            #if the record index is out of range then it will raise an exception
            if slot_index < 0 or slot_index >= self.slot_count:
                raise IndexError(f"Slot {slot_index} out of range")

            slot_pos = PAGE_SIZE - ((slot_index + 1) * SLOT_SIZE )
            offset,length = struct.unpack_from(SLOT_FORMAT, self.data , slot_pos)

            #If the new data is larger than the old data, then it will raise an exception
            if len(new_data) > length:
                raise ValueError(f"New data ({len(new_data)} bytes) is larger than slot ({length} bytes)."
                f"v1 does not support in-place growth.")

            #write the new data into the slot
            self.data[offset:offset + len(new_data)] = new_data
            #Update slot length if new data is smaller
            struct.pack_into(SLOT_FORMAT, self.data, slot_pos, offset, len(new_data))
            self.dirty = True


#Now we will be managing many pages in the heap.db
class HeapFile:
    """
    Manages reading/writing Page objects to/from the heap.db file.
    Each page lives at offset page_id * PAGE_SIZE
    """

    #The heap file class will basically manage the pages of the heap.db file
    #We give it the path to the heap.db file and it will open it in read/write mode with create flag

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.fd = os.open(filepath, os.O_RDWR | os.O_CREAT)

    #Reading a page from disk
    #If the page_id is out of range then it will raise an exception
    #If the page is corrupt then it will raise an exception
    #If the page is not found then it will return a fresh page
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


    #Syncing the pages to disk
    #If the page is not synced and is not only on ram we have to sync it to be available on the disk
    def sync(self):
        """Force all written pages to disk"""
        if hasattr(os, 'fdatasync'):
            os.fdatasync(self.fd)

        else:
            os.fsync(self.fd)


    def close(self):
        os.close(self.fd)

    