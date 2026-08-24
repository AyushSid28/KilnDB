# Kiln

A single-node **transactional key-value database** we build ourselves.

Not SQL. Not tables. Not joins. Not Postgres, MySQL, or SQLite as the store.

The API is tiny. The internals are the project.

This document is the spec. If code disagrees with it, the code is wrong.

---

## 1. What are we actually building?

We are building a **transactional key-value database**.

At the beginning, the database understands only:

```text
BEGIN
GET  key
PUT  key value
DEL  key
COMMIT
ABORT
```

For example:

```text
BEGIN
PUT user:42 "Ayush"
GET user:42
→ Ayush
COMMIT
```

The user sees something simple.

But underneath, Kiln has to solve a lot of problems:

```text
                    KILN
                      │
       ┌──────────────┼──────────────┐
       │              │              │
       ▼              ▼              ▼
    Storage       Transactions    Recovery
       │              │              │
       ▼              ▼              ▼
     Pages           MVCC          WAL
       │              │              │
       └──────────────┼──────────────┘
                      ▼
                 Correctness
                      │
                      ▼
                   Checker
```

**The API is tiny. The internals are the project.**

### API rules (locked)

- Keys and values are byte strings (UTF-8 in the console is fine).
- `GET` of a missing key returns **not found**, not an error.
- `DEL` is a versioned tombstone. Older snapshots may still see the previous value.
- Uncommitted writes are visible only to the transaction that made them (read-your-own-writes).
- `COMMIT` returns OK only after the commit is **durable** on disk (see §6).
- `ABORT` discards the private write set. Nothing from that txn becomes visible.



### What we are not building (v1)

- SQL, schemas, indexes (beyond finding a key), replication, Raft
- Postgres wire protocol
- Claiming **serializable** isolation (we implement **snapshot isolation** and name write skew)
- Steal/undo (ARIES). V1 is **no-steal, redo-only** (see §5 and Add-ons)

---



## 2. Why not just store a dictionary?

The first thing we could build is:

```go
map[string]string
```

So:

```text
data["name"] = "Ayush"
```

Easy.

But now imagine:

```text
data["balance"] = 1000
PUT balance 500
💥 kill -9
```

When the process dies:

```text
RAM → GONE
```

Your database has forgotten everything.

So we need **persistent storage**.

Even `write()` to a file is not enough: the OS may hold bytes in a buffer. A crash mid-page write can leave **garbage**, not “old value” or “new value.”

Kiln exists to make **commit** a promise that survives that.

---



## 3. Our first real layer: the disk

We create our own database directory:

```text
kiln-data/
    wal.log
    heap.db
    meta
```

These are just files. But Kiln gives them meaning.

### heap.db

This contains **database pages**.

```text
heap.db

┌─────────────────────────────┐
│ Page 0                      │
│                             │
│ records                     │
│ records                     │
│ records                     │
└─────────────────────────────┘

┌─────────────────────────────┐
│ Page 1                      │
│                             │
│ records                     │
│ records                     │
└─────────────────────────────┘
```

Each page is initially **4096 bytes**.

**Why fixed-size pages?**

Because databases don't generally think:

> "I'll modify this arbitrary JSON object."

They think:

> "I need to read/write page 137."

That gets us into real storage-engine territory.

Each page carries:

```text
┌──────────────────────────────┐
│ page ID                      │
│ checksum                     │
│ page LSN (last WAL applied)  │
│ data (records)               │
└──────────────────────────────┘
```

---



## 4. But writing directly to pages isn't safe

Suppose:

```text
x = 100
```

and we want:

```text
x = 200
```

We modify the page.

But halfway through the physical write:

```text
💥 POWER LOSS
```

Now the page might contain **garbage**.

So we introduce the **WAL**.

---



## 5. WAL — Write Ahead Log

**WAL** means:

> Before changing the actual database page, record what you're going to change in a log.

Suppose:

```text
PUT x 200
```

Kiln records something like:

```text
WAL:

Transaction 17
PUT x
new = 200
COMMIT T17
```

Then:

```text
fdatasync(wal)
```

Now the log is durable.

Only **later** do we update:

```text
heap.db
```

```text
                 PUT x=200
                     │
                     ▼
                  WAL
                     │
                  fsync
                     │
                     ▼
                ACK COMMIT
                     │
                     ▼
                heap.db later
```

This creates a fundamental rule:

> **The WAL is the durable record of what happened.**
> **The heap is the materialized database state.**



### V1: redo-only, no-steal

In v1 we use **no-steal**: uncommitted data never goes to `heap.db`.

WAL records are **redo** (what a committed txn installed). We do not need `old = 100` on every PUT unless we later add steal/undo.

Recovery replays **committed** transactions after the checkpoint. In-flight txns never touched the heap, so we do not undo from pages.

### Incomplete WAL tail

If the last record is torn (bad length or checksum): **discard the tail**. That transaction is not committed.

---



## 6. What does COMMIT actually mean?

This is one of the most important things in the project.

A beginner thinks:

```text
COMMIT
```

means:

> "I changed the variable."

**No.**

In Kiln, commit means:

> **The transaction's commit record has been durably written to the WAL.**

For example:

```text
BEGIN T42
PUT x=100
PUT y=200
COMMIT T42
```

We do:

```text
write WAL
      ↓
fdatasync(WAL)
      ↓
COMMIT is durable
      ↓
send OK to client
```

Only after durability do we tell the client:

```text
COMMIT → OK
```

That is a **promise**.

**Durable then ACK.** We never ACK before fsync. If the client never hears OK but the commit record is fsynced, recovery **may** still show the commit. The checker uses this rule.

---



## 7. Why is this promise important?

Because immediately after:

```text
COMMIT → OK
```

the worst thing imaginable can happen:

```text
💥 kill -9
```

Kiln disappears.

Then we restart it.

The heap might be incomplete:

```text
heap:
x = old value
```

But the WAL says:

```text
T42
PUT x=100
COMMIT
```

Recovery says:

> "T42 committed. I need to replay it."

So:

```text
WAL
 ↓
recovery
 ↓
heap
```

and eventually:

```text
GET x
→ 100
```

That's **durability**.

---



## 8. Recovery is a major part of Kiln

When Kiln starts:

```text
kiln
```

it doesn't blindly trust `heap.db`.

It does something like:

```text
read metadata
      ↓
find checkpoint
      ↓
read WAL
      ↓
validate records
      ↓
replay committed transactions
      ↓
reconstruct state
      ↓
start accepting requests
```

This is the **database recovery engine**.

Only **committed** transactions are replayed. Aborted or incomplete txns are not.

---



## 9. Why checkpoints?

Imagine the database has been running for a year.

Your WAL might contain:

```text
T1
T2
T3
...
T1,000,000
```

You don't want every startup to replay one million operations.

So periodically we create a **CHECKPOINT**.

Conceptually:

```text
WAL

T1 T2 T3 T4 T5 T6 T7 T8 T9
             ↑
        checkpoint
```

At the checkpoint, relevant data has been safely written to the heap.

Now recovery can start from **checkpoint LSN** instead of the WAL beginning.

### LSN — Log Sequence Number

Every WAL record gets an increasing position:

```text
LSN 1
LSN 2
LSN 3
LSN 4
...
```

Checkpoint might say:

```text
checkpoint_lsn = 8421
```

Recovery knows:

> "The heap is valid up to this point. Start replaying after 8421."

---



## 10. Checksums

Now suppose the disk itself gives us garbage.

Maybe **Page 17** was partially written.

We don't want Kiln to silently read:

```text
x = 827364827
```

and pretend everything is fine.

So pages have checksums.

Conceptually:

```text
┌──────────────────────────────┐
│ page ID                      │
│ checksum                     │
│ data                         │
└──────────────────────────────┘
```

When reading:

```text
checksum(data)
        ↓
compare stored checksum
```

If they differ:

```text
❌ CORRUPTION
```

Kiln doesn't say:

> "Let's just ignore that."

It says:

> "I don't trust this data."

Then recovery can attempt to reconstruct from WAL or **fail closed** (refuse to serve junk).





## First: What is a checksum?

A **checksum is a small fingerprint/hash generated from data**.

Its job is:

> "When I read this data later, how do I know it is exactly the same data I wrote earlier?"

Example:

Imagine your page contains:

```

```

```
Page 17:

x = 100
y = 200
z = 300
```

Before writing this page to disk, Kiln calculates:

```

```

```
checksum(page data)
```

Suppose the calculation gives:

```

```

```
84729384
```

Now Kiln stores BOTH:

```

```

```
Disk page:

+----------------+
| Page ID: 17    |
| Checksum:      |
| 84729384       |
|                |
| x = 100        |
| y = 200        |
| z = 300        |
+----------------+
```

The checksum is stored **inside the page metadata**.

---

## Now what happens later?

Suppose Kiln reads Page 17:

Disk gives:

```

```

```
Page ID: 17

Stored checksum:
84729384


Data:

x = 100
y = 200
z = 300
```

Kiln calculates again:

```

```

```
checksum(
 x = 100
 y = 200
 z = 300
)
```

It gets:

```

```

```
84729384
```

Compare:

```

```

```
Stored checksum:    84729384
Calculated checksum:84729384

MATCH ✅
```

Kiln says:

> "This page is exactly what I wrote. I trust it."

---

## But what if disk corruption happens?

Suppose the disk has a bad write.

Original:

```

```

```
x = 100
```

After corruption:

```

```

```
x = 827364827
```

Now the page is:

```

```

```
Stored checksum:
84729384


Data:

x = 827364827
y = 200
z = 300
```

Kiln recalculates:

```

```

```
checksum(data)
```

Now result:

```

```

```
93847291
```

Compare:

```

```

```
Stored:     84729384
Calculated:93847291

Mismatch ❌
```

Kiln knows:

> "The data changed after I wrote it."

---

## Why do we need this?

Because storage is not magical.

Things can happen:

-  power failure during write 
-  SSD/HDD bad sector 
-  partial page write 
-  hardware failure 

Example:

Kiln wants to write:

```

```

```
Old page:

x = 50
```

to:

```

```

```
New page:

x = 100
```

A page is maybe 16KB.

The disk starts writing:

```

```

```
16KB page:

First 8KB written ✅
Second 8KB not written ❌
```

Now disk contains garbage:

```

```

```
x = 100
other data = old/random
```

Without checksum:

```

```

```
Database reads it:
"Oh okay x=100"
```

Wrong.

With checksum:

```

```

```
Checksum mismatch ❌

Do not trust this page.
```

---



---



## 11. Up to here, we have a storage engine

At this point:

```text
PUT
 ↓
WAL
 ↓
fsync
 ↓
heap
 ↓
checkpoint
 ↓
recovery
```

We've solved:

> **"How do I keep data alive after a crash?"**

But we haven't solved:

> **"What happens when TWO clients use the database simultaneously?"**

And that's where the project gets much more interesting.

---



## 12. Two clients

Imagine:

```text
Client A                 Client B

BEGIN                    BEGIN

GET balance              GET balance
→ 1000                   → 1000
```

Both saw **1000**.

Now:

```text
A: PUT balance 300
B: PUT balance 500
```

What happens?

If we simply do **last write wins**, we might end up with:

```text
500
```

even though both transactions were based on **1000**.

That's a **concurrency problem**.

---



# 13.Why do we need transactions?

Imagine two users accessing the database:

```

```

```
Client A                 Client B

Read balance = 100       Read balance = 100

Withdraw 50              Withdraw 70

Write 50                 Write 30
```

Both read the same old value.

Final balance becomes:

```

```

```
30
```

instead of:

```

```

```
100 - 50 - 70 = -20
```

This is a concurrency problem.

Transactions solve this by giving each operation an isolated workspace.

---

# What is a transaction?

A transaction is a **logical unit of work**.

Example:

Bank transfer:

```

```

```
BEGIN TRANSACTION

Debit Account A
Credit Account B

COMMIT
```

Either:

```

```

```
Both operations happen
```

or:

```

```

```
Nothing happens
```

This gives us atomicity.

---

# Transaction Object

Each client gets a transaction object:

```

```

```
Client
  |
  |
 Transaction
  |
  +-- ID
  +-- timestamp
  +-- state
  +-- writes
  +-- snapshot
```

Example:

```

```

```
T17
```

means transaction number 17.

---

# Transaction ID

```

```

```
T1
T2
T3
...
```

Every transaction gets a unique identifier.

Purpose:

-  tracking active transactions 
-  debugging 
-  locking/version visibility 

Example:

```

```

```
Transaction Table

ID     State
---------------
T1     COMMITTED
T2     ACTIVE
T3     ABORTED
```

---

# Transaction State

A transaction moves through states:

```

```

```
          BEGIN
            |
            v
        ACTIVE
            |
     +------+------+
     |             |
 COMMIT          ERROR
     |             |
     v             v
 COMMITTED      ABORTED
```

---

## ACTIVE

Transaction is currently running.

Example:

```

```

```
T17

state = ACTIVE
```

It can:

-  read data 
-  write data 
-  perform calculations 

---

## COMMITTED

Transaction successfully finished.

Example:

```

```

```
T17

state = COMMITTED
```

Its changes become visible to other transactions.

---

## ABORTED

Transaction failed.

Reasons:

-  conflict 
-  crash 
-  validation failure 

Its changes are discarded.

---

# Private Write Buffer

This is a very important concept.

A transaction does NOT immediately modify the database.

Example:

```

```

```
Database:

x = 100
y = 300
```

Transaction:

```

```

```
T17

writes:

x -> 200
y -> 500
```

These changes are stored separately:

```

```

```
T17 private memory

+---------+
| x  200 |
| y  500 |
+---------+
```

The real database still has:

```

```

```
x = 100
y = 300
```

---

Why?

Because the transaction may fail.

Example:

```

```

```
T17

x = 200
y = 500

ERROR OCCURS

ABORT
```

If we had directly modified storage:

```

```

```
x = 200
y = 500
```

we would need to undo everything.

The private buffer makes rollback easy:

```

```

```
Discard buffer
```

---

# Snapshot

Now comes MVCC.

(Multi-Version Concurrency Control)

Instead of storing one value:

```

```

```
x = 100
```

we store versions:

```

```

```
x:

Version 1
value = 50
commit_ts = 10


Version 2
value = 100
commit_ts = 20
```

---

A transaction does not see "current data".

It sees a snapshot of the database.

Example:

Database:

```

```

```
x = 100

commit_ts = 50
```

Transaction:

```

```

```
T17 begins

start_ts = 40
```

It cannot see:

```

```

```
commit_ts  > 40
```

because that happened after it started.

---

# Timestamp System

Now the important rules.

---

# start_ts

Allocated at BEGIN.

Example:

Current database:

```

```

```
Last committed timestamp = 1024
```

Transaction starts:

```

```

```
BEGIN T17
```

Assign:

```

```

```
start_ts = 1024
```

Meaning:

> "T17 sees the database as it existed at timestamp 1024."

---

# commit_ts

Allocated at COMMIT.

Important:

**Commit timestamp does not exist when transaction starts.**

Example:

```

```

```
T17 starts

start_ts = 1024


does work...


COMMIT
```

Now database gives:

```

```

```
commit_ts = 1050
```

The new version gets:

```

```

```
begin_ts = 1050
```

because this version became visible at commit time.

---

# Version Visibility Rule

A transaction can see only versions committed before it started.

Example:

Database:

```

```

```
x versions:

x=100
begin_ts=900


x=200
begin_ts=1100
```

Transaction:

```

```

```
start_ts = 1000
```

Question:

Can it see x=200?

No.

Why?

Because:

```

```

```
1100 > 1000
```

The commit happened after the transaction started.

So it sees:

```

```

```
x=100
```

---

# Timeline Example

```

```

```
Time ----------------------------->


Commit x=100
timestamp=100


          T1 BEGIN
          start_ts=100


                  T2 updates x

                  COMMIT
                  timestamp=200


          T1 reads x
```

T1 sees:

```

```

```
x=100
```

not:

```

```

```
x=200
```

because T2 committed after T1 started.

---

# Why serialize commits in v1?

The design says:

> commit is serialized (one commit at a time)

Meaning:

Only one transaction can finalize changes at a time.

Example:

```

```

```
T1 COMMIT
 |
 v
T2 COMMIT
 |
 v
T3 COMMIT
```

Not:

```

```

```
T1 COMMIT
T2 COMMIT
T3 COMMIT
```

simultaneously.

---

Why do this?

Because commit is the dangerous part.

During commit we need to:

1.  validate conflicts 
2.  assign timestamp 
3.  create new versions 
4.  update indexes 
5.  flush WAL/storage 

If many transactions commit together:

```

```

```
T1       T2       T3

write   write   write

??? timestamp order
??? conflicts
??? visibility
```

It becomes complicated.

---

# But reads are still concurrent

Important:

Only commits are serialized.

Reads can happen together.

Example:

```

```

```
Client A                 Client B

GET x                    GET y

(snapshot 100)           (snapshot 100)
```

Both can read without blocking.

---



## 14. Now MVCC

**MVCC** = Multi-Version Concurrency Control.

Instead of storing only:

```text
x = 100
```

we store **versions**:

```text
x

Version 1
value = 100
begin = 0
end = 101

Version 2
value = 200
begin = 101
end = ∞
```

So the database remembers **history**.

---



## 15. Why multiple versions?

Imagine T1 started when:

```text
x = 100
```

Then T2 changes:

```text
x = 200
```

T1 should not suddenly see:

```text
100 → 200
```

halfway through its transaction.

T1's snapshot should remain consistent.

So:

```text
T1 starts
snapshot = 100
```

Even after T2 commits:

```text
T1 GET x → 100
```

while a new transaction:

```text
T3 GET x → 200
```

That's the core idea behind **snapshot isolation**.

---



## 16. Visibility

Every version has:

```text
begin_ts
end_ts
```

Suppose:

```text
version:
begin = 100
end = 150
```

Transaction snapshot:

```text
start_ts = 120
```

Check:

```text
100 <= 120 < 150   → True
```

So the transaction can see it.

Another transaction:

```text
start_ts = 160
```

checks:

```text
100 <= 160 < 150   → False
```

So it doesn't see that version.

That's how Kiln determines what a transaction is allowed to see.

Plus **read-your-own-writes**: uncommitted puts in the current txn are visible to that txn only.







Because the rule is **the opposite**: a transaction can see versions that were committed **before its own start timestamp**.

So:

- Version: `begin_ts = 100` means it became visible at time 100.
- Transaction starts at `start_ts = 120`.

Since:

```

```

```
100 <= 120
```

the version already existed when the transaction started, so it can see it.

A newer transaction starting at `160` does not see it only because the version's visibility range ended:

```

```

```
100 <= 160 < 150  → false
```

meaning that version was replaced/expired before this transaction began.

---



## 17. Now concurrent writes

Suppose:

```text
x = 100
```

T1:

```text
start = 100
PUT x=200
```

T2:

```text
start = 100
PUT x=300
```

Both started from the same snapshot.

Under our SI implementation: **first committer wins**.

So:

```text
T1 COMMIT ✓
T2 COMMIT ✗
```

because both wrote the same key.

Final:

```text
x = 200
```

This prevents a classic **lost-update** situation.

---



## 18. But we deliberately don't claim serializability

This is important.

Snapshot isolation has anomalies.

For example — **write skew**:

```text
Doctors:
A = ON
B = ON
```

T1:

```text
reads A
reads B
turns A OFF
```

T2:

```text
reads A
reads B
turns B OFF
```

They're modifying **different keys**. So both can commit.

Result:

```text
A = OFF
B = OFF
```

That's **write skew**.

The system is still operating according to Snapshot Isolation.

We are **not** going to lie and say:

> "Kiln is fully serializable."

That's an important engineering decision.





This is explaining a **limitation of Snapshot Isolation (SI)**.

The problem is: **SI prevents two transactions from overwriting the same data, but it does not prevent logical conflicts between different data items.**

Example:

Initial state:

```

```

```
Doctors:

A = ON
B = ON
```

Rule:

> At least one doctor must always be ON.

---

### Transaction T1:

```

```

```
Reads:
A = ON
B = ON

Decides:
"I will turn A OFF"

Writes:
A = OFF
```

---

### Transaction T2 (at the same time):

```

```

```
Reads:
A = ON
B = ON

Decides:
"I will turn B OFF"

Writes:
B = OFF
```

---

Now the database sees:

```

```

```
T1 changed A
T2 changed B
```

Different keys.

So SI's conflict detection says:

```

```

```
A conflict? No
Same key written? No

Allow both commits
```

Final state:

```

```

```
A = OFF
B = OFF
```

But the business rule is broken:

```

```

```
No doctor is ON
```

This is called **write skew**.

---

### Why Kiln doesn't claim serializability?

A serializable database would behave as if transactions ran one after another:

```

```

```
T1 finishes completely
       |
       v
T2 starts and sees T1's changes
```

Then T2 would notice:

```

```

```
A = OFF
B = ON
```

and would not turn B off.

But Snapshot Isolation allows both transactions to use the same old snapshot.

So Kiln says:

> "We provide Snapshot Isolation, not full Serializable Isolation."

Meaning:

-  Same-row conflicts → prevented ✅ 
-  Different-row logical conflicts → possible ❌ 

This is the honest engineering tradeoff.







is it good if we break business rule just to avoid serialisability

No, **breaking business rules is not "good"**. The point is not that Snapshot Isolation is better because it breaks rules. The point is that **serializability has a cost**, and many real systems choose a tradeoff.

Think of it like this:

### Serializable isolation

Guarantees:

> "The database behaves exactly as if transactions happened one by one."

Pros:

- Business rules are safer.
- No write skew.
- Strong correctness.

Cons:

- More locking/conflicts.
- Lower concurrency.
- More complex implementation.
- More transactions may abort under heavy load.

---

### Snapshot Isolation (what Kiln implements)

Guarantees:

> "Transactions read a consistent snapshot and conflicting writes are prevented."

Pros:

- Much higher concurrency.
- Reads don't block writes.
- Easier to scale.
- Better performance.

Cons:

- Some rare logical anomalies like write skew can happen.

---

So the engineering decision is:

- **Bank account balance, medical records, inventory count** → serializable may be worth it.
- **Social media likes, analytics counters, logs, feeds** → Snapshot Isolation is often acceptable.



---



##  This section is about building a **verification system** for Kiln. The checker is basically a **database correctness tester**.

Without the checker, you only have:

- WAL → durability
- MVCC → concurrency control

But you don't know whether your implementation is actually correct.

---

The checker records **everything that happens**:

Example:

```

```

```
T1 BEGIN
T1 READ x → 10

T2 BEGIN
T2 READ x → 10

T1 WRITE x → 20
T2 WRITE x → 30

T1 COMMIT
T2 ABORT
```

This sequence of events is called a **history**.

The checker takes this history and asks:

-  Did T1 really see the correct snapshot? 
-  Did T2 correctly abort because of write conflict? 
-  Were committed values visible to later transactions? 
-  Did recovery after crash produce the correct state? 

Basically:

> "Given this sequence of operations, did Kiln behave according to its transaction rules?"

---

Why use a separate history log instead of WAL?

WAL is optimized for recovery:

```

```

```
Page 10 changed
LSN 500
Redo this update
```

It is low-level storage information.

A history log is human-readable:

```

```

```
T1 READ x=10
T1 WRITE x=20
T1 COMMIT
```

Much easier for the checker to analyze.

---

Crashes are also events:

Example:

```

```

```
T1 WRITE x=50

CRASH

RECOVER

T1 COMMIT?
```

The checker verifies:

-  Did uncommitted writes disappear? 
-  Did committed writes survive? 

---



---



## 20. Checker reads the history

It doesn't care how your engine internally implemented things.

It sees: **what happened?**

Then it asks: **was this history legal?**

For example:

```text
T1 writes x
T2 writes x
both started concurrently
T1 commits
T2 commits
```

Checker says:

```text
❌ VIOLATION

Under Kiln's SI rule,
conflicting concurrent writers
cannot both commit.
```

That's extremely powerful.

The checker validates **durability** too: if `COMMIT → OK` was returned, after crash+recovery the committed writes must exist.

---



## 21. Random histories

Instead of manually writing:

```text
T1...
T2...
```

we **generate** them.

Something like:

```text
100 clients
10,000 operations
random reads
random writes
random transaction timing
random aborts
random crashes
```

And then:

```text
history
   ↓
Kiln
   ↓
crash
   ↓
recovery
   ↓
checker
```

Repeat this thousands of times.

---



## 22. Chaos engine — deliberately attack Kiln


| Attack                                  | Expected                                                                       |
| --------------------------------------- | ------------------------------------------------------------------------------ |
| Kill during WAL write                   | Incomplete WAL tail → discard tail → txn not committed                         |
| Kill after WAL fsync, before heap write | WAL contains durable commit → recovery replays → data survives                 |
| Kill during heap write                  | Heap may be incomplete → WAL is authoritative → recover → no silent corruption |
| Two concurrent writers, same key        | One commits, one aborts                                                        |
| Crash every 50ms for 30 seconds         | Restart loop → checker on the log                                              |


---



## 23. The whole system

```text
                         ┌─────────────┐
                         │   Client A  │
                         └──────┬──────┘
                                │
                         ┌──────▼──────┐
                         │   Client B  │
                         └──────┬──────┘
                                │
                                ▼
                    ┌─────────────────────┐
                    │    KILN ENGINE      │
                    │                     │
                    │ Transaction Manager │
                    │ MVCC                │
                    │ Visibility          │
                    │ Conflict Detection  │
                    └──────────┬──────────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
               ┌─────────┐          ┌──────────┐
               │   WAL   │          │  Pages   │
               │ wal.log │          │ heap.db  │
               └────┬────┘          └────┬─────┘
                    │                    │
                    └────────┬───────────┘
                             ▼
                           Disk
                             │
                         💥 CRASH
                             │
                             ▼
                        ┌──────────┐
                        │ Recovery │
                        └────┬─────┘
                             │
                             ▼
                         Kiln state
                             │
                             ▼
                        ┌──────────┐
                        │ Checker  │
                        └────┬─────┘
                             │
                      ┌──────┴──────┐
                      ▼             ▼
                    PASS           BUG
```

**That is the project.**

---



## Engineering add-ons (locked)

These extend the walkthrough above; they are not optional.

1. **No foreign engine as the store.** Postgres/MySQL/SQLite never persist Kiln data. SQLite may be a **test oracle** only.
2. **Fail closed.** Checksum fail, unreadable meta, unknown LSN → do not serve a maybe-correct heap.
3. **Linux in Docker** is the durability test universe for `fdatasync` semantics.
4. **Build order:** (1) WAL + checksum + kill-while-append → (2) pages + checkpoint + kill-while-heap → (3) MVCC + first-committer-wins → (4) checker → (5) chaos loop → (6) thin console. No UI before (1)–(2) survive `kill -9`.
5. **Engine in Node + TypeScript.** One process, event loop (v1: serialize COMMITs). Durability path is **sync** `writeSync` + `fsync`/`fdatasync`; crash injection is `SIGKILL` / `process.kill`, never `process.exit`. Console later (Next.js). Not Go.
6. **Single node.** One process, one `kiln-data/`.

---



## Spine (one glance)

```text
COMMIT OK  ⇒  commit record fsynced on WAL
heap       ⇒  may lag; never uncommitted (v1 no-steal)
crash      ⇒  replay committed WAL after checkpoint
GET        ⇒  snapshot + own writes
same-key concurrent writers ⇒ one wins, one aborts
write skew ⇒ allowed; we say snapshot isolation
checker    ⇒  history is legal or we have a bug
corrupt    ⇒  fail closed
```

---



## Status

Spec locked. Implementation not started.

First code: WAL records + checksum + torn-tail recovery + `kill -9` during append.