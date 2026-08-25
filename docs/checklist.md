# Kiln — checklist

## Spec gates

- [x] Engine is **Python 3**, not Go (we don't know Go)
- [x] Next.js is **console-only** and **last**
- [ ] No foreign DB as the store
- [ ] Linux Docker is where fdatasync / crash tests run

## Phase 0 — WAL

- [x] Record: u32 length, u32 crc32, u8 type, payload (little-endian)
- [x] Types: RedoPut, RedoDel, Commit, Checkpoint
- [x] LSN = byte offset in `wal.log`
- [x] `fdatasync` after commit group; never ACK before fsync
- [x] Torn tail: bad length or crc → truncate to last good record
- [x] Test: 1000 records, chop last 7 bytes, reopen sees 999

## Phase 1 — Real crash on WAL

- [ ] Spawn kiln as child; `os._exit` during append
- [ ] Recovery: no crash loop, no invented records
- [ ] Incomplete txn is not committed

## Phase 2 — Heap + checkpoint

- [ ] 4096-byte slotted pages: id, checksum, page_lsn, slots
- [ ] Checksum mismatch → fail closed or rebuild from WAL; never serve junk
- [ ] Buffer pool; no dirty eviction (checkpoint if pool too dirty)
- [ ] Checkpoint order: heap fsync **then** meta fsync
- [ ] Kill mid-page write; recovery from WAL; no silent corruption

## Phase 3 — One transaction

- [ ] BEGIN / GET / PUT / DEL / COMMIT / ABORT
- [ ] Write set in RAM; WAL only at COMMIT
- [ ] GET missing → NOTFOUND; read-your-own-writes
- [ ] ABORT discards write set
- [ ] Crash after ACK → restart → committed keys present

## Phase 4 — MVCC / SI

- [ ] In-memory hash key → versions; rebuilt on recovery
- [ ] Visibility at start_ts + own writes
- [ ] DEL = tombstone; old snapshots can still see prior value
- [ ] First-committer-wins at COMMIT; loser `ERR conflict`
- [ ] Write skew demo exists and is named as allowed

## Phase 5 — Checker

- [ ] History: Begin, Read, Write, Delete, Commit (only if OK), Abort, Crash, Recovered
- [ ] Invariants: durability, atomicity, snapshot reads, lost-update, no dirty read
- [ ] Does not claim serializability
- [ ] Hand-written both-commit lost-update → checker FAIL

## Phase 6 — Chaos

- [ ] Faults: `before_wal_append`, `during_wal_append`, `after_wal_sync_before_ack`, `after_ack_before_heap`, `during_heap_page_write`
- [ ] Child process + real files (not in-process fake crash only)
- [ ] Random histories; Docker loop green

## Phase 7 — Product surface

- [ ] TCP text protocol (PRODUCT §3.7); one txn per connection
- [ ] CLI
- [ ] Next console: two clients, inject WAL-tear / heap-tear / Recover, LSN, checker panel
- [ ] Demo: durability, conflict, write skew, kill during WAL vs after fsync

## Limits (v1)

- [ ] Key ≤ 1KiB, value ≤ 64KiB, write set ≤ 1024, page 4096
