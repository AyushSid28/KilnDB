import os
import struct #Converts numbers in python to raw bytes on disk
import zlib #Used for CRC32 Checksum
from enum import IntEnum #Creates integer based enums 
from typing import Iterator, Tuple #Used for type hints
#tuple is a fixed set of values (str,int)
#An iterator is something you can loop through one element at a time.

from fault import faults

class RecordType(IntEnum):
    REDO_PUT = 1 #during recovery put the data again
    REDO_DEL = 2 #Delete this record during recovery.
    COMMIT = 3 #A signal that a transaction is finished.
    CHECKPOINT = 4 #A periodic marker to reset the "starting point" for recovery.

class WAL:#This is like a manager of our log
    # It handles:

    #writing records
    #syncing
    #recovery
    #reading logs
    # Header format: u32 (payload_length) + u32 (crc32) + u8(type) = 9bytes
    
    
    #This is the format of the header
    #"<IIB" means : Little-endian (<) 
    # I: unsigned int (4 bytes)
    # I: CRC32 CheckSum (4 bytes)
    # B: unsigned char (1 byte)
    # Together: 9 bytes
    HEADER_FORMAT= "<IIB"
    HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

    def __init__(self, filepath: str):

        self.filepath = filepath #Tells the class where the WAL file is located.
        
        #open for reading and appending. Create if it doesn't exist

        #Use unbuffered I/O (O_SYNC isn't required here because we manually fdatasync)
        #RDWR for read+write , CREAT for creating file if it doesnt exist , APPEND for appending to the end of the file
        self.fd = os.open(filepath, os.O_RDWR | os.O_CREAT | os.O_APPEND)

        #When opening, we must check for torn writes and recover
        #Basically if a record is corrupted or complete payload is missing then that specific record is removed
        self._recover_and_truncate()

    def append(self, record_type: int, payload: bytes) -> int:
        """
        Appends a record to the WAL and returns its LSN (byte offset).
        Note: This does NOT fsync. Call `sync()`
        separately.
        """
        #Get the length of the payload
        payload_length = len(payload)

        # Calculate CRC32 over (type+payload)

        #We pack the type into a byte to include it in CRC calculation 
        type_byte = struct.pack("<B", record_type)
        #Calculate CRC32 on (type + payload)
        crc = zlib.crc32(type_byte + payload) & 0xFFFFFFFF #This & 0xFFFFFFFF is used to ensure that the CRC is under 32 bytes

        header = struct.pack(self.HEADER_FORMAT,payload_length, crc, record_type)

        full_record = header + payload
       
        #LSN is the byte offset before we write this record
        #This gives the current position in the file and lseek means look for something
        lsn = os.lseek(self.fd,0, os.SEEK_END)


        #check for wal append fault so that we can inject a fault in the middle of the write
        faults.check('before_wal_append')

        #Now we need to inject a fault in the middle of the write
        if faults.active_fault == 'during_wal_append':
            #Now we can simply use the half of the writes and then add a crash after it
            half=max(1, len(full_record)//2)

            #Write only half of the record to the file 
            os.write(self.fd, full_record[:half])
            #This os._exit is used to immediately terminate the program  
            os._exit(1)



        #Write the full record to the file
        os.write(self.fd, full_record)

        return lsn


    def sync(self):
        """
        Forces the WAL to disk, making recent appends durable.
        """
 
        #writing is not enough ,crash can loose data so we need to sync it to disk
        #os.fdatasync is preferred on Linux/unix to avoid flushing metadata is size didn't change
        if hasattr(os, 'fdatasync'):
            os.fdatasync(self.fd)
        else:
            os.fsync(self.fd)

    def _recover_and_truncate(self):
        """
        Reads through the WAL from the beginning. If a torn record is found
        (bad length or bad CRC), truncates the file to last valid LSN.
        """
        os.lseek(self.fd, 0, os.SEEK_SET)
        valid_lsn = 0

        while True:
            header_bytes = os.read(self.fd, self.HEADER_SIZE)



            #EOF reached cleanely
            if len(header_bytes) < self.HEADER_SIZE:
                break

            payload_length, stored_crc, record_type = struct.unpack(self.HEADER_FORMAT, header_bytes)

            #Read the payload
            payload = os.read(self.fd, payload_length)
            
            #Torn payload (not enough bytes for the expected payload)
            if len(payload) < payload_length:
                break

            #Verify CRC
            type_byte = struct.pack("<B", record_type)
            calculated_crc = zlib.crc32(type_byte +
            payload) & 0xFFFFFFFF
    
            if calculated_crc != stored_crc:
                break
            
            #if record is valid, move the valid_lsn pointer forward
            valid_lsn += self.HEADER_SIZE + payload_length

        #truncate the file to remove any trailing garbage
        os.ftruncate(self.fd, valid_lsn)

        #seek to end so subsequent appends work correctly
        os.lseek(self.fd, 0, os.SEEK_END)

    def read_all(self) -> Iterator[Tuple[int, int, bytes]]:
        """
        Yields (lsn,record_type,payload) for all valid records in the WAL.
        Useful for DB recovery.
        """
        #Ensure we are reading from the beginning 
        os.lseek(self.fd, 0, os.SEEK_SET)
        #start from the 0 position in the file
        current_lsn = 0

        while True:
            header_bytes = os.read(self.fd,self.HEADER_SIZE)

            if not header_bytes or len(header_bytes)< self.HEADER_SIZE:
                break
        

            payload_length, _, record_type= struct.unpack(self.HEADER_FORMAT , header_bytes)

            payload= os.read(self.fd, payload_length)

            #return the (LSN,Type,Payload)
            yield ( current_lsn, record_type, payload)


            current_lsn += self.HEADER_SIZE + payload_length


    def close(self):
        os.close(self.fd)
