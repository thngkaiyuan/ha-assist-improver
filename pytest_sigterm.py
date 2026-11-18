"""A pytest plugin, `-p pytest_sigterm` (tools/check.sh passes it to every suite, tools/bench/haos.sh to the journeys):
being stopped (SIGTERM) is an interrupt, so a suite still ends with its result so far, and the journeys stop each Home
Assistant they started. As a container's PID 1 (on the bench), pytest gets a signal only through a handler. Not in
tools/: Home Assistant's image has a tools package of its own, which would shadow ours."""
import signal

signal.signal(signal.SIGTERM, signal.default_int_handler)
