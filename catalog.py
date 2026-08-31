from dataclasses import dataclass 
from typing import Optional

@dataclass
# begin_ts: When this version became valid (commit timestamp).
# end_ts: When it stopped being valid. 0 means it is still current.
# value: The stored bytes for this version.
# is_tombstone: True means this version represents a delete.

# Version	begin_ts	end_ts	Meaning
# V1	        5	     12	    Valid from commit 5 until commit 12.
class Version:
    """One committed version of a key"""
    begin_ts: int    #commit_ts of the txn that created this version

    end_ts: int = 0   #commit_ts of the txn that overwrote it (0 = still current)

    value: Optional[bytes] = None

    is_tombstone: bool = False

#This is in memory MVCC catalog hich acts like a dictionary that remembers every committed version of every key in db
class Catalog:
    """
    In memory index: key(bytes) list of version objects.
    Rebuilt on recovery by replacing commited WAL records.

    This is NOT on disk. If the process dies, the catalog is gone.
    Recovery rebuilds it from WAL + heap checkpoint.
    
    """

    def __init__(self):
        self.versions: dict[bytes, list[Version]] = {}

    #this is used to basically add a new committed version and mark the previous committed version as invalid
    #end_t =0 because at beginning because its the newest version and has not been overwritten yet
    def install_version(self, key: bytes,version: Version):
        """
        Add a new committed version for a key.
        Closes out the previous latest version's end_ts.
        """

        if key not in self.versions:
            self.versions[key] = []

        #close out the previous "current" version
        versions= self.versions[key]
        #After adding the new version, while closing the previous version the end_ts of previous version is set to the begin_ts of the new version
        #We need to update the end_ts everytime because without it both version uld appear current which would break MVCC
        if versions:
            latest = versions[-1]
            if latest.end_ts == 0:
                latest.end_ts = version.begin_ts

        versions.append(version)

    #This is the logic for snapshot isolation 

#A transaction asks the catalog for the latest visible version of a key at a given start_ts
#here start_ts is for a transaction and begin_ts and end_ts is for a version
#     Visibility Rule

# A version is visible if: begin_ts≤start_ts<end_ts or end_ts == 0.

# Meaning:

# Condition	Meaning


# begin_ts ≤ start_ts :The version existed before the transaction began.
# end_ts == 0 :Version is still current.
# end_ts > start_ts :Version had not been overwritten yet.
    def get_visible(self, key: bytes, start_ts: int) -> Optional[Version]:
        """
        Find the version of key visible at start_ts.
        Visible means: begin_ts <= start_ts AND (end_ts == 0 OR end_ts > start_ts)
        Returns None if key has no visible version.
        """
        if key not in self.versions:
            return None

        #Walk backwards (newest first) to find the latest visible version

      #We find the version in reverse because we want to find the latest version that is visible to the start_ts
        for version in reversed(self.versions[key]):
            if version.begin_ts <= start_ts:
                if version.end_ts == 0 or version.end_ts> start_ts:
                    return version
            
        return None

    def get_latest_commit_ts(self, key: bytes) -> int:
        """
        Return the begin_ts of latest version, or 0 if key has no version.
        Used in Phase 4 for first-commiter-wins conflict detection.
        """

        if key not in self.versions or not self.versions[key]:
            return 0

        return self.versions[key][-1].begin_ts


