# Kiln DB 🔥

**Live Demo:** [https://kilndb.onrender.com](https://kilndb.onrender.com)

Kiln DB is a crash-safe, MVCC storage engine built completely from scratch in Python. It's designed to be a learning project that dives deep into how real database engines handle concurrency, durability, and recovery under the hood.

We've built an interactive Streamlit UI so you don't even have to clone the repo to see how it works. You can just click the live demo link above to open the transaction console, step through concurrent scenarios, and even inject a simulated crash to see how the engine recovers!

## What makes it tick?
- **Write-Ahead Logging (WAL):** Ensures that no matter when a crash happens—even mid-write—the engine can perfectly recover committed data using a rigid crash recovery loop.
- **Multi-Version Concurrency Control (MVCC):** Readers and writers don't block each other. Every transaction gets a frozen snapshot of the database at its `start_ts`.
- **Snapshot Isolation:** A strict "first-committer-wins" rule prevents lost updates, and we actively allow (and demonstrate!) write skew anomalies.
- **In-Memory Buffer Pool with Disk Heap:** Pages are managed in memory and flushed to disk behind the scenes, governed by strict LSN tracking.
- **Live Formal Checker:** The UI lets you run chaos testing loops, hooking directly into an invariant checker to guarantee that no dirty reads or lost updates ever slip through.

## Try it out locally
If you want to play with the code yourself:
```bash
# Clone and enter the repo
git clone https://github.com/AyushSid28/KilnDB.git
cd KilnDB

# Install requirements
pip install -r requirements.txt

# Run the test suite (42 tests covering concurrency and crash scenarios)
pytest

# Launch the interactive UI
streamlit run app.py
```

