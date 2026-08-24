The whole story of this file

A transaction happens:

INSERT USER
     |
     v
append()
     |
     v
Create:

[length][crc][type][data]

     |
     v
Write to wal.log

     |
     v
sync()

     |
     v
COMMIT SUCCESS

Crash happens:

Restart Kiln

     |
     v

recover_and_truncate()

     |
     v

Remove incomplete records

     |
     v

read_all()

     |
     v

Replay valid changes