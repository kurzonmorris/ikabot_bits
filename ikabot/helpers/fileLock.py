#! /usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Shared file-based locking utility for cross-process synchronization.

Provides atomic file locks that work across multiple ikabot processes on
both Windows and Unix. Used for:
  - Session data file (.ikabot) access
  - Shipping coordination (merchant ships / freighters)

Lock files are stored in the user's home directory with a standardized
naming convention so all ikabot scripts and custom modules can coordinate.
"""

import json
import os
import time

import psutil


class FileLock:
    """
    File-based lock for cross-process synchronization.

    Uses atomic file creation (os.O_CREAT | os.O_EXCL) to prevent race
    conditions. Reentrant within the same process via a per-process
    reference counter.

    Can be used as a context manager:
        with FileLock("/path/to/lock"):
            # critical section

    Parameters
    ----------
    lock_path : str
        Path to the lock file
    timeout : int
        Maximum seconds to wait for the lock (default 30)
    stale_timeout : int
        Seconds after which a lock is considered stale (default 600)
    retry_interval : float
        Seconds between lock acquisition attempts (default 0.5)
    """

    # Per-process reentrancy tracking (path -> count)
    _held_locks = {}

    def __init__(self, lock_path, timeout=30, stale_timeout=600, retry_interval=0.5):
        self.lock_path = lock_path
        self.timeout = timeout
        self.stale_timeout = stale_timeout
        self.retry_interval = retry_interval

    def acquire(self):
        """
        Try to acquire the lock, waiting up to timeout seconds.

        Returns
        -------
        success : bool
            True if lock acquired, False if timeout
        """
        # Reentrant: if this process already holds the lock, just increment
        if self.lock_path in FileLock._held_locks:
            FileLock._held_locks[self.lock_path] += 1
            return True

        start_time = time.time()

        while time.time() - start_time < self.timeout:
            try:
                # Atomic create - fails with FileExistsError if file exists
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'w') as f:
                    json.dump({
                        'pid': os.getpid(),
                        'timestamp': time.time()
                    }, f)
                FileLock._held_locks[self.lock_path] = 1
                return True

            except FileExistsError:
                # Lock file exists - check if it's stale or from a dead process
                try:
                    with open(self.lock_path, 'r') as f:
                        lock_data = json.load(f)

                    lock_pid = lock_data.get('pid', -1)
                    lock_time = lock_data.get('timestamp', 0)

                    # Check for stale lock (exceeded stale_timeout)
                    if time.time() - lock_time > self.stale_timeout:
                        self._force_remove()
                        continue

                    # Check if the locking process is still alive
                    if not psutil.pid_exists(lock_pid):
                        self._force_remove()
                        continue

                except (json.JSONDecodeError, OSError, ValueError):
                    # Corrupted lock file - remove it
                    self._force_remove()
                    continue

            except OSError:
                pass

            time.sleep(self.retry_interval)

        return False

    def release(self):
        """Release the lock. Only removes the file when the reentrancy count reaches zero."""
        if self.lock_path in FileLock._held_locks:
            FileLock._held_locks[self.lock_path] -= 1
            if FileLock._held_locks[self.lock_path] <= 0:
                del FileLock._held_locks[self.lock_path]
                try:
                    # Verify ownership before removing
                    with open(self.lock_path, 'r') as f:
                        lock_data = json.load(f)
                    if lock_data.get('pid') == os.getpid():
                        os.remove(self.lock_path)
                except (json.JSONDecodeError, OSError):
                    # If we can't verify, try to remove anyway
                    try:
                        os.remove(self.lock_path)
                    except OSError:
                        pass

    def _force_remove(self):
        """Force-remove a lock file (stale or corrupted)."""
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock: {self.lock_path}")
        return self

    def __exit__(self, *args):
        self.release()


# ---------------------------------------------------------------------------
# Convenience functions for session data locking
# ---------------------------------------------------------------------------

def get_session_lock_path():
    """Get the path to the session data lock file (.ikabot.lock)."""
    return os.path.join(os.path.expanduser("~"), ".ikabot.lock")


# ---------------------------------------------------------------------------
# Convenience functions for shipping locks
# Matches the naming convention from resourceTransportManager so that ALL
# scripts (built-in and custom modules) coordinate on the same lock files.
#
# Lock file format: .ikabot_shared_{ship_type}_{server}_{username}.lock
# ---------------------------------------------------------------------------

def get_shipping_lock_path(session, use_freighters=False):
    """
    Get the path to the shared shipping lock file.

    Parameters
    ----------
    session : ikabot.web.session.Session
    use_freighters : bool

    Returns
    -------
    lock_file_path : str
    """
    ship_type = "freighters" if use_freighters else "merchant_ships"
    safe_server = session.servidor.replace('/', '_').replace('\\', '_')
    safe_username = session.username.replace('/', '_').replace('\\', '_')
    lock_filename = f".ikabot_shared_{ship_type}_{safe_server}_{safe_username}.lock"
    return os.path.join(os.path.expanduser("~"), lock_filename)


def acquire_shipping_lock(session, use_freighters=False, timeout=300):
    """
    Try to acquire the shipping lock, waiting up to timeout seconds.

    Parameters
    ----------
    session : ikabot.web.session.Session
    use_freighters : bool
    timeout : int

    Returns
    -------
    success : bool
        True if lock acquired, False if timeout
    """
    lock_path = get_shipping_lock_path(session, use_freighters)
    lock = FileLock(lock_path, timeout=timeout, stale_timeout=600, retry_interval=5)
    return lock.acquire()


def release_shipping_lock(session, use_freighters=False):
    """
    Release the shipping lock.

    Parameters
    ----------
    session : ikabot.web.session.Session
    use_freighters : bool
    """
    lock_path = get_shipping_lock_path(session, use_freighters)
    lock = FileLock(lock_path)
    lock.release()
