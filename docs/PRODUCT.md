# Kiln — product and build contract

`README.md` is **why** the engine exists (the 23-section spine).

This file is **what we ship** and **what we freeze before writing code**. If a decision is not here, we would invent it mid-build. That is the gap.

Read this once. Then we design. Then we build.

---

## 1. What “the product” is

Kiln is **not** a SaaS. It is a **database you can hurt**, plus a **demo that makes the hurting visible**.

Three artifacts, one story:

| Artifact | Who uses it | Job |
|---|---|---|
| **Engine** (`kiln` server) | Clients / tests | Durable SI key-value |
| **CLI** (`kiln-cli`) | You, reviewers | Type `BEGIN` / `PUT` / `GET` / `COMMIT` |
| **Console** (thin web UI, last) | Interview demo | Two clients, Crash button, checker PASS/BUG |

The engine is the product. The console is a window. GridLocalizer energy: **inject failure, show the invariant**, not a pretty empty dashboard.

**Done** means a stranger can:

1. Start Kiln on an empty `kiln-data/` directory.
2. Run two clients against it.
3. Click / run **crash** during commit.
4. Restart from the same directory.
5. See committed data survive (or correctly vanish if fsync never happened).
6. See the **checker** say PASS or print a bad history.

If we only have a library with unit tests, we built homework. If we have a console before WAL survives `kill -9`, we built a costume.

---

## 2. What README already covers (do not rewrite)

You already understand and we already locked:

- Tiny API, no SQL
- Files: `wal.log`, `heap.db`, `meta`
- 4KiB pages, checksums, fail closed
- WAL before heap, fsync, then ACK
- Recovery from checkpoint LSN
- MVCC + snapshot isolation
- First-committer-wins on the same key
- Write skew is allowed; we do not say serializable
- Checker + chaos

**Do not make README longer for its own sake.** The missing pieces below are *product/build* holes, not more poetry about WAL.

---

## 3. Holes that would wreck a build if we skip them

These are the things README never decided.

### 3.1 When does a PUT hit the WAL?

**Freeze: only at COMMIT.**

While a txn is ACTIVE, puts live in a **private write set in RAM**. Other txns cannot see them. Crash before COMMIT → write set gone. That matches **no-steal**: uncommitted data never reaches `heap.db` *or* a durable WAL commit record.

At COMMIT (one txn at a time in v1):

1. Check first-committer-wins against committed versions.
2. If conflict → ABORT, no WAL commit record, client gets error.
3. If OK → append redo records for every PUT/DEL in the write set, then a **COMMIT** record, then `fdatasync(wal)`, then ACK OK, then install versions in memory, mark heap pages dirty.

We do **not** WAL every PUT as it happens. That would need undo and steal. Out of v1.

### 3.2 How do we find a key? (README never said)

Pages store records. GET still needs an index.

**Freeze: in-memory hash `key → []VersionRef`.**

- `VersionRef` = `{begin_ts, end_ts, page_id, slot}` or the value itself if we keep a small in-memory catalog.
- Rebuilt on recovery by replaying WAL after checkpoint (and reading heap for checkpointed versions).
- No B-tree in v1. No “scan every page on every GET” except as a debug assertion.

This is still a real engine: the **source of truth on disk** is WAL + pages. The hash is a cache of locations, like a buffer manager’s catalog. If we process-kill, the hash dies; recovery rebuilds it.

**GET path:**

1. If key is in this txn’s write set → that value (or not-found if we deleted it).
2. Else walk versions for `key` visible at `start_ts`.
3. Else not-found.

### 3.3 What does a heap page actually look like?

**Freeze: slotted page, 4096 bytes.**

```text
[ header: page_id | checksum | page_lsn | slot_count | free_start ]
[ slots growing down from the end ]
[ free space ]
[ records growing up from the header ]
```

A record is one **version** of one key (or a tombstone). If a PUT does not fit on the page that currently holds that key’s latest version, allocate a new page. v1 does not compact pages aggressively; GC of dead versions is **later** (checkpoint-time or a dedicated vacuum). Until then, old versions may accumulate. That is OK if we document it; it is not OK if the heap grows without bound in a 10k-op chaos run — so **vacuum at checkpoint**: drop versions whose `end_ts` is before the oldest active `start_ts` (and ≤ checkpoint).

### 3.4 Buffer pool

**Freeze: small in-memory pool of pages** (e.g. 64–256 pages).

- Pin page → read from `heap.db` if miss.
- Dirty pages stay in RAM until checkpoint (or until the pool must evict: **only evict clean pages** in v1, or refuse eviction of dirty — simpler: **never evict dirty; checkpoint when pool is too dirty**).
- Uncommitted data is **not** in heap pages (write set only).

### 3.5 Checkpoint protocol (order matters)

**Freeze this order.** Wrong order = lie after crash.

1. No txn may COMMIT mid-checkpoint (commit lock).
2. Flush all dirty pages to `heap.db`.
3. `fdatasync(heap.db)`.
4. Write `meta`: `{checkpoint_lsn, next_txn_id, next_ts, page_size}`.
5. `fdatasync(meta)` (or write `meta.tmp` + `fsync` + `rename` + `fsync` dir).
6. Optional v1.5: truncate WAL before `checkpoint_lsn`. v1 may leave WAL and just skip on replay.

If we crash between 3 and 5: old meta, replay extra WAL — **safe**.  
If we write meta before heap fsync: meta claims heap is current, heap is not — **unsafe**. Never do that.

### 3.6 WAL record layout (bytes)

**Freeze: little-endian, checksum over type+payload.**

```text
u32  payload_length
u32  crc32
u8   type
[payload]
```

Types:

| Type | Payload | When |
|---|---|---|
| `RedoPut` | `txn_id`, `commit_ts`, `key`, `value` | At COMMIT, per put |
| `RedoDel` | `txn_id`, `commit_ts`, `key` | At COMMIT, per del |
| `Commit` | `txn_id`, `commit_ts` | Last record of a successful commit |
| `Checkpoint` | `checkpoint_lsn` | Optional marker |

LSN = byte offset of the record in `wal.log` (or a monotone counter stored in the record). Pick **byte offset** — recovery is then “seek to checkpoint.”

Incomplete last record: length past EOF or crc mismatch → **truncate file to last good record**.

### 3.7 Client protocol (how humans talk to Kiln)

**Freeze: text protocol over TCP, one transaction per connection.**

```text
→ BEGIN
← OK t=17 start_ts=1024
→ PUT user:42 Ayush
← OK
→ GET user:42
← VALUE Ayush
→ GET missing
← NOTFOUND
→ COMMIT
← OK
```

Errors:

```text
← ERR conflict     # first-committer-wins
← ERR not_in_txn
← ERR txn_aborted
← ERR too_large
```

Same language as the README API. CLI is a thin wrapper around this.

HTTP JSON is allowed **later** for the console; the engine’s native surface is this TCP text protocol so tests do not need a browser.

**Limits (v1):**

| Limit | Value |
|---|---|
| Max key | 1 KiB |
| Max value | 64 KiB |
| Max write set per txn | 1024 ops |
| Page size | 4096 |
| One txn per connection | yes |

### 3.8 Isolation: when do we detect conflict?

**Freeze: at COMMIT, not at PUT.**

PUT into the write set always succeeds (unless limits). Two txns can both `PUT x` while ACTIVE. First to COMMIT wins; second COMMIT returns `ERR conflict`.

That matches “first committer wins” and is easy to check: for each key in the write set, if a version was committed with `begin_ts > start_ts`, abort.

### 3.9 Transaction states

```text
ACTIVE ──COMMIT──► COMMITTED
   │
   └──ABORT──► ABORTED
```

Also: engine crash ⇒ process gone ⇒ ACTIVE txns **do not exist** after recovery. No WAL commit ⇒ they never happened.

Client that had an ACTIVE txn and the server died: next command fails; they must BEGIN again.

### 3.10 Checker: what it actually checks (formal enough to code)

History events:

```text
Begin(t, start_ts)
Read(t, key, value | NOTFOUND)
Write(t, key, value)      # PUT
Delete(t, key)
Commit(t, commit_ts)      # only if client got OK
Abort(t)
Crash()
Recovered(key, value)*    # snapshot of committed state after recovery
```

**Must-hold (v1):**

1. **Durability.** If `Commit(t)` is in the history (OK returned), then after the next `Recovered`, every Write/Delete of `t` is reflected (unless a later committed txn overwrote that key).
2. **Atomicity.** No prefix of `t`’s writes appears committed without all of them (commit is one WAL group).
3. **Snapshot reads.** `Read(t, k, v)` equals the version visible at `start_ts` plus `t`’s own writes. Not a later txn’s commit.
4. **Lost update.** There do not exist two committed txns that both wrote `k` and whose snapshots both saw the same prior version (equivalently: second commit would have seen `begin_ts > start_ts` on `k`).
5. **No dirty read.** `v` was not written by an uncommitted/aborted txn other than `t`.

**Must-not-claim:** serializability / absence of write skew.

If a generated history violates 1–5, that is a **Kiln bug**, not “flaky test.”

### 3.11 Chaos: how we kill (product-level)

Not a button that `os.Exit` after a sleep. We need **fault points**:

- `before_wal_append`
- `during_wal_append` (write half a record, then `_exit`)
- `after_wal_sync_before_ack`
- `after_ack_before_heap`
- `during_heap_page_write` (write half a page, then `_exit`)

Tests spawn Kiln as a **child process**, set a fault, send COMMIT, wait for death, start a new process on the same `kiln-data/`, run checker.

In-process “simulate crash” is weaker (no real torn write). Child + `_exit` + real files is the demo that opens jaws.

### 3.12 Console (last, still specified so we know the destination)

One screen:

- Two client panels (A / B) with command log
- Buttons: Inject WAL-tear, Inject heap-tear, Recover
- Live: last LSN, checkpoint LSN, open txns
- Checker panel: last run PASS or the violating history pretty-printed

No Grafana. No auth. Hardcoded local server.

---

## 4. What we are explicitly not building (so scope stays honest)

- SQL, query planner, secondary indexes, range scans
- Replication, Raft, multi-disk, encryption
- SSI / serializable
- Steal + undo + ARIES
- Concurrent COMMITs (parallel group commit)
- Production-grade buffer replacement (CLOCK/LRU sophistication)
- Vacuum that runs continuously (checkpoint-time GC is enough)
- Network partitions (single process)

---

## 5. Build phases (do not skip)

| Phase | Proof it is done |
|---|---|
| **0** WAL file: append, crc, truncate torn tail, reopen | Test writes 1000 records, truncates last 7 bytes, reopen sees 999 |
| **1** Child process `_exit` mid-append | Recovery never panics; no bogus record |
| **2** Heap page + checksum + checkpoint order | Kill mid-page; recover from WAL; no silent junk |
| **3** Single txn PUT/GET/COMMIT/ABORT + crash after ACK | Data comes back |
| **4** Two txns, SI reads, first-committer-wins | The balance example from README §12–17 |
| **5** Checker on recorded histories | Hand-written lost-update must FAIL the checker if both commit |
| **6** Random + chaos loop | N runs green on Linux Docker |
| **7** CLI + console | Demo script of 3 minutes |

Phase 7 is product polish. Phases 0–6 are Kiln.

---

## 6. Interview / demo script (why this is a product)

1. Empty dir. `PUT x=1` COMMIT. Kill. Restart. `GET x` → `1`.
2. Two clients both read `balance=1000`, both PUT, first COMMIT wins, second `ERR conflict`.
3. Show write skew on `A`/`B` doctors — **and say it is allowed**.
4. Kill during WAL: value gone. Kill after fsync: value back.
5. Open checker: random run PASS (or a bug we found and a commit that fixed it).

---

## 7. Spine vs product (one paragraph)

README: commit is a fsynced WAL record; heap may lag; snapshots freeze time; same-key writers fight at commit; a checker falsifies us.

PRODUCT: humans speak a TCP text protocol; PUTs sit in RAM until commit; pages are slotted 4KiB; a hash finds keys; checkpoints fsync heap **then** meta; chaos is a real child `_exit`; the console is last.

That is complete enough to kick off **design** (TypeScript modules) without inventing durability rules on the fly.

---

## Status

README = teaching spec (done).  
PRODUCT = build contract (this file).  

**Not started:** WAL byte format code, tests, server.
