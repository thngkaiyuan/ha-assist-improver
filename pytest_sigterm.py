"""`-p pytest_sigterm` (tools/check.sh, tools/bench/haos.sh): being stopped (SIGTERM) is an interrupt, so a suite
ends with its result so far, also as a container's PID 1. At the root: HA's image has a tools package of its own."""
import signal

signal.signal(signal.SIGTERM, signal.default_int_handler)
