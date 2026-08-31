                 User
                  |
                  v
              Engine
                  |
    ---------------------------------
    |        |        |             |
   WAL     Heap    Catalog       Txn
    |        |        |             |
Durability Disk   Versions   Isolation