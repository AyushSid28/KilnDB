START DATABASE

      |
      v

Engine()

      |
      v

Recover WAL

      |
      v

READY


User:

begin()

      |
      v

PUT/GET/DELETE

      |
      v

commit()

      |
      v

WAL fsync

      |
      v

Catalog update

      |
      v

checkpoint()

      |
      v

Heap storage