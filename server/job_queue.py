"""
JobQueue - SQLite-backed persistent job queue for background tasks.

Replaces the in-memory BACKGROUND_JOBS dict with persistent storage.
Jobs survive server restarts and can be recovered.

Schema:
    jobs (
        id TEXT PRIMARY KEY,
        title TEXT,
        command TEXT,
        status TEXT,  -- 'pending', 'running', 'completed', 'failed', 'cancelled', 'orphaned'
        started_at REAL,
        completed_at REAL,
        log_path TEXT,
        pid INTEGER,
        session_id TEXT,
        exit_code INTEGER
    )

Concurrency limits:
    - MAX_CONCURRENT: Maximum jobs running at once (default: CPU count)
    - MAX_PENDING_PER_SESSION: Maximum pending jobs per session (default: 50)
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional


def _get_db_path() -> str:
    """Get the database path from environment or default."""
    workspace = os.environ.get("AGENTIC_WORKSPACE") or os.path.expanduser("~/AgentIC-workspace")
    state_dir = Path(workspace) / ".agentic"
    state_dir.mkdir(parents=True, exist_ok=True)
    return str(state_dir / "jobs.db")


def _get_log_dir() -> str:
    """Get the log directory for job outputs."""
    workspace = os.environ.get("AGENTIC_WORKSPACE") or os.path.expanduser("~/AgentIC-workspace")
    log_dir = Path(workspace) / ".agentic" / "job-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir)


class JobQueue:
    """SQLite-backed persistent job queue for background tasks."""

    MAX_CONCURRENT = int(os.environ.get("AGENTIC_MAX_JOBS", os.cpu_count() or 4))
    MAX_PENDING_PER_SESSION = int(os.environ.get("AGENTIC_MAX_PENDING", 50))
    MAX_HISTORY = int(os.environ.get("AGENTIC_JOBS_HISTORY", 1000))

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _get_db_path()
        self.log_dir = _get_log_dir()
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    command TEXT,
                    status TEXT DEFAULT 'pending',
                    started_at REAL,
                    completed_at REAL,
                    log_path TEXT,
                    pid INTEGER,
                    session_id TEXT,
                    exit_code INTEGER
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_session_id ON jobs(session_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_started_at ON jobs(started_at)
            """)
            conn.commit()

    def recover(self) -> dict:
        """
        Recover jobs from previous session. Called on server startup.
        Checks if running PIDs are still alive, marking dead processes as 'orphaned' or 'completed'.
        """
        recovered = 0
        orphaned = 0
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT id, pid, log_path FROM jobs WHERE status = 'running'")
                running_jobs = cursor.fetchall()
                
                for job in running_jobs:
                    pid = job["pid"]
                    is_alive = False
                    if pid:
                        try:
                            if os.name != "nt":
                                os.kill(int(pid), 0)
                                is_alive = True
                            else:
                                import ctypes
                                handle = ctypes.windll.kernel32.OpenProcess(1, False, int(pid))
                                if handle:
                                    ctypes.windll.kernel32.CloseHandle(handle)
                                    is_alive = True
                        except Exception:
                            is_alive = False

                    if is_alive:
                        recovered += 1
                    else:
                        conn.execute("""
                            UPDATE jobs SET status = 'orphaned', completed_at = ? WHERE id = ?
                        """, [time.time(), job["id"]])
                        orphaned += 1

                # Clean up pending jobs
                cursor = conn.execute("UPDATE jobs SET status = 'orphaned' WHERE status = 'pending'")
                orphaned += cursor.rowcount
                conn.commit()

        return {'orphaned': orphaned, 'recovered': recovered}


    def generate_id(self) -> str:
        """Generate a unique job ID."""
        return f"bg_{uuid.uuid4().hex[:8]}"

    def submit(
        self,
        title: str,
        command: str,
        session_id: str = "",
        pid: Optional[int] = None,
    ) -> str:
        """
        Submit a new job to the queue.
        
        Args:
            title: Human-readable job title
            command: Shell command being executed
            session_id: Associated session ID (optional)
            pid: Process ID if already running (optional)
            
        Returns:
            Job ID string
        """
        job_id = self.generate_id()
        log_path = os.path.join(self.log_dir, f"{job_id}.log")
        
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO jobs (id, title, command, status, started_at, log_path, pid, session_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    job_id,
                    title[:200],
                    command[:1000],
                    'running' if pid else 'pending',
                    time.time(),
                    log_path,
                    pid,
                    session_id,
                ])
                conn.commit()
        
        return job_id

    def get(self, job_id: str) -> Optional[dict]:
        """Get a job by ID."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM jobs WHERE id = ?", [job_id])
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None

    def list(
        self,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        List jobs, optionally filtered.
        
        Args:
            session_id: Filter by session ID
            status: Filter by status
            limit: Maximum number of results
            
        Returns:
            List of job dicts
        """
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                query = "SELECT * FROM jobs WHERE 1=1"
                params = []
                
                if session_id:
                    query += " AND (session_id = ? OR session_id = '' OR session_id IS NULL)"
                    params.append(session_id)
                
                if status:
                    query += " AND status = ?"
                    params.append(status)
                
                query += " ORDER BY started_at DESC LIMIT ?"
                params.append(limit)
                
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

    def update_status(
        self,
        job_id: str,
        status: str,
        exit_code: Optional[int] = None,
    ) -> bool:
        """
        Update job status.
        
        Args:
            job_id: Job ID
            status: New status ('running', 'completed', 'failed', 'cancelled')
            exit_code: Process exit code (optional)
            
        Returns:
            True if updated, False if job not found
        """
        valid_statuses = {'pending', 'running', 'completed', 'failed', 'cancelled', 'orphaned'}
        if status not in valid_statuses:
            raise ValueError(f"Invalid status: {status}")
        
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                if exit_code is not None:
                    cursor = conn.execute("""
                        UPDATE jobs 
                        SET status = ?, completed_at = ?, exit_code = ?
                        WHERE id = ?
                    """, [status, time.time(), exit_code, job_id])
                else:
                    cursor = conn.execute("""
                        UPDATE jobs 
                        SET status = ?, completed_at = ?
                        WHERE id = ?
                    """, [status, time.time(), job_id])
                
                conn.commit()
                return cursor.rowcount > 0

    def update_pid(self, job_id: str, pid: int) -> bool:
        """Update the PID for a job."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    UPDATE jobs SET pid = ? WHERE id = ?
                """, [pid, job_id])
                conn.commit()
                return cursor.rowcount > 0

    def cancel(self, job_id: str) -> Optional[int]:
        """
        Cancel a job. Returns the PID if found (so caller can kill the process).
        
        Returns:
            PID if job was running, None if not found or already completed
        """
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT pid, status FROM jobs WHERE id = ?", [job_id])
                row = cursor.fetchone()
                
                if not row:
                    return None
                
                if row['status'] not in ('running', 'pending'):
                    return None
                
                conn.execute("""
                    UPDATE jobs 
                    SET status = 'cancelled', completed_at = ?
                    WHERE id = ?
                """, [time.time(), job_id])
                conn.commit()
                
                return row['pid']

    def count(self, status: Optional[str] = None, session_id: Optional[str] = None) -> int:
        """Count jobs matching criteria."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                query = "SELECT COUNT(*) FROM jobs WHERE 1=1"
                params = []
                
                if status:
                    query += " AND status = ?"
                    params.append(status)
                
                if session_id:
                    query += " AND session_id = ?"
                    params.append(session_id)
                
                cursor = conn.execute(query, params)
                return cursor.fetchone()[0]

    def can_submit(self, session_id: str = "") -> tuple[bool, str]:
        """
        Check if a new job can be submitted based on concurrency limits.
        
        Returns:
            (can_submit: bool, reason: str)
        """
        running = self.count(status='running')
        if running >= self.MAX_CONCURRENT:
            return False, f"Max concurrent jobs reached ({self.MAX_CONCURRENT})"
        
        if session_id:
            pending_session = self.count(status='pending', session_id=session_id)
            if pending_session >= self.MAX_PENDING_PER_SESSION:
                return False, f"Session has {pending_session} pending jobs (max {self.MAX_PENDING_PER_SESSION})"
        
        return True, ""

    def cleanup_old_jobs(self, days: int = 7) -> int:
        """
        Remove old completed/failed/cancelled jobs from database.
        
        Args:
            days: Remove jobs older than this many days
            
        Returns:
            Number of jobs removed
        """
        cutoff = time.time() - (days * 24 * 60 * 60)
        
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    DELETE FROM jobs 
                    WHERE status IN ('completed', 'failed', 'cancelled') 
                    AND completed_at IS NOT NULL 
                    AND completed_at < ?
                """, [cutoff])
                conn.commit()
                return cursor.rowcount

    def prune_history(self, keep: int = 1000) -> int:
        """
        Keep only the most recent jobs in history.
        
        Args:
            keep: Number of recent jobs to keep
            
        Returns:
            Number of jobs removed
        """
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    DELETE FROM jobs 
                    WHERE id IN (
                        SELECT id FROM jobs 
                        WHERE status IN ('completed', 'failed', 'cancelled', 'orphaned')
                        ORDER BY started_at DESC
                        LIMIT -1 OFFSET ?
                    )
                """, [keep])
                conn.commit()
                return cursor.rowcount


# Singleton instance
_queue_instance: Optional[JobQueue] = None
_queue_lock = threading.Lock()


def get_job_queue() -> JobQueue:
    """Get the singleton JobQueue instance."""
    global _queue_instance
    if _queue_instance is None:
        with _queue_lock:
            if _queue_instance is None:
                _queue_instance = JobQueue()
    return _queue_instance
