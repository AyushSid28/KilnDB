from heap import Page, HeapFile, PAGE_SIZE

#A class to manage a buffer pool of pages from a heap file
#Buffer pool is ram cache of kiln
class BufferPool:
    """
    A small in-memory cache of pages.

    Rules:
    -Pin a page-> read from heap.db if not in pool
    -Dirty pages stay in RAM until checkpoint
    -Never ever evict a dirty page.Only evict clean pages
    - If pool is too dirty, force a checkpoint
    """


#max_pages : maximum clean pages allowed in RAM before eviction
#heap_file : heap file object used to read/write disk pages
#pages : Dictionary acting as the RAM cache.
#next_page_id: ID to assign when creating a brand new page.
    def __init__(self, heap_file: HeapFile, max_pages: int = 128):
        self.heap_file = heap_file
        self.max_pages = max_pages
        #page_id -> Page Object
        self.pages: dict[int,Page]={}
        #Track the next page_id to allocate
        self.next_page_id = 0

    #TO fetch a page from the buffer of RAM if available and in case if page is not available in the RAM, then read it from the heap and add it to the buffer of RAM 
    def get_page(self,page_id: int)-> Page:
        """Fetch a page. Returns from cache if available,
        otherwise reads from disk."""

        if page_id in self.pages:
            return self.pages[page_id]

        #Need to load from disk - make room if necessary
        self._maybe_evict()

        page = self.heap_file.read_page(page_id)
        self.pages[page_id] = page
        return page


    #TO allocate a brand new empty page and add it to the buffer of RAM
    def new_page(self) -> Page :
        """Allocate a branc new empty page"""
        self._maybe_evict()

        page_id = self.next_page_id
        self.next_page_id += 1

        page = Page(page_id)
        self.pages[page_id] = page
        #we make the page dirty because it is new and has not been written to the disk yet
        page.dirty = True
        return page

   #Making a page dirty means that the page has been modified in memory and has not been written to the disk yet
   #So if the data is not modified on disk as compared to ram then we simply make that specific page dirty
    def make_dirty(self, page_id: int):
        """Make a page as dirty (modified in memory, not yet on disk)"""

        if page_id in self.pages:
            self.pages[page_id].dirty = True

    #TO evict a clean page from the buffer of RAM if the buffer is full and there is no dirty page in the buffer of RAM and if there is a dirty page in the buffer of RAM then we do not evict it
    #We do not evict dirty pages because they have been modified and we need to write them to the disk before we can evict them
    def _maybe_evict(self):
        """If pool is full, evict a clean page. If all pages are dirty, do nothing (checkpoint will handle it.)"""
        if len(self.pages)< self.max_pages:
            return

        #Try to find a clean page to evict
        for pid in list(self.pages.keys()):
            if not self.pages[pid].dirty:
                del self.pages[pid]
                return 

        #All pages are dirty - caller should checkpoint before continuing
        # For v1, we just allow the pool to grow slightly over limit rather than losing data.return

   #TO write all dirty pages to the heap.db
    def flush_all_dirty(self):
        """Write all dirty pages to  heap.db.Called during checkpoint"""

        for page in self.pages.values():
            if page.dirty:
                self.heap_file.write_page(page)
                #write_page sets page.dirty = False


   #To get the number of dirty pages in the buffer of RAM
   #This is used to check if the buffer of RAM is full and if it is full then we need to evict a clean page
    def get_dirty_count(self) -> int:
        return sum(1 for p in self.pages.values() if p.dirty)