from heap import Page, HeapFile, PAGE_SIZE

class BufferPool:
    """
    A small in-memory cache of pages.

    Rules:
    -Pin a page-> read from heap.db if not in pool
    -Dirty pages stay in RAM until checkpoint
    -Never ever evict a dirty page.Only evict clean pages
    - If pool is too dirty, force a checkpoint
    """

    def __init__(self, heap_file: HeapFile, max_pages: int = 128):
        self.heap_file = heap_file
        self.max_pages = max_pages
        #page_id -> Page Object
        self.pages: dict[int,Page]={}
        #Track the next page_id to allocate
        self.next_page_id = 0
    
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

    def new_page(self) -> Page :
        """Allocate a branc new empty page"""
        self._maybe_evict()

        page_id = self.next_page_id
        self.next_page_id += 1

        page = Page(page_id)
        self.pages[page_id] = page
        page.dirty = True
        return page


    def make_dirty(self, page_id: int):
        """Make a page as dirty (modified in memory, not yet on disk)"""

        if page_id in self.pages:
            self.pages[page_id].dirty = True

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

        def flush_all_dirty(self):
            """Write all dirty pages to  heap.db.Called during checkpoint"""

            for page in self.pages.values():
                if page.dirty:
                    self.heap_file.write_page(page)
                    #write_page sets page.dirty = False

        def get_dirty_count(self) -> int:
            return sum(1 for p in self.pages.values() if p.dirty)

            