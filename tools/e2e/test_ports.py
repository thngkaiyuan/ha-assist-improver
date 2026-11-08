"""The harness's ports (conftest.free_port), checked without a Home Assistant."""

from conftest import _EPHEMERAL, _share


def test_every_worker_draws_from_10081_to_below_the_systems_range():
    """xdist starts n workers, then up to 4n more in place of crashed ones, numbered on (gw<n>, gw<n+1>, ...): each
    one's share lies above the ports browsers refuse and below the range the system hands out to bind(0)."""
    for workers in (1, 2, 3, 5, 8, 21):
        for worker in range(5 * workers):
            ports = _share(worker, workers)
            assert 10081 <= ports[0] and ports[-1] < _EPHEMERAL, (worker, workers, ports)
