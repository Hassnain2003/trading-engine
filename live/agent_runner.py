import sys, os, fcntl
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Single-instance lock: two agents trading the same account simultaneously
# would double positions and corrupt reconciliation. The lock is held for
# the process lifetime and released automatically on any kind of exit.
_LOCK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agent.lock')
_lock_fd = open(_LOCK_PATH, 'w')
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except BlockingIOError:
    print("Another agent instance is already running (agent.lock held) — exiting.")
    sys.exit(3)

from execution.engine import ExecutionEngine

if __name__ == '__main__':
    engine = ExecutionEngine()
    engine.start()
