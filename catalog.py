from dataclasses import dataclass 
from typing import Optional

@dataclass
class Version:
    """One committed version of a key"""
    begin_ts: int    #commit_ts of the txn that created this version

    end_ts: int = 0   #commit_ts of the txn that overwrote it (0 = still current)

    value: Optional[bytes] = None

    is_tombstone: bool = False

class Catalog:
    """
    In memory index: key(bytes) list of version objects.
    Rebuilt on recovery by replacing commited WAL records.

    This is NOT on disk. If the process dies, the catalog is gone.
    Recovery rebuilds it from WAL + heap checkpoint.
    
    """

    def __init__(self):
        self.versions: dict[bytes, list[Version]] = {}

    def install_version(self, key: bytes,version: Version):
        """
        Add a new committed version for a key.
        Closes out the previous latest version's end_ts.
        """

        if key not in self.versions:
            self.versions[key] = []

        #close out the previous "current" version
        versions= self.versions[key]
        if versions:
            latest = versions[-1]
            if latest.end_ts == 0:
                latest.end_ts = version.begin_ts

        versions.append(version)


    def get_visible(self, key: bytes, start_ts: int) -> Optional[Version]:
        """
        Find the version of key visible at start_ts.
        Visible means: begin_ts <= start_ts AND (end_ts == 0 OR end_ts > start_ts)
        Returns None if key has no visible version.
        """
        if key not in self.versions:
            return None

        #Walk backwards (newest first) to find the latest visible version


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


