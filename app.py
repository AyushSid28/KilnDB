"""
KILN DB Interactive Console
============================
Interactive demo of the KilnDB storage engine.
Test transactions, crash recovery, concurrency, and inspect WAL/catalog internals.
"""

import streamlit as st
import os
import sys
import tempfile
import subprocess
import struct
import time
import random
import shutil

# Ensure project modules are importable
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from engine import Engine, ConflictError, decode_redo_put, decode_redo_del, decode_commit
from txn import TxnState
from wal import WAL, RecordType
from checker import (
    Checker, Begin as CBegin, Read as CRead, Write as CWrite,
    Delete as CDelete, Commit as CCommit, Abort as CAbort,
    Crash as CCrash, Recovered as CRecovered,
)

# ─── Page Config ────────────────────────────────────────────────────
st.set_page_config(
    page_title="KILN DB",
    page_icon="K",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ─────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700;800&display=swap');

/* Global */
.stApp { font-family: 'Inter', sans-serif; }

/* Header */
.kiln-header {
    background: linear-gradient(135deg, #1a0f00 0%, #2d1800 50%, #1a0f00 100%);
    border: 1px solid #3d2800;
    border-radius: 16px;
    padding: 1.5rem 2rem;
    margin-bottom: 1.5rem;
    text-align: center;
}
.kiln-header h1 {
    font-family: 'Inter', sans-serif;
    font-weight: 800;
    font-size: 2.2rem;
    background: linear-gradient(135deg, #f59e0b, #fbbf24, #f59e0b);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin: 0;
    letter-spacing: 0.05em;
}
.kiln-header p {
    color: #a8a29e;
    font-size: 0.95rem;
    margin: 0.3rem 0 0 0;
}

/* Terminal output */
.terminal {
    background: #0a0a0a;
    border: 1px solid #2a2a2a;
    border-radius: 10px;
    padding: 1rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.82rem;
    line-height: 1.6;
    max-height: 420px;
    overflow-y: auto;
    color: #d4d4d4;
}
.terminal .cmd { color: #fbbf24; }
.terminal .ok { color: #10b981; }
.terminal .err { color: #ef4444; }
.terminal .val { color: #60a5fa; }
.terminal .dim { color: #525252; }

/* Status badges */
.badge {
    display: inline-block;
    padding: 0.2rem 0.7rem;
    border-radius: 9999px;
    font-size: 0.75rem;
    font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
    letter-spacing: 0.05em;
}
.badge-active { background: #1e3a1e; color: #4ade80; border: 1px solid #166534; }
.badge-committed { background: #1e293b; color: #60a5fa; border: 1px solid #1e40af; }
.badge-aborted { background: #3b1111; color: #f87171; border: 1px solid #991b1b; }
.badge-none { background: #1a1a1a; color: #737373; border: 1px solid #2a2a2a; }

/* WAL record types */
.wal-put { color: #10b981; font-weight: 600; }
.wal-del { color: #ef4444; font-weight: 600; }
.wal-commit { color: #3b82f6; font-weight: 600; }
.wal-checkpoint { color: #8b5cf6; font-weight: 600; }

/* Cards */
.metric-card {
    background: linear-gradient(135deg, #1a1a2e 0%, #16162a 100%);
    border: 1px solid #2a2a3e;
    border-radius: 12px;
    padding: 1rem 1.2rem;
    text-align: center;
}
.metric-card .label { color: #737373; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.1em; }
.metric-card .value { color: #fbbf24; font-size: 1.8rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }

/* Scenario card */
.scenario-card {
    background: #1a1a2e;
    border: 1px solid #2a2a3e;
    border-radius: 12px;
    padding: 1.2rem;
    margin-bottom: 0.8rem;
}
.scenario-card h4 { color: #fbbf24; margin: 0 0 0.4rem 0; }
.scenario-card p { color: #a8a29e; font-size: 0.85rem; margin: 0; }

/* Step indicator */
.step-box {
    background: #111;
    border-left: 3px solid #f59e0b;
    padding: 0.8rem 1rem;
    margin: 0.5rem 0;
    border-radius: 0 8px 8px 0;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.82rem;
}

/* Result boxes */
.result-pass {
    background: #052e16;
    border: 1px solid #166534;
    border-radius: 10px;
    padding: 1rem;
    color: #4ade80;
    font-family: 'JetBrains Mono', monospace;
}
.result-fail {
    background: #2d0a0a;
    border: 1px solid #991b1b;
    border-radius: 10px;
    padding: 1rem;
    color: #f87171;
    font-family: 'JetBrains Mono', monospace;
}

/* Sidebar styling */
section[data-testid="stSidebar"] .stMarkdown h2 {
    color: #fbbf24;
    font-size: 1.1rem;
}
</style>
""", unsafe_allow_html=True)

# ─── Session State Init ─────────────────────────────────────────────

def init_state():
    """Initialize all session state keys with defaults."""
    defaults = {
        "data_dir": os.path.join(tempfile.gettempdir(), "kilndb-console"),
        "engine": None,
        # Console
        "console_history": [],
        "console_txn": None,
        # Concurrent
        "conc_data_dir": None,
        "conc_engine": None,
        "conc_txn_a": None,
        "conc_txn_b": None,
        "conc_log": [],
        "conc_step": 0,
        "conc_scenario": None,
        # Crash
        "crash_log": [],
        "crash_baseline": {},
        "crash_data_dir": None,
        "crash_phase": 0,
        # Chaos
        "chaos_results": [],
        "chaos_running": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()


def get_engine():
    """Get or create the main engine instance."""
    if st.session_state.engine is None:
        os.makedirs(st.session_state.data_dir, exist_ok=True)
        st.session_state.engine = Engine(st.session_state.data_dir)
    return st.session_state.engine


def reset_engine():
    """Close engine, wipe data, create fresh."""
    if st.session_state.engine is not None:
        try:
            st.session_state.engine.close()
        except:
            pass
    st.session_state.engine = None
    st.session_state.console_txn = None
    st.session_state.console_history = []
    if os.path.exists(st.session_state.data_dir):
        shutil.rmtree(st.session_state.data_dir, ignore_errors=True)
    get_engine()


def txn_badge(txn):
    """Return an HTML badge for a transaction's state."""
    if txn is None:
        return '<span class="badge badge-none">NO TXN</span>'
    state = txn.state
    if state == TxnState.ACTIVE:
        return f'<span class="badge badge-active">ACTIVE T{txn.txn_id}</span>'
    elif state == TxnState.COMMITTED:
        return f'<span class="badge badge-committed">COMMITTED T{txn.txn_id}</span>'
    else:
        return f'<span class="badge badge-aborted">ABORTED T{txn.txn_id}</span>'


# ─── Header ─────────────────────────────────────────────────────────
st.markdown("""
<div class="kiln-header">
    <h1>KILN DB</h1>
</div>
""", unsafe_allow_html=True)

# ─── Sidebar ────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Engine Controls")

    db = get_engine()

    # Stats
    wal_path = os.path.join(st.session_state.data_dir, "wal.log")
    wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
    catalog_keys = len(db.catalog.versions) if db else 0
    total_versions = sum(len(v) for v in db.catalog.versions.values()) if db else 0

    c1, c2 = st.columns(2)
    c1.metric("WAL Size", f"{wal_size} B")
    c2.metric("Keys", catalog_keys)

    c3, c4 = st.columns(2)
    c3.metric("Versions", total_versions)
    c4.metric("Next TS", db.next_ts if db else 0)

    st.divider()

    if st.button("Reset Database", use_container_width=True, type="secondary"):
        reset_engine()
        st.rerun()

    st.divider()
    st.caption(f"Data: `{st.session_state.data_dir}`")
    st.caption("WAL / MVCC / Snapshot Isolation")


# ═══════════════════════════════════════════════════════════════════
# TABS
# ═══════════════════════════════════════════════════════════════════

tab_console, tab_conc, tab_crash, tab_wal, tab_catalog, tab_chaos = st.tabs([
    "Transaction Console",
    "Concurrent Txns",
    "Crash & Recovery",
    "WAL Inspector",
    "Catalog Viewer",
    "Chaos Testing",
])


# ═══════════════════════════════════════════════════════════════════
# TAB 1: TRANSACTION CONSOLE
# ═══════════════════════════════════════════════════════════════════

with tab_console:
    st.markdown("### Interactive Transaction Console")
    st.caption("Type database commands just like a real DBMS shell.")

    # Quick action buttons
    qcol1, qcol2, qcol3, qcol4, qcol5 = st.columns(5)

    def exec_console_cmd(cmd_str):
        """Execute a console command and append to history."""
        db = get_engine()
        parts = cmd_str.strip().split()
        if not parts:
            return
        cmd = parts[0].upper()
        result = ""
        is_error = False

        try:
            if cmd == "BEGIN":
                if st.session_state.console_txn is not None and \
                   st.session_state.console_txn.state == TxnState.ACTIVE:
                    result = "ERR: Already in a transaction. COMMIT or ABORT first."
                    is_error = True
                else:
                    txn = db.begin()
                    st.session_state.console_txn = txn
                    result = f"OK - Transaction T{txn.txn_id} started (start_ts={txn.start_ts})"

            elif cmd == "PUT":
                if st.session_state.console_txn is None or \
                   st.session_state.console_txn.state != TxnState.ACTIVE:
                    result = "ERR: No active transaction. BEGIN first."
                    is_error = True
                elif len(parts) < 3:
                    result = "ERR: Usage: PUT <key> <value>"
                    is_error = True
                else:
                    key = parts[1].encode()
                    value = " ".join(parts[2:]).encode()
                    db.put(st.session_state.console_txn, key, value)
                    result = f"OK - Buffered PUT {parts[1]} = {' '.join(parts[2:])}"

            elif cmd == "GET":
                if st.session_state.console_txn is None or \
                   st.session_state.console_txn.state != TxnState.ACTIVE:
                    result = "ERR: No active transaction. BEGIN first."
                    is_error = True
                elif len(parts) != 2:
                    result = "ERR: Usage: GET <key>"
                    is_error = True
                else:
                    key = parts[1].encode()
                    val = db.get(st.session_state.console_txn, key)
                    if val is None:
                        result = f"NOTFOUND - Key '{parts[1]}' does not exist"
                    else:
                        result = f"VALUE - {parts[1]} = {val.decode('utf-8', errors='replace')}"

            elif cmd == "DEL" or cmd == "DELETE":
                if st.session_state.console_txn is None or \
                   st.session_state.console_txn.state != TxnState.ACTIVE:
                    result = "ERR: No active transaction. BEGIN first."
                    is_error = True
                elif len(parts) != 2:
                    result = "ERR: Usage: DEL <key>"
                    is_error = True
                else:
                    key = parts[1].encode()
                    db.delete(st.session_state.console_txn, key)
                    result = f"OK - Buffered DEL {parts[1]}"

            elif cmd == "COMMIT":
                if st.session_state.console_txn is None or \
                   st.session_state.console_txn.state != TxnState.ACTIVE:
                    result = "ERR: No active transaction."
                    is_error = True
                else:
                    try:
                        db.commit(st.session_state.console_txn)
                        cts = st.session_state.console_txn.commit_ts
                        result = f"OK - Committed T{st.session_state.console_txn.txn_id} (commit_ts={cts})"
                    except ConflictError as e:
                        result = f"CONFLICT - {str(e)}"
                        is_error = True

            elif cmd == "ABORT":
                if st.session_state.console_txn is None or \
                   st.session_state.console_txn.state != TxnState.ACTIVE:
                    result = "ERR: No active transaction."
                    is_error = True
                else:
                    tid = st.session_state.console_txn.txn_id
                    st.session_state.console_txn.abort()
                    result = f"OK - Aborted T{tid}. Write set discarded."

            else:
                result = f"ERR: Unknown command '{cmd}'. Use BEGIN, PUT, GET, DEL, COMMIT, ABORT."
                is_error = True

        except Exception as e:
            result = f"ERR: {str(e)}"
            is_error = True

        st.session_state.console_history.append({
            "cmd": cmd_str.strip(),
            "result": result,
            "error": is_error,
        })

    # Quick buttons
    if qcol1.button("BEGIN", use_container_width=True, key="qb_begin"):
        exec_console_cmd("BEGIN")
        st.rerun()
    if qcol2.button("COMMIT", use_container_width=True, key="qb_commit"):
        exec_console_cmd("COMMIT")
        st.rerun()
    if qcol3.button("ABORT", use_container_width=True, key="qb_abort"):
        exec_console_cmd("ABORT")
        st.rerun()

    # Command input
    with st.form("console_form", clear_on_submit=True):
        cmd_input = st.text_input(
            "Command",
            placeholder="PUT name Ayush  |  GET name  |  DEL name",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Execute", use_container_width=True)
        if submitted and cmd_input.strip():
            exec_console_cmd(cmd_input)
            st.rerun()

    # Layout: history | state
    hist_col, state_col = st.columns([3, 1])

    with hist_col:
        st.markdown("**Command History**")
        if st.session_state.console_history:
            lines = []
            for entry in st.session_state.console_history:
                cmd_class = "cmd"
                res_class = "err" if entry["error"] else "ok"
                lines.append(f'<span class="{cmd_class}">> {entry["cmd"]}</span>')
                lines.append(f'<span class="{res_class}">  {entry["result"]}</span>')
                lines.append("")
            html = '<div class="terminal">' + "<br>".join(lines) + "</div>"
            st.markdown(html, unsafe_allow_html=True)
        else:
            st.markdown(
                '<div class="terminal"><span class="dim">No commands yet. '
                'Type BEGIN to start a transaction.</span></div>',
                unsafe_allow_html=True,
            )

    with state_col:
        st.markdown("**Transaction State**")
        txn = st.session_state.console_txn
        st.markdown(txn_badge(txn), unsafe_allow_html=True)

        if txn and txn.state == TxnState.ACTIVE and txn.write_set:
            st.markdown("**Write Set** _(buffered)_")
            for k, op in txn.write_set.items():
                key_str = k.decode("utf-8", errors="replace")
                if op.is_delete:
                    st.markdown(f"`{key_str}` : DEL")
                else:
                    val_str = op.value.decode("utf-8", errors="replace")
                    st.markdown(f"`{key_str}` : `{val_str}`")
        elif txn and txn.state == TxnState.ACTIVE:
            st.caption("Write set is empty")

        st.markdown("---")
        st.markdown("**Commands**")
        st.code("BEGIN\nPUT <key> <value>\nGET <key>\nDEL <key>\nCOMMIT\nABORT", language=None)


# ═══════════════════════════════════════════════════════════════════
# TAB 2: CONCURRENT TRANSACTIONS
# ═══════════════════════════════════════════════════════════════════

SCENARIOS = {
    "first_committer_wins": {
        "title": "First-Committer-Wins",
        "desc": "Two txns write the same key. First to commit wins, second gets ConflictError.",
        "steps": [
            ("setup", "PUT x = 100, COMMIT", "Baseline: key 'x' = 100"),
            ("a", "BEGIN", "Txn A starts - snapshot sees x=100"),
            ("b", "BEGIN", "Txn B starts - same snapshot"),
            ("a", "PUT x = 200", "A buffers x=200 (write set only)"),
            ("b", "PUT x = 300", "B buffers x=300 (write set only)"),
            ("a", "COMMIT", "A commits first - wins"),
            ("b", "COMMIT", "B gets ConflictError - x was modified after B's snapshot"),
            ("verify", "GET x", "Final value: x = 200 (A's write)"),
        ],
    },
    "snapshot_isolation": {
        "title": "Snapshot Isolation",
        "desc": "A transaction sees a frozen snapshot. Other commits are invisible.",
        "steps": [
            ("setup", "PUT x = 100, COMMIT", "Baseline: key 'x' = 100"),
            ("a", "BEGIN", "Txn A starts - snapshot frozen at this moment"),
            ("b", "BEGIN + PUT x = 999 + COMMIT", "Txn B commits x=999 AFTER A started"),
            ("a", "GET x", "A reads x -> still 100. Snapshot is frozen."),
            ("verify", "New txn: GET x", "A new txn sees x = 999 (B's committed value)"),
        ],
    },
    "write_skew": {
        "title": "Write Skew (Allowed under SI)",
        "desc": "Two txns read overlapping data, write different keys. Both commit - invariant can break.",
        "steps": [
            ("setup", "PUT doctor_a=on_call, doctor_b=on_call, COMMIT", "Both doctors on call"),
            ("a", "BEGIN, read both", "A sees both on_call"),
            ("b", "BEGIN, read both", "B sees both on_call"),
            ("a", "PUT doctor_a = off_call", "A: 'B is on call, safe to go off'"),
            ("b", "PUT doctor_b = off_call", "B: 'A is on call, safe to go off'"),
            ("a", "COMMIT", "A commits (different key from B - no conflict)"),
            ("b", "COMMIT", "B commits (different key from A - no conflict)"),
            ("verify", "GET both", "Both off_call. Invariant broken - this is expected under SI."),
        ],
    },
}


def run_scenario_step(scenario_key, step_idx):
    """Execute one step of a concurrent transaction scenario."""
    scenario = SCENARIOS[scenario_key]
    steps = scenario["steps"]
    if step_idx >= len(steps):
        return

    who, action, explanation = steps[step_idx]

    # Ensure clean engine for scenario
    if step_idx == 0:
        d = os.path.join(tempfile.gettempdir(), f"kilndb-conc-{int(time.time())}")
        if st.session_state.conc_engine is not None:
            try:
                st.session_state.conc_engine.close()
            except:
                pass
        st.session_state.conc_data_dir = d
        st.session_state.conc_engine = Engine(d)
        st.session_state.conc_txn_a = None
        st.session_state.conc_txn_b = None
        st.session_state.conc_log = []

    db = st.session_state.conc_engine
    result = ""

    try:
        if scenario_key == "first_committer_wins":
            result = _run_fcw_step(db, step_idx)
        elif scenario_key == "snapshot_isolation":
            result = _run_si_step(db, step_idx)
        elif scenario_key == "write_skew":
            result = _run_ws_step(db, step_idx)
    except Exception as e:
        result = f"ERROR: {str(e)}"

    st.session_state.conc_log.append({
        "step": step_idx + 1,
        "who": who,
        "action": action,
        "explanation": explanation,
        "result": result,
    })
    st.session_state.conc_step = step_idx + 1


def _run_fcw_step(db, idx):
    if idx == 0:
        t = db.begin()
        db.put(t, b"x", b"100")
        db.commit(t)
        return "Committed x=100"
    elif idx == 1:
        st.session_state.conc_txn_a = db.begin()
        return f"T{st.session_state.conc_txn_a.txn_id} (start_ts={st.session_state.conc_txn_a.start_ts})"
    elif idx == 2:
        st.session_state.conc_txn_b = db.begin()
        return f"T{st.session_state.conc_txn_b.txn_id} (start_ts={st.session_state.conc_txn_b.start_ts})"
    elif idx == 3:
        db.put(st.session_state.conc_txn_a, b"x", b"200")
        return "Buffered in A's write set"
    elif idx == 4:
        db.put(st.session_state.conc_txn_b, b"x", b"300")
        return "Buffered in B's write set"
    elif idx == 5:
        db.commit(st.session_state.conc_txn_a)
        return f"PASS: Committed (commit_ts={st.session_state.conc_txn_a.commit_ts})"
    elif idx == 6:
        try:
            db.commit(st.session_state.conc_txn_b)
            return "WARN: Unexpected - commit succeeded"
        except ConflictError:
            return "CONFLICT: ConflictError raised. First-committer-wins enforced."
    elif idx == 7:
        t = db.begin()
        val = db.get(t, b"x")
        return f"x = {val.decode() if val else 'None'}"


def _run_si_step(db, idx):
    if idx == 0:
        t = db.begin()
        db.put(t, b"x", b"100")
        db.commit(t)
        return "Committed x=100"
    elif idx == 1:
        st.session_state.conc_txn_a = db.begin()
        return f"T{st.session_state.conc_txn_a.txn_id} (start_ts={st.session_state.conc_txn_a.start_ts})"
    elif idx == 2:
        t = db.begin()
        db.put(t, b"x", b"999")
        db.commit(t)
        return f"B committed x=999 (commit_ts={t.commit_ts})"
    elif idx == 3:
        val = db.get(st.session_state.conc_txn_a, b"x")
        return f"A reads x = {val.decode() if val else 'None'} (snapshot frozen)"
    elif idx == 4:
        t = db.begin()
        val = db.get(t, b"x")
        return f"New txn reads x = {val.decode() if val else 'None'}"


def _run_ws_step(db, idx):
    if idx == 0:
        t = db.begin()
        db.put(t, b"doctor_a", b"on_call")
        db.put(t, b"doctor_b", b"on_call")
        db.commit(t)
        return "Both doctors on_call"
    elif idx == 1:
        st.session_state.conc_txn_a = db.begin()
        a = db.get(st.session_state.conc_txn_a, b"doctor_a")
        b = db.get(st.session_state.conc_txn_a, b"doctor_b")
        return f"A={a.decode()}, B={b.decode()}"
    elif idx == 2:
        st.session_state.conc_txn_b = db.begin()
        a = db.get(st.session_state.conc_txn_b, b"doctor_a")
        b = db.get(st.session_state.conc_txn_b, b"doctor_b")
        return f"A={a.decode()}, B={b.decode()}"
    elif idx == 3:
        db.put(st.session_state.conc_txn_a, b"doctor_a", b"off_call")
        return "Buffered in A's write set"
    elif idx == 4:
        db.put(st.session_state.conc_txn_b, b"doctor_b", b"off_call")
        return "Buffered in B's write set"
    elif idx == 5:
        db.commit(st.session_state.conc_txn_a)
        return f"PASS: Committed (commit_ts={st.session_state.conc_txn_a.commit_ts})"
    elif idx == 6:
        db.commit(st.session_state.conc_txn_b)
        return f"PASS: Committed (commit_ts={st.session_state.conc_txn_b.commit_ts})"
    elif idx == 7:
        t = db.begin()
        a = db.get(t, b"doctor_a")
        b = db.get(t, b"doctor_b")
        return f"doctor_a={a.decode()}, doctor_b={b.decode()} [INVARIANT BROKEN]"


with tab_conc:
    st.markdown("### Concurrent Transaction Scenarios")
    st.caption("Step through real MVCC scenarios to see snapshot isolation and conflict detection in action.")

    sc1, sc2, sc3 = st.columns(3)

    with sc1:
        st.markdown(
            '<div class="scenario-card"><h4>First-Committer-Wins</h4>'
            "<p>Two txns write same key. Second loses.</p></div>",
            unsafe_allow_html=True,
        )
        if st.button("Run This", key="sc_fcw", use_container_width=True):
            st.session_state.conc_scenario = "first_committer_wins"
            st.session_state.conc_step = 0
            st.session_state.conc_log = []
            st.rerun()

    with sc2:
        st.markdown(
            '<div class="scenario-card"><h4>Snapshot Isolation</h4>'
            "<p>Frozen snapshots - can't see future commits.</p></div>",
            unsafe_allow_html=True,
        )
        if st.button("Run This", key="sc_si", use_container_width=True):
            st.session_state.conc_scenario = "snapshot_isolation"
            st.session_state.conc_step = 0
            st.session_state.conc_log = []
            st.rerun()

    with sc3:
        st.markdown(
            '<div class="scenario-card"><h4>Write Skew</h4>'
            "<p>Different keys, same snapshot - both commit.</p></div>",
            unsafe_allow_html=True,
        )
        if st.button("Run This", key="sc_ws", use_container_width=True):
            st.session_state.conc_scenario = "write_skew"
            st.session_state.conc_step = 0
            st.session_state.conc_log = []
            st.rerun()

    # Active scenario
    if st.session_state.conc_scenario:
        scenario = SCENARIOS[st.session_state.conc_scenario]
        st.markdown(f"#### {scenario['title']}")
        st.caption(scenario["desc"])

        total_steps = len(scenario["steps"])
        current = st.session_state.conc_step

        # Progress
        st.progress(current / total_steps, text=f"Step {current}/{total_steps}")

        # Next step button
        if current < total_steps:
            _, who, action, explanation = (None,) + scenario["steps"][current]
            st.markdown(
                f'<div class="step-box">Next: <b>{action}</b> - {explanation}</div>',
                unsafe_allow_html=True,
            )
            if st.button("Execute Next Step", type="primary", use_container_width=True, key="conc_next"):
                run_scenario_step(st.session_state.conc_scenario, current)
                st.rerun()
        else:
            st.success("Scenario complete.")

        # Log
        if st.session_state.conc_log:
            st.markdown("**Execution Log**")
            lines = []
            for entry in st.session_state.conc_log:
                who = entry["who"].upper()
                lines.append(
                    f'<span class="dim">Step {entry["step"]}</span> '
                    f'<span class="cmd">[{who}]</span> {entry["action"]}<br>'
                    f'<span class="ok">  -> {entry["result"]}</span><br>'
                    f'<span class="dim">  {entry["explanation"]}</span>'
                )
            html = '<div class="terminal">' + "<br><br>".join(lines) + "</div>"
            st.markdown(html, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════
# TAB 3: CRASH & RECOVERY
# ═══════════════════════════════════════════════════════════════════

FAULT_POINTS = {
    "before_wal_append": {
        "label": "Before WAL Append",
        "desc": "Crash before anything is written. Data is lost - this is correct.",
        "expect_survive": False,
    },
    "during_wal_append": {
        "label": "During WAL Append (torn write)",
        "desc": "Half a WAL record written. Recovery truncates the torn record. Uncommitted data lost.",
        "expect_survive": False,
    },
    "after_wal_sync_before_ack": {
        "label": "After WAL Sync, Before ACK",
        "desc": "WAL is synced = data IS durable. Client never heard OK, but recovery replays it.",
        "expect_survive": True,
    },
    "after_ack_before_heap": {
        "label": "After ACK, Before Heap Write",
        "desc": "Client got OK. Heap is stale, but WAL has the truth. Recovery replays.",
        "expect_survive": True,
    },
    "during_heap_page_write": {
        "label": "During Heap Page Write (checkpoint)",
        "desc": "Torn page on disk. Checksum catches it. WAL replays on recovery.",
        "expect_survive": True,
    },
}

with tab_crash:
    st.markdown("### Crash & Recovery Simulator")
    st.caption("Commit data, inject a fault at a specific point in the write path, crash, and verify recovery.")

    # Phase display
    phase = st.session_state.crash_phase

    # Step 1: Commit baseline
    st.markdown("#### Step 1: Commit Baseline Data")
    with st.form("crash_baseline_form"):
        cr1, cr2 = st.columns(2)
        crash_key = cr1.text_input("Key", value="balance", key="crash_key_input")
        crash_val = cr2.text_input("Value", value="1000", key="crash_val_input")
        baseline_submitted = st.form_submit_button(
            "Commit & Close Engine" if phase == 0 else "Baseline Committed",
            disabled=phase > 0,
            use_container_width=True,
        )
        if baseline_submitted and phase == 0:
            d = os.path.join(tempfile.gettempdir(), f"kilndb-crash-{int(time.time())}")
            db = Engine(d)
            txn = db.begin()
            db.put(txn, crash_key.encode(), crash_val.encode())
            db.commit(txn)
            db.close()
            st.session_state.crash_data_dir = d
            st.session_state.crash_baseline = {crash_key: crash_val}
            st.session_state.crash_log = [f"COMMITTED: {crash_key}={crash_val}, engine closed."]
            st.session_state.crash_phase = 1
            st.rerun()

    # Step 2: Select fault + crash
    if phase >= 1:
        st.markdown("#### Step 2: Inject Fault & Crash")

        fault_name = st.selectbox(
            "Fault Point",
            options=list(FAULT_POINTS.keys()),
            format_func=lambda k: FAULT_POINTS[k]["label"],
            key="crash_fault_select",
        )

        info = FAULT_POINTS[fault_name]
        st.info(f"**{info['label']}**: {info['desc']}")

        if phase == 1 and st.button("Simulate Crash", type="primary", use_container_width=True):
            action = "checkpoint" if fault_name == "during_heap_page_write" else "commit"
            worker = os.path.join(PROJECT_ROOT, "crash_worker.py")
            result = subprocess.run(
                [sys.executable, worker, st.session_state.crash_data_dir, fault_name, action],
                capture_output=True, text=True, timeout=10,
            )
            st.session_state.crash_log.append(
                f"CRASHED at '{info['label']}' (exit code {result.returncode})"
            )
            st.session_state.crash_phase = 2
            st.rerun()

    # Step 3: Recovery
    if phase >= 2:
        st.markdown("#### Step 3: Recovery & Verification")

        if phase == 2 and st.button("Recover Engine & Verify", type="primary", use_container_width=True):
            try:
                db = Engine(st.session_state.crash_data_dir)
                txn = db.begin()
                results = {}
                for k, expected in st.session_state.crash_baseline.items():
                    val = db.get(txn, k.encode())
                    actual = val.decode() if val else None
                    results[k] = {"expected": expected, "actual": actual, "match": actual == expected}
                db.close()

                st.session_state.crash_log.append("RECOVERED: Engine rebuilt from WAL.")
                for k, r in results.items():
                    if r["match"]:
                        st.session_state.crash_log.append(f"PASS: {k} = {r['actual']} (survived)")
                    else:
                        st.session_state.crash_log.append(
                            f"FAIL: {k}: expected {r['expected']}, got {r['actual']}"
                        )
                st.session_state.crash_phase = 3
            except Exception as e:
                st.session_state.crash_log.append(f"FAIL: Recovery error: {str(e)}")
                st.session_state.crash_phase = 3
            st.rerun()

    # Reset
    if phase >= 3:
        if st.button("Run Another Crash Test", use_container_width=True):
            st.session_state.crash_phase = 0
            st.session_state.crash_log = []
            st.session_state.crash_baseline = {}
            st.session_state.crash_data_dir = None
            st.rerun()

    # Log display
    if st.session_state.crash_log:
        st.markdown("**Recovery Log**")
        lines = []
        for entry in st.session_state.crash_log:
            if "PASS" in entry:
                css = "ok"
            elif "FAIL" in entry or "CRASHED" in entry:
                css = "err"
            else:
                css = "val"
            lines.append(f'<span class="{css}">{entry}</span>')
        html = '<div class="terminal">' + "<br>".join(lines) + "</div>"
        st.markdown(html, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════
# TAB 4: WAL INSPECTOR
# ═══════════════════════════════════════════════════════════════════

def parse_wal_records(data_dir):
    """Read WAL file and return parsed records."""
    wal_path = os.path.join(data_dir, "wal.log")
    if not os.path.exists(wal_path):
        return []

    records = []
    header_fmt = "<IIB"
    header_size = struct.calcsize(header_fmt)

    with open(wal_path, "rb") as f:
        raw = f.read()

    offset = 0
    while offset + header_size <= len(raw):
        payload_len, crc, rec_type = struct.unpack_from(header_fmt, raw, offset)
        payload_start = offset + header_size
        if payload_start + payload_len > len(raw):
            break

        payload = raw[payload_start : payload_start + payload_len]
        lsn = offset
        offset = payload_start + payload_len

        record = {"lsn": lsn, "type": rec_type, "size": header_size + payload_len}

        try:
            if rec_type == RecordType.REDO_PUT:
                txn_id, commit_ts, key, value = decode_redo_put(payload)
                record.update({
                    "type_name": "REDO_PUT",
                    "txn_id": txn_id, "commit_ts": commit_ts,
                    "key": key.decode("utf-8", errors="replace"),
                    "value": value.decode("utf-8", errors="replace"),
                })
            elif rec_type == RecordType.REDO_DEL:
                txn_id, commit_ts, key = decode_redo_del(payload)
                record.update({
                    "type_name": "REDO_DEL",
                    "txn_id": txn_id, "commit_ts": commit_ts,
                    "key": key.decode("utf-8", errors="replace"),
                    "value": "[TOMBSTONE]",
                })
            elif rec_type == RecordType.COMMIT:
                txn_id, commit_ts = decode_commit(payload)
                record.update({
                    "type_name": "COMMIT",
                    "txn_id": txn_id, "commit_ts": commit_ts,
                    "key": "-", "value": "-",
                })
            else:
                record.update({
                    "type_name": f"TYPE_{rec_type}",
                    "txn_id": "?", "commit_ts": "?",
                    "key": "?", "value": f"{len(payload)} bytes",
                })
        except Exception:
            record.update({
                "type_name": f"TYPE_{rec_type}",
                "txn_id": "?", "commit_ts": "?",
                "key": "?", "value": "decode error",
            })

        records.append(record)

    return records


with tab_wal:
    st.markdown("### WAL Inspector")
    st.caption("View every record in the Write-Ahead Log. This is the source of truth for crash recovery.")

    db = get_engine()
    records = parse_wal_records(st.session_state.data_dir)

    # Metrics
    wm1, wm2, wm3, wm4 = st.columns(4)
    wal_path = os.path.join(st.session_state.data_dir, "wal.log")
    wal_bytes = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0

    wm1.markdown(
        f'<div class="metric-card"><div class="label">Records</div>'
        f'<div class="value">{len(records)}</div></div>',
        unsafe_allow_html=True,
    )
    puts = sum(1 for r in records if r.get("type_name") == "REDO_PUT")
    wm2.markdown(
        f'<div class="metric-card"><div class="label">PUT Ops</div>'
        f'<div class="value">{puts}</div></div>',
        unsafe_allow_html=True,
    )
    dels = sum(1 for r in records if r.get("type_name") == "REDO_DEL")
    wm3.markdown(
        f'<div class="metric-card"><div class="label">DEL Ops</div>'
        f'<div class="value">{dels}</div></div>',
        unsafe_allow_html=True,
    )
    commits = sum(1 for r in records if r.get("type_name") == "COMMIT")
    wm4.markdown(
        f'<div class="metric-card"><div class="label">Commits</div>'
        f'<div class="value">{commits}</div></div>',
        unsafe_allow_html=True,
    )

    st.markdown(f"**WAL File**: `{wal_path}` ({wal_bytes} bytes)")

    if records:
        # Checkpoint LSN
        meta_path = os.path.join(st.session_state.data_dir, "meta")
        ckpt_lsn = 0
        if os.path.exists(meta_path):
            try:
                import json
                with open(meta_path) as f:
                    ckpt_lsn = json.load(f).get("checkpoint_lsn", 0)
            except:
                pass

        # Display records
        lines = []
        for r in records:
            tn = r.get("type_name", "?")
            if tn == "REDO_PUT":
                type_html = '<span class="wal-put">REDO_PUT</span>'
            elif tn == "REDO_DEL":
                type_html = '<span class="wal-del">REDO_DEL</span>'
            elif tn == "COMMIT":
                type_html = '<span class="wal-commit">COMMIT  </span>'
            else:
                type_html = f'<span class="wal-checkpoint">{tn}</span>'

            skip = "[SKIP]" if r["lsn"] < ckpt_lsn else "      "
            lines.append(
                f'{skip} LSN={r["lsn"]:>5}  {type_html}  '
                f'T{r.get("txn_id","?"):<4}  ts={r.get("commit_ts","?"):<4}  '
                f'{r.get("key",""):<15} = {r.get("value","")}'
            )
        html = '<div class="terminal">' + "<br>".join(lines) + "</div>"
        st.markdown(html, unsafe_allow_html=True)

        if ckpt_lsn > 0:
            st.caption(f"[SKIP] = skipped during recovery (checkpoint_lsn = {ckpt_lsn})")
    else:
        st.info("WAL is empty. Use the Transaction Console to write some data first.")


# ═══════════════════════════════════════════════════════════════════
# TAB 5: CATALOG VIEWER
# ═══════════════════════════════════════════════════════════════════

with tab_catalog:
    st.markdown("### Version Chain Viewer")
    st.caption("Inspect the MVCC catalog: every committed version of every key, with visibility rules.")

    db = get_engine()
    versions = db.catalog.versions

    if not versions:
        st.info("Catalog is empty. Use the Transaction Console to commit some data first.")
    else:
        keys = sorted(versions.keys(), key=lambda k: k.decode("utf-8", errors="replace"))
        key_labels = [k.decode("utf-8", errors="replace") for k in keys]

        selected_key_label = st.selectbox("Select Key", key_labels, key="catalog_key_sel")
        selected_key = keys[key_labels.index(selected_key_label)]

        chain = versions[selected_key]

        st.markdown(f"**Key `{selected_key_label}`** - {len(chain)} version(s)")

        # Version table
        rows = []
        for i, v in enumerate(chain):
            val_display = "TOMBSTONE" if v.is_tombstone else (
                v.value.decode("utf-8", errors="replace") if v.value else "None"
            )
            end_display = "inf (current)" if v.end_ts == 0 else str(v.end_ts)
            rows.append({
                "#": i,
                "begin_ts": v.begin_ts,
                "end_ts": end_display,
                "value": val_display,
                "tombstone": "Yes" if v.is_tombstone else "No",
            })

        # Render as styled table
        for row in rows:
            is_current = row["end_ts"] == "inf (current)"
            border_color = "#f59e0b" if is_current else "#2a2a2a"
            bg = "#1a1500" if is_current else "#111"
            st.markdown(
                f'<div style="background:{bg};border:1px solid {border_color};'
                f'border-radius:8px;padding:0.8rem;margin:0.4rem 0;'
                f'font-family:JetBrains Mono,monospace;font-size:0.85rem;">'
                f'<b>v{row["#"]}</b> &nbsp; '
                f'begin_ts=<span style="color:#fbbf24">{row["begin_ts"]}</span> &nbsp; '
                f'end_ts=<span style="color:#60a5fa">{row["end_ts"]}</span> &nbsp; '
                f'value=<span style="color:#10b981">{row["value"]}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Visibility calculator
        st.markdown("---")
        st.markdown("**Snapshot Visibility Calculator**")
        test_ts = st.number_input(
            "Enter a start_ts to see which version is visible:",
            min_value=0, max_value=max(db.next_ts, 10), value=max(db.next_ts - 1, 0),
            key="vis_ts",
        )
        visible = db.catalog.get_visible(selected_key, test_ts)
        if visible is None:
            st.markdown(
                f'<div class="result-fail">At start_ts={test_ts}: Key not visible (NOTFOUND)</div>',
                unsafe_allow_html=True,
            )
        elif visible.is_tombstone:
            st.markdown(
                f'<div class="result-fail">At start_ts={test_ts}: TOMBSTONE (key was deleted)</div>',
                unsafe_allow_html=True,
            )
        else:
            val = visible.value.decode("utf-8", errors="replace") if visible.value else "None"
            st.markdown(
                f'<div class="result-pass">At start_ts={test_ts}: value = "{val}" '
                f'(from version with begin_ts={visible.begin_ts})</div>',
                unsafe_allow_html=True,
            )


# ═══════════════════════════════════════════════════════════════════
# TAB 6: CHAOS TESTING
# ═══════════════════════════════════════════════════════════════════

with tab_chaos:
    st.markdown("### Chaos Testing Dashboard")
    st.caption("Run randomized crash-recovery loops and verify invariants.")

    ch1, ch2 = st.columns(2)
    num_runs = ch1.slider("Number of Runs", min_value=1, max_value=30, value=10, key="chaos_runs")
    num_keys = ch2.slider("Baseline Keys per Run", min_value=1, max_value=10, value=3, key="chaos_keys")

    fault_options = list(FAULT_POINTS.keys())

    if st.button("Run Chaos Test", type="primary", use_container_width=True):
        results = []
        progress = st.progress(0, text="Starting chaos test...")

        for run_idx in range(num_runs):
            progress.progress(
                (run_idx) / num_runs,
                text=f"Run {run_idx + 1}/{num_runs}...",
            )

            d = os.path.join(tempfile.gettempdir(), f"kilndb-chaos-{int(time.time())}-{run_idx}")
            seed = run_idx * 42 + int(time.time()) % 1000
            random.seed(seed)
            fault = random.choice(fault_options)

            try:
                # Commit baseline
                db = Engine(d)
                baseline = {}
                for i in range(num_keys):
                    txn = db.begin()
                    k = f"key{i}".encode()
                    v = f"val{i}".encode()
                    db.put(txn, k, v)
                    db.commit(txn)
                    baseline[k] = v
                db.close()

                # Crash
                action = "checkpoint" if fault == "during_heap_page_write" else "commit"
                worker = os.path.join(PROJECT_ROOT, "crash_worker.py")
                subprocess.run(
                    [sys.executable, worker, d, fault, action],
                    capture_output=True, timeout=10,
                )

                # Recover
                db = Engine(d)
                txn = db.begin()
                survived = 0
                total = len(baseline)
                for k, expected in baseline.items():
                    actual = db.get(txn, k)
                    if actual == expected:
                        survived += 1
                db.close()

                results.append({
                    "run": run_idx + 1,
                    "fault": FAULT_POINTS[fault]["label"],
                    "keys": total,
                    "survived": survived,
                    "status": "PASS" if survived == total else "FAIL",
                    "seed": seed,
                })

            except Exception as e:
                results.append({
                    "run": run_idx + 1,
                    "fault": FAULT_POINTS.get(fault, {}).get("label", fault),
                    "keys": num_keys,
                    "survived": 0,
                    "status": f"ERROR: {str(e)[:40]}",
                    "seed": seed,
                })

            # Cleanup
            shutil.rmtree(d, ignore_errors=True)

        progress.progress(1.0, text="Chaos test complete.")
        st.session_state.chaos_results = results

    # Display results
    if st.session_state.chaos_results:
        results = st.session_state.chaos_results
        passes = sum(1 for r in results if r["status"] == "PASS")
        fails = sum(1 for r in results if r["status"] == "FAIL")
        errors = sum(1 for r in results if "ERROR" in r["status"])

        rm1, rm2, rm3 = st.columns(3)
        rm1.markdown(
            f'<div class="metric-card"><div class="label">Passed</div>'
            f'<div class="value" style="color:#10b981">{passes}</div></div>',
            unsafe_allow_html=True,
        )
        rm2.markdown(
            f'<div class="metric-card"><div class="label">Failed</div>'
            f'<div class="value" style="color:#ef4444">{fails}</div></div>',
            unsafe_allow_html=True,
        )
        rm3.markdown(
            f'<div class="metric-card"><div class="label">Errors</div>'
            f'<div class="value" style="color:#f59e0b">{errors}</div></div>',
            unsafe_allow_html=True,
        )

        st.markdown("**Results**")

        lines = []
        for r in results:
            status_class = "ok" if r["status"] == "PASS" else "err"
            lines.append(
                f'<span class="dim">Run {r["run"]:>2}</span>  '
                f'<span class="{status_class}">{r["status"]:<6}</span>  '
                f'{r["fault"]:<35}  '
                f'<span class="val">{r["survived"]}/{r["keys"]} keys survived</span>'
            )
        html = '<div class="terminal">' + "<br>".join(lines) + "</div>"
        st.markdown(html, unsafe_allow_html=True)

        if fails == 0 and errors == 0:
            st.balloons()
            st.success(f"All {passes} chaos runs passed. The engine is crash-safe.")
        elif fails > 0:
            st.error(f"{fails} run(s) failed. Check the results above.")
