from typing import Optional,List

class Event:
    """Base class for history events."""
    pass

class Begin(Event):
    def __init__(self, txn_id: int, start_ts: int):
        self.txn_id = txn_id
        self.start_ts = start_ts

    def __repr__(self):
        return f"Begin(t={self.txn_id}, start_ts={self.start_ts})"


class Read(Event):
    def __init__(self, txn_id: int, key: bytes, value: Optional[bytes]):
        self.txn_id = txn_id
        self.key = key
        self.value = value #None=NOTFOUND
    
    def __repr__(self):
        return f"Read(t={self.txn_id}, {self.key!r})={self.value!r}"



class Write(Event):
    def __init__(self, txn_id: int, key: bytes, value: bytes):
        self.txn_id = txn_id
        self.key = key
        self.value = value
    
    def __repr__(self):
        return f"Write(t={self.txn_id}, {self.key!r}={self.value!r})"


class Delete(Event):
    def __init__(self, txn_id: int, key: bytes):
        self.txn_id = txn_id
        self.key = key

    def __repr__(self):
        return f"Delete(t={self.txn_id}, {self.key!r})"


class Commit(Event):
    """Only recorded if the client actually got OK back"""

    def __init__(self, txn_id: int, commit_ts: int):
        self.txn_id = txn_id
        self.commit_ts = commit_ts

    def __repr__(self):
        return f"Commit(t={self.txn_id}, commit_ts={self.commit_ts})"

class Abort(Event):
    def __init__(self, txn_id: int):
        self.txn_id = txn_id
    def __repr__(self):
        return f"Abort(t={self.txn_id})"

class Crash(Event):
    def __repr__(self):
        return "Crash()"

class Recovered(Event):
    """Snapshot of one key's committed state after recovery"""

    def __init__(self, key: bytes, value: Optional[bytes]):
        self.key = key
        self.value = value

    def __repr__(self):
        return f"Recovered({self.key!r}={self.value!r})"


#Violation

class Violation:
    def __init__(self, invariant: str, message: str):
        self.invariant = invariant
        self.message = message

    def __repr__(self):
        return f"BUG[{self.invariant}]: {self.message}"

#CHECKER

class Checker:
    """
    Validates a recorded history against kiln's snapshot isolation invariants.

    Checks 
    1.Durability - committed data survives crash
    2.Atomicity - no partial txn appears after crash
    3. Snapshot reads - reads match snapshot at start_ts + own writes
    4. Lost Update - two committed txns cant both write same key
    5. No dirty read - never read uncommitte/aborted txn's data

    Does NOT claim serializability. Write skew is allowed
    """

    def __init__(self, history: List[Event]):
        self.history = history

    def check(self) -> List[Violation]:
        """Run all 5 invariant checks.Empty list = PASS"""
        violations = []
        violations.extend(self._check_durability())
        violations.extend(self._check_atomicity())
        violations.extend(self._check_snapshot_reads())
        violations.extend(self._check_lost_update()) 
        violations.extend(self._check_no_dirty_read())
        return violations

    #Helpers

    def _find_crash_index(self) -> Optional[int]:
        """Find the last crash event index, or None"""
        for i in range(len(self.history) -1, -1, -1):
            if isinstance(self.history[i], Crash):
                return i

        return None


    def _build_committed_state(self, up_to_index: int) -> dict:
        """
        Build expected DB state from comitted txns up to an index.
        Returns {key: value} Value of None = key was deleted
        """
        committed = {} #txn_id -> commit_ts
        writes = {} #txn_id -> {key: value}

        for e in self.history[:up_to_index]:
            if isinstance(e, Commit):
                committed[e.txn_id] = e.commit_ts
            elif isinstance(e, Write):
                writes.setdefault(e.txn_id, {})[e.key] = e.value
            elif isinstance(e, Delete):
                writes.setdefault(e.txn_id, {})[e.key] = None

        #Apply in commit_ts order ( earlier commits first, later overwrites)
        state = {}
        for tid in sorted(committed, key=lambda t: committed[t]):
            if tid in writes:
                for k, v in writes[tid].items():
                    state[k] = v
        return state

    def _get_recovered_state(self, crash_idx: int) -> dict:
        state = {}
        for e in self.history[crash_idx:]:
            if isinstance(e, Recovered):
                state[e.key] = e.value
        return state


    #Invariant 1: Durability

    def _check_durability(self) -> List[Violation]:
        """
        If commit(t) returned OK, then after crash + recovery.
        every Write/Delete of t must be reflected 
        (unless a later committed txn overwrote that key)
        """

        violations = []
        crash_idx = self._find_crash_index()
        if crash_idx is None:
            return violations

        expected = self._build_committed_state(crash_idx)
        recovered = self._get_recovered_state(crash_idx)

        for key, exp_val in expected.items():
            rec_val = recovered.get(key)
            if exp_val is None:
                #key should be deleted
                if key in recovered and recovered[key] is not None:
                    violations.append(Violation("durability",
                      f"key {key!r} was declared by committed txn"

                      f"but recoverd as {rec_val!r}"))

            else:
                if rec_val != exp_val:
                    violations.append(Violation("durability",
                    f"Key {key!r}: expected {exp_val!r}"
                    f"(committed), recovered {rec_val!r}"))
            
        return violations

    #Invariant 2:Atomicity

    def _check_atomicity(self) -> List[Violation]:
       """No partial txn after crash. If a txn never committed,
       none of its writes should appear in recovered state
       """
       violations = []
       crash_idx = self._find_crash_index()
       if crash_idx is None:
         return violations


       begun = set()
       committed_ids = set()
       writes = {} 

       for e in self.history[:crash_idx]:
          if isinstance(e, Begin):
            begun.add(e.txn_id)

          elif isinstance(e, Write):
            writes.setdefault(e.txn_id, {})[e.key] = e.value

          elif isinstance(e, Delete):
            writes.setdefault(e.txn_id, {})[e.key] = None

       uncommitted = begun - committed_ids
       committed_state = self._build_committed_state(crash_idx)
       recovered = self._get_recovered_state(crash_idx)

       for tid in uncommitted:
          if tid not in writes:
                continue
          for key, value in writes[tid].items():
                rec_val = recovered.get(key)
                if value is not None and rec_val == value:
                    if committed_state.get(key)!= value:
                      violations.append(Violation("atomicity",
                      f"Uncommitted txn {tid} wrote"
                      f"{key!r}={value!r} and it appeared"
                      f"in recovered state"))

       return violations



    #Invariant 3: Snapshot reads


    def _check_snapshot_reads(self) -> List[Violation]:
       """
       Read(t,k,v) must equal the version visible at start_ts plus t's own writes.
       Visibility rule: a version is visible if its commit_ts < start_ts
       """

       violations = []

       #collect committed txn info
       txn_start = {}
       txn_commit = {}
       all_writes = {} # txn_id -> [(key, value|None)]

       for e in self.history:
          if isinstance(e,Begin):
              txn_start[e.txn_id] = e.start_ts

          elif isinstance(e, Commit):
              txn_commit[e.txn_id] = e.commit_ts
        
          elif isinstance(e, Write):
              all_writes.setdefault(e.txn_id, []).append((e.key, e.value))

          elif isinstance(e, Delete):
              all_writes.setdefault(e.txn_id, []).append((e.key, None))


        
        #Build committed version per key: [(commit_ts,value)] sorted

          committed_versions = {}
          for tid, cts in txn_commit.items():
            if tid in all_writes:
                for key, value in all_writes[tid]:
                    committed_versions.setdefault(key,[]).append((cts, value))

          for key in committed_versions:
            committed_versions[key].sort()

        #
          own_writes = {}

          for e in self.history:
            if isinstance(e, Begin):
                own_writes[e.txn_id] = {}

            elif isinstance(e, Write):
                own_writes.setdefault(e.txn_id, {})[e.key] = e.value

            elif isinstance(e, Delete):
                own_writes.setdefault(e.txn_id, {})[e.key] = None

            elif isinstance(e, Read):
                if e.txn_id not in txn_start:
                    continue

                start_ts = txn_start[e.txn_id]
                ow = own_writes.get(e.txn_id, {})

                #Expected: own writes first, then latest visible committed version

                if e.key in ow:
                    expected = ow[e.key]

                else:
                    expected = None

                    #Walk backwards through committed versions
                    for cts, val in reversed(committed_versions.get(e.key, [])):
                        if cts < start_ts:
                            expected = val
                            break


                if expected != e.value:
                    violations.append(Violation("snapshot read",
                    f"Txn {e.txn_id} read {e.key!r}= {e.value!r}"

                    f"but expected {expected!r}"
                    f"(start_ts={start_ts})"))

       return violations


    #Invariant 4: Lost Update

    def _check_lost_update(self) -> List[Violation]:
        """
        Two committed txns must not both write key k from the same 
        prior snapshot. If txn B committed after txnA on the same key,
        then A's commit_ts must be < B's start_ts.
        """
                
        violations = []

        txn_start = {}
        txn_commit = {}
        txn_write_keys= {}  #txn_id -> set of keys written

        for e in self.history:
            if isinstance(e, Begin):
                txn_start[e.txn_id] = e.start_ts

            elif isinstance(e, Commit):
                txn_commit[e.txn_id] = e.commit_ts

            elif isinstance(e, (Write, Delete)):
                txn_write_keys.setdefault(e.txn_id, set()).add(e.key)

        #For each key, collect all committed writers 
        key_writers = {} #key -> [(txn_id,start_ts,commit_ts)]
        for tid in txn_commit:
            if tid in txn_write_keys:
                for key in txn_write_keys[tid]:
                    key_writers.setdefault(key, []).append(
                        (tid, txn_start[tid], txn_commit[tid])
                    )

        #Check consecutive pairs in commit order
        for key, writers in key_writers.items():
            writers.sort(key= lambda w: w[2]) #sort by commit_ts
            for i in range(1, len(writers)):
                later_tid, later_start, _ = writers[i]
                earlier_tid, _, earlier_commit = writers[i-1]

                #later txn must have started AFTER the earlier one commited
                if earlier_commit >= later_start:
                    violations.append(Violation("lost_update",
                     f"Key {key!r}: txn {earlier_tid}"
                     f"(commit_ts = {earlier_commit}) and"
                     f"txn {later_tid} (start_ts={later_start})"


                     f"both committed writes from the same snapshot"))


        return violations

    #Invariant 5:No dirty read


    def _check_no_dirty_read(self) -> List[Violation]:
        """
        A read's value must not come from an uncommitted/aborted txn
        other than the reading txn itself.
        
        """

        violations = []

        txn_committed = set()
        value_writers = {}

        for e in self.history:
            if isinstance(e, Commit):
                txn_committed.add(e.txn_id)

            elif isinstance(e, Write):
                value_writers.setdefault(
                    (e.key, e.value), set()).add(e.txn_id)

        for e in self.history:
            if isinstance(e, Read) and e.value is not None:
                sources = value_writers.get((e.key, e.value), set())

                if not sources:
                    continue #value could be from initial state or not tracked


                has_valid_source = False
                for src in sources:
                    if src == e.txn_id:
                        has_valid_source = True
                        break

                    if src in txn_committed:
                        has_valid_source = True
                        break

                if not has_valid_source:
                    violations.append(Violation("no_dirty_read",
                     f"Txn {e.txn_id} read {e.key!r}={e.value!r}"
                     
                     f"which was only written by"
                     f"uncommitted txns: {sources}"))

        return violations