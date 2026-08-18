"""Test fixtures.

Nothing here uses a real account id, household id or list uuid — this repo is
public, and the soak harness that produced the real ones is not.

There is deliberately no Home Assistant test harness loaded. Every test so far
covers api.py, which does not import Home Assistant, and merely registering the
plugin starts a Home Assistant thread that outlives its own cleanup check —
failing a suite in which everything passed. It comes back with the first config
flow or coordinator test, which will genuinely need it.
"""
