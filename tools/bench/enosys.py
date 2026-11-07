"""Run a command with Landlock's system calls answering ENOSYS, as on HA OS, whose kernels don't build Landlock.

  python3 tools/bench/enosys.py COMMAND [ARG...]

A seccomp filter (inherited by every process the command starts) answers landlock_create_ruleset, landlock_add_rule
and landlock_restrict_self (444-446 on every architecture) with ENOSYS and lets every other call through, as a
kernel without Landlock answers them. The bench installs it in the container, before Home Assistant or the tests
start: Docker could do the same with a seccomp profile, Apple's container can't, so one way serves both.
"""

import ctypes
import errno
import os
import struct
import sys

_PR_SET_NO_NEW_PRIVS, _PR_SET_SECCOMP, _SECCOMP_MODE_FILTER = 38, 22, 2
_LOAD_NR, _JUMP_IF_EQUAL, _RETURN = 0x20, 0x15, 0x06  # BPF_LD|BPF_W|BPF_ABS, BPF_JMP|BPF_JEQ|BPF_K, BPF_RET|BPF_K.
_ALLOW, _ERRNO = 0x7FFF0000, 0x00050000  # SECCOMP_RET_ALLOW, SECCOMP_RET_ERRNO.


def program():
    """The filter: the call's number (seccomp_data.nr, at offset 0); 444, 445 or 446 answer ENOSYS, the rest pass."""
    return [(_LOAD_NR, 0, 0, 0), (_JUMP_IF_EQUAL, 3, 0, 444), (_JUMP_IF_EQUAL, 2, 0, 445),
            (_JUMP_IF_EQUAL, 1, 0, 446), (_RETURN, 0, 0, _ALLOW), (_RETURN, 0, 0, _ERRNO | errno.ENOSYS)]


def install():
    libc = ctypes.CDLL(None, use_errno=True)
    code = b"".join(struct.pack("=HBBI", *step) for step in program())
    filters = ctypes.create_string_buffer(code, len(code))

    class Program(ctypes.Structure):  # struct sock_fprog.
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]
    fprog = Program(len(program()), ctypes.addressof(filters))
    ulong = ctypes.c_ulong  # prctl's arguments, each a full register.
    # Only root may install a filter without no_new_privs; anyone else sets it first.
    if os.geteuid() != 0 and libc.prctl(_PR_SET_NO_NEW_PRIVS, ulong(1), ulong(0), ulong(0), ulong(0)) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
    if libc.prctl(_PR_SET_SECCOMP, ulong(_SECCOMP_MODE_FILTER), ctypes.byref(fprog), ulong(0), ulong(0)) != 0:
        number = ctypes.get_errno()
        raise OSError(number, f"seccomp: {os.strerror(number)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    install()
    os.execvp(sys.argv[1], sys.argv[1:])
