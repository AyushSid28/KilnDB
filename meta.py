import os
import struct
import json

class Meta:
    """
    Manages the 'meta' file in kiln-data/.
    Contains: checkpoint_lsn, next_txn_id, next_ts, page_size.

    Written atomically: write to meta.tmp -> fsync -> rename over meta-> fsync directory.
     
    If meta is missing or corrupt on startup, kiln starts from scratch (LSN 0).
    
    """

    def __init__(self, dirpath:str):
        self.dirpath = dirpath
        self.filepath = os.path.join(dirpath, "meta")
        self.tmp_filepath = os.path.join(dirpath, "meta.tmp")

        #Defaults for a frsh database

        self.checkpoint_lsn = 0
        self.next_txn_id = 1
        self.next_ts = 1
        self.page_size = 4096

    def load(self) -> bool:
        """
        Load metadata from disk. Returns True if loaded successfully,
        False if the file doesn't exist or is corrupt (fresh start).
        """

        if not os.path.exists(self.filepath):
            return False

        try:
            with open(self.filepath, "r") as f:
                data = json.load(f)

            self.checkpoint_lsn = data["checkpoint_lsn"]
            self.next_txn_id = data["next_txn_id"]
            self.next_ts = data["next_ts"]
            self.page_size = data.get("page_size",4096)

            return True

        except (json.JSONDecodeError, KeyError , OSError):
            #Corrupt or unreadable meta - fail closed , start fro scratch
            return False


    def save(self):
        """
        Atomically write metsdata to disk
        Order: write tmp ->fsync tmp-> rename-> fsync directory
        This ensures we never have a half-written meta file.
        
        """

        data = {
            "checkpoint_lsn": self.checkpoint_lsn,
            "next_txn_id": self.next_txn_id,
            "next_ts": self.next_ts,
            "page_size": self.page_size,    
        }

        #1.Write to tmp file
        fd = os.open(self.tmp_filepath,os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.write(fd, json.dumps(data).encode("utf-8"))

        #2. fsync the tmp file
        if hasattr(os, 'fdatasync'):
            os.fdatasync(fd)

        else:
            os.fsync(fd)

        os.close(fd)


        #3. Atomic  rename (replaces old meta)
        os.rename(self.tmp_filepath, self.filepath)

        #4. fsync the directory so the rename is durable 
        dir_fd = os.open(self.dirpath, os.O_RDONLY)
        os.fsync(dir_fd)
        os.close(dir_fd)
