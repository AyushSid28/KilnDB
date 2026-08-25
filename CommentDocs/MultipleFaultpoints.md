<!-- 
after_wal_append

after_wal_sync

before_page_write

after_page_write

before_commit_record

after_commit_record 
-->




<!-- Transaction starts

       |
       v

Write WAL record

       |
       X  <-- CRASH HERE

       |
       v

Sync WAL -->




<!-- 
Your Kiln crash lifecycle now looks like:

                Write good WAL
                     |
                     v
              WAL is durable


                Crash


                     |
                     v


             Restart database


                     |
                     v


              WAL recovery


                     |
                     v


          Continue with clean WAL
           -->