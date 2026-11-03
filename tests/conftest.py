import pytest


@pytest.fixture(autouse=True)
def _reset_worker_state():
    """A worker marked down, or an outage already reported, in one test must not leak into the next."""
    try:
        from core import worker_client, worker_restart
    except Exception:
        yield
        return
    worker_client._DOWN_UNTIL.clear()
    worker_restart._outage["requested"] = False
    yield
    worker_client._DOWN_UNTIL.clear()
    worker_restart._outage["requested"] = False
