Client
 |
 v
WAL append
 |
 v
Heap page update
 |
 v
serialize page
 |
 v
write heap.db
 |
 v
sync




Crash ::::

heap corrupted

       |
       v

Restart

       |
       v

Read WAL

       |
       v

Redo missing changes

       |
       v

Correct heap