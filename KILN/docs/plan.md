# Kiln — build plan

README = why. PRODUCT.md = frozen rules. This file = language and order of work.

## Why not Go

The first spec said Go. We do not know Go. **Python 3** is the engine language so we spend time on WAL, pages, MVCC, and recovery — not syntax.

Node is fine for the **console** later. It is a poor home for the engine: the event loop and extra buffering make `fdatasync` + child `_exit` + torn writes harder to reason about. Next.js is a UI framework, not a database.

## Stack (frozen)

| Artifact | Stack | Job |
|----------|--------|-----|
| Engine (`python -m kiln`) | Python 3.12+, **stdlib only** for on-disk data | WAL, heap, recovery, MVCC, TCP server |
| CLI (`python -m kiln.cli`) | Same | Type BEGIN / PUT / GET / COMMIT |
| Tests + chaos | `pytest`, spawn a **child process**, real `kiln-data/` | Kill mid-write, reopen, checker |
| Durability universe | **Linux in Docker** | `os.fdatasync` semantics |
| Console (phase 7 only) | Next.js + TypeScript | Two clients, crash buttons, checker panel. **Stores nothing.** |

**Forbidden as the store:** Postgres, MySQL, SQLite. SQLite may be a test oracle only.

Do not start Next until phases 0–2 survive process kill.

## Python notes that match the spec

- Persist with `os.open` / `os.write` / `os.fdatasync` (Linux) or `os.fsync`. Never treat `file.write` + close as durable.
- Crash injection: `os._exit(1)` in the child — not `sys.exit` (that can flush and run atexit).
- Concurrent clients: `threading` + **one commit lock** (v1: no parallel COMMITs).
- WAL bytes: `struct` little-endian; crc32 via `zlib.crc32`.
- One txn per TCP connection (PRODUCT §3.7).

## Module map

```text
kiln/
  wal.py          append, crc, LSN = byte offset, truncate torn tail, fdatasync
  heap.py         4KiB slotted pages, checksum, page LSN
  meta.py         checkpoint_lsn, next_txn_id, next_ts; heap fsync then meta
  buffer.py       small pool; never evict dirty; checkpoint when too dirty
  catalog.py      in-memory key → versions; rebuild on recovery
  txn.py          write set in RAM; WAL only at COMMIT
  mvcc.py         snapshot visibility; first-committer-wins at COMMIT
  recover.py      meta → checkpoint LSN → replay committed only → fail closed
  server.py       TCP text protocol
  fault.py        named crash points
  checker.py      history invariants 1–5
  cli.py
  __main__.py
tests/
docker/           Linux image that runs pytest chaos
console/          Next.js — phase 7 only
```

## Frozen behaviors (do not reinvent)

- PUT hits WAL **only at COMMIT** (private write set until then).
- No-steal, redo-only; uncommitted data never on heap or in a durable commit record.
- Checkpoint: commit lock → flush dirty pages → fdatasync heap → write meta → fdatasync meta.
- Isolation: snapshot; conflict at COMMIT not PUT; write skew allowed and named.
- Fail closed on bad checksum / unreadable meta.

## Phases (do not skip)

| Phase | Proof it is done |
|-------|------------------|
| **0** WAL | 1000 records, truncate last 7 bytes, reopen sees 999 |
| **1** Child `_exit` mid-append | Recovery never panics; no bogus record |
| **2** Heap + checksum + checkpoint order | Kill mid-page; recover from WAL; no silent junk |
| **3** Single txn + crash after ACK | Data comes back |
| **4** Two txns, SI, first-committer-wins | README balance / conflict examples |
| **5** Checker | Hand-written lost-update must FAIL if both commit |
| **6** Random + chaos | N runs green on Linux Docker |
| **7** CLI + Next console | 3-minute demo |

Phase 7 is polish. Phases 0–6 are Kiln.

## Out of v1

SQL, Raft, serializable/SSI, ARIES steal/undo, concurrent COMMITs, fancy buffer replacement, continuous vacuum, network partitions.
