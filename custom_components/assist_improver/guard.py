"""A replay worker's containment guard (DETAILS P11.8.4): seatbelts that catch a sandbox's accidents. Stand-ins are
what keep a test safe and faithful; this catches what gets past them.

worker.BOOT installs it before Home Assistant is imported. A Python audit hook refuses, raising PermissionError in the
caller: a write outside the worker's folder, starting a process (but Home Assistant's own ffmpeg version check and
Python's library lookups), a signal to any process but the worker itself and those it started (to a group or every
process too: os.killpg, os.kill with 0 or a negative pid; signal.pthread_kill reaches only its own threads, and Linux's
signal.pidfd_send_signal, which raises no audit event, is refused whatever it targets), listening anywhere but in the
folder, and a connection anywhere but a public internet address beyond the host's own networks or the answering
provider's own endpoints (allow). The household's devices, the real Home Assistant and the Supervisor stay unreachable.
Where the kernel has Landlock, it holds the same rule for files, and from its ABI 4 refuses TCP binds: on a Home
Assistant Container install whose host kernel enables it. HA OS's kernels don't build Landlock, so there the audit hook
is the only barrier. The first refusal stops the worker's job as the machinery's fault (worker.Server.answer). Limits:
code in compiled libraries can get past the audit hook (the worker's session of its own keeps a signal to its process
group from Home Assistant), and Python raises no audit event for os.mkfifo, os.mknod or os.memfd_create, so it doesn't
see a pipe or device made outside the folder, nor a file made in memory (Landlock, where it applies, still holds files);
it stops accidents, not malware.
"""

import ctypes
import fcntl
import ipaddress
import os
import signal
import socket
import struct
import sys
from urllib.parse import unquote, urlsplit

FOLDER = None  # The worker's folder, resolved, once the guard is installed: the only place it writes.
ALLOWED = set()  # (address, port): the answering provider's own endpoints (allow).
LINKS = ()  # The networks of the host's own interfaces, once the guard is installed: the household's (_links).
REFUSED = []  # What was refused, in order.
_LOGGED = set()  # The kinds of refusal logged so far: each once.
# _posixsubprocess.fork_exec: subprocess.Popen's own (both events come), and what multiprocessing calls directly.
_PROCESSES = frozenset({"subprocess.Popen", "_posixsubprocess.fork_exec", "os.system", "os.exec", "os.posix_spawn",
                        "os.spawn", "os.fork", "os.forkpty", "pty.spawn", "os.startfile"})
# The processes allowed, which only print: Home Assistant's ffmpeg integration, which text-to-speech depends on (so
# Assist's satellites too), reads ffmpeg's version as it's set up, with its defaults, as here; and Python's
# ctypes.util.find_library on a glibc Linux, which asks ldconfig (then gcc, objdump or ld) where a library is
# (_finding_a_library): Home Assistant's network helpers look up the C library as they load (ifaddr), its camera
# integration libjpeg-turbo's. Home Assistant's own image (musl) patches find_library to search folders itself, starting
# no process, so there only ffmpeg's check is ever started.
_FFMPEG_VERSION = ["ffmpeg", "-version", ""]
# Each event that writes: where its arguments name each path it writes, (path, its dir_fd or None, whether the path's
# last link is followed: a file written through, not the entry itself).
_WRITES = {
    "os.remove": ((0, 1, False),), "os.rmdir": ((0, 1, False),), "os.mkdir": ((0, 2, False),),
    "os.rename": ((0, 2, False), (1, 3, False)), "os.link": ((0, 2, True), (1, 3, False)),
    "os.symlink": ((1, 2, False),), "os.chmod": ((0, 2, True),), "os.chown": ((0, 3, True),),
    "os.truncate": ((0, None, True),), "os.utime": ((0, 3, True),), "os.chflags": ((0, None, True),),
    "os.setxattr": ((0, None, True),), "os.removexattr": ((0, None, True),),
    "shutil.copyfile": ((1, None, True),), "shutil.copymode": ((1, None, True),),
    "shutil.copystat": ((1, None, True),), "shutil.copytree": ((1, None, False),),
    "shutil.move": ((0, None, False), (1, None, False)), "shutil.rmtree": ((0, 1, False),),
    "shutil.chown": ((0, None, True),), "shutil.make_archive": ((0, None, False),),
    "shutil.unpack_archive": ((1, None, False),),
}
_SIGNALS = frozenset({"os.kill", "os.killpg"})
_NETWORK = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind"})
_WATCHED = frozenset({"open", "sqlite3.connect", *_WRITES, *_PROCESSES, *_SIGNALS, *_NETWORK})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LISTEN = socket.socket.listen


def install(folder):
    """In the worker, before Home Assistant is imported, while it has one thread (Landlock holds the threads started
    after it). worker.BOOT has already stopped Python writing bytecode (the improver's code isn't in the folder)."""
    global FOLDER, LINKS
    FOLDER = os.path.realpath(folder)
    # Done first, as at home, what the guard would refuse of what Home Assistant does anyway: its start reads the
    # processor's name (platform runs `uname -p`, once), and urllib3, once imported, probes for IPv6 by binding a socket
    # to ::1 (it never listens). ifaddr (Home Assistant's, for its network helpers) reads the host's networks.
    import platform

    import ifaddr
    import urllib3  # noqa: F401
    platform.uname().processor  # noqa: B018
    LINKS = _links(ifaddr.get_adapters())
    _log(landlock(FOLDER))
    socket.socket.listen = _listening
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal = _pidfd_signal
    sys.addaudithook(_audit)


def _links(adapters):
    """The networks the host's interfaces are on: its own addresses and its neighbours', public ones too (an IPv6 LAN's
    are). A household's IPv6 prefix is at most a /48, so all of that is its own (other rooms' networks)."""
    links = set()
    for adapter in adapters:
        for ip in adapter.ips:
            address = ip.ip if isinstance(ip.ip, str) else ip.ip[0]  # IPv6's: (address, flow, scope).
            link = ipaddress.ip_interface(f"{address}/{ip.network_prefix}").network
            links.add(link.supernet(new_prefix=48) if link.version == 6 and link.prefixlen > 48 else link)
    return tuple(links)


def _listening(sock, *args):
    """socket.socket.listen, in the folder only. Listening needs no bind (the kernel picks a port, or a Unix socket an
    abstract name), so no audit event sees it."""
    if not (sock.family == socket.AF_UNIX and _unix(sock.getsockname())):
        _refuse("listen", f"listen on {sock.getsockname()!r}")
    return _LISTEN(sock, *args)


def _pidfd_signal(pidfd, *args):
    """signal.pidfd_send_signal (Linux's), which raises no audit event: refused, whatever process the pidfd is."""
    _refuse("signal", f"signal a process by pidfd ({pidfd})")


def allow(entry):
    """In the worker, before it loads the household's assistant: the provider's own endpoints become reachable, each
    address its host resolves to now, at its port: the URLs its config entry names (a local model's), and those the
    environment points the provider's library at (an SDK's base URL, a proxy)."""
    if FOLDER is None:  # Not a worker's process: tests serve inline.
        return
    urls = [*_strings([entry.get("data"), entry.get("options")]),
            *(value for name, value in os.environ.items() if name.upper().endswith(("_BASE_URL", "_PROXY")))]
    for url in urls:
        try:
            parts = urlsplit(url.strip())
            host, port = parts.hostname, parts.port or _PORTS.get(parts.scheme)
        except ValueError:
            continue
        if "://" in url and host and port:
            ALLOWED.update((address, port) for address in _addresses(host, port))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _audit(event, args):
    if event not in _WATCHED:
        return
    if event == "open":
        path, mode, flags = args
        # An open file opened again (open(fd)) writes through it only if it was opened to write: checked then.
        if not isinstance(path, int) and (
                flags & _WRITE_FLAGS if flags is not None else any(c in (mode or "") for c in "wax+")):
            _write(path)
    elif event == "sqlite3.connect":
        if (database := _database(args[0])) is not None:
            _write(database)
    elif event in _WRITES:
        for at, dir_fd, follow in _WRITES[event]:
            _write(args[at], args[dir_fd] if dir_fd is not None else None, follow)
    elif event in _PROCESSES:
        what = args[1] if event in ("subprocess.Popen", "_posixsubprocess.fork_exec", "os.posix_spawn") else (
            args[0] if args else "")
        if not (isinstance(what, (list, tuple)) and list(what) == _FFMPEG_VERSION or _finding_a_library()):
            _refuse("process", f"start a process ({event}: {str(what)[:200]})")
    elif event in _SIGNALS:  # Its own pid, or a child's, alone: os.killpg's target is a group, as os.kill's 0 or
        # negative pid is (-1: every process).
        if event == "os.killpg" or args[0] != os.getpid() and not _child(args[0]):
            _refuse("signal", f"signal {'process group' if event == 'os.killpg' else 'process'} {args[0]}")
    elif event == "socket.bind":
        sock, address = args
        if not (sock.family == socket.AF_UNIX and _unix(address)):
            _refuse("listen", f"listen on {address}")
    else:  # A connection, or a datagram sent.
        sock, address = args
        if address is not None and not _reachable(sock.family, address):
            _refuse("connect", f"connect to {address}")


def _write(path, dir_fd=None, follow=True):
    """A write to path: refused outside the folder. A path that's a number is an open file's (chmod, chown, utime...
    through it): where that file is, unless it's a pipe or socket. The null device keeps nothing (a process's output
    thrown away)."""
    if isinstance(path, int) and (path := _fd_path(path)) is None:
        return
    if path not in (os.devnull, os.fsencode(os.devnull)) and not _inside(path, dir_fd, follow):
        _refuse("write", f"write {os.fsdecode(path) if path is not None else '.'}")


def _database(name):
    """The file sqlite3.connect writes, as named: None in memory or read only."""
    name = os.fsdecode(name)
    if name.startswith("file:"):  # A URI (uri=True).
        parts = urlsplit(name)
        if {"mode=memory", "mode=ro"} & set(parts.query.split("&")):
            return None
        name = unquote(parts.path)
    return None if name in ("", ":memory:") else name


def _child(pid):
    """Whether pid is a process the worker started (only what _FFMPEG_VERSION allows), not yet reaped: Home
    Assistant's ffmpeg integration kills its version check if ffmpeg doesn't answer in time. Asked without reaping."""
    if pid <= 0:
        return False
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)  # Running or exited: its child either way.
    except OSError:  # Not its child.
        return False
    return True


def _finding_a_library():
    """Whether ctypes.util.find_library is what starts the process (_FFMPEG_VERSION)."""
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__") != "ctypes.util":
        frame = frame.f_back
    return frame is not None


def _inside(path, dir_fd=None, follow=True):
    """Whether path (relative to dir_fd, or to the working folder) is the worker's folder or in it."""
    path = os.fsdecode(path) if path is not None else "."
    if not os.path.isabs(path):
        base = os.getcwd() if dir_fd in (None, -1) else _fd_path(dir_fd)
        if base is None:
            return False
        path = os.path.join(base, path)
    name = os.path.basename(path)
    real = (os.path.realpath(path) if follow or name in ("", ".", "..")
            else os.path.join(os.path.realpath(os.path.dirname(path)), name))
    return real == FOLDER or real.startswith(FOLDER + os.sep)


def _fd_path(fd):
    """The path of a file or folder open as fd (shutil.rmtree removes by folder fd), or None: a pipe, a socket, or a
    file with no name left (deleted, or made in memory), which keeps nothing anywhere."""
    try:
        path = os.readlink(f"/proc/self/fd/{fd}")  # Linux.
        # A pipe's is "pipe:[...]"; a file with no name left reads as its last path, or "/memfd:...", and " (deleted)".
        return path if path.startswith("/") and os.fstat(fd).st_nlink else None
    except OSError:
        if (get_path := getattr(fcntl, "F_GETPATH", None)) is None:  # macOS's, which has no /proc.
            return None
        try:
            return os.fsdecode(fcntl.fcntl(fd, get_path, bytes(1024)).split(b"\0", 1)[0])
        except OSError:
            return None


def _unix(address):
    """A Unix-domain socket's address in the worker's folder (never the abstract namespace, which has no folder)."""
    return isinstance(address, (str, bytes)) and address[:1] not in ("", b"", "\0", b"\0") and _inside(address)


def _reachable(family, address):
    if family == socket.AF_UNIX:
        return _unix(address)
    if family not in (socket.AF_INET, socket.AF_INET6):  # Netlink, Bluetooth, CAN...: not the internet.
        return False
    port = address[1]
    found = _addresses(address[0], port, family)
    return bool(found) and all((a, port) in ALLOWED or a.is_global and not (a.is_multicast or a.is_reserved)
                               and not any(a in link for link in LINKS) for a in found)


def _addresses(host, port, family=0):
    """The addresses a host is: itself, or each its name resolves to now (none when it doesn't)."""
    try:
        return {_address(host)}
    except ValueError:
        try:
            return {_address(info[4][0]) for info in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)}
        except OSError:
            return set()


def _address(host):
    found = ipaddress.ip_address(host.split("%", 1)[0])  # Without an IPv6 scope.
    if found in _NAT64:  # An IPv4 address reached over IPv6 (NAT64): that address.
        return ipaddress.IPv4Address(int(found) & 0xFFFFFFFF)
    return getattr(found, "ipv4_mapped", None) or found


def _refuse(kind, what):
    why = f"The replay's sandbox may not {what}"
    REFUSED.append(why)
    if kind not in _LOGGED:
        _LOGGED.add(kind)
        _log(why)
    raise PermissionError(why)


def _log(line):
    os.write(2, f"Replay guard: {line}\n".encode(errors="replace"))  # No lock to wait on, whatever runs it.


# Landlock (Linux 5.13 and later). Its system calls have the same numbers on every architecture.
_CREATE_RULESET, _ADD_RULE, _RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_WRITE_FILE, _TRUNCATE = 1 << 1, 1 << 14
# Every right that writes, by the ABI that brought it: write a file; remove a folder or file, make a device, folder,
# file, socket, pipe or link (ABI 1); link or move from another folder (2); truncate (3).
_FS_WRITE = {1: _WRITE_FILE | sum(1 << n for n in range(4, 13)), 2: 1 << 13, 3: _TRUNCATE}
_NET_BIND_TCP = 1  # ABI 4.


def rules(abi, folder):
    """What Landlock is given at an ABI: (the file rights it handles, the network rights it handles, [(path, rights
    allowed beneath it)]). Reads stay unrestricted."""
    handled = sum(rights for since, rights in _FS_WRITE.items() if abi >= since)
    return handled, _NET_BIND_TCP if abi >= 4 else 0, [(folder, handled), ("/dev/null", handled & (
        _WRITE_FILE | _TRUNCATE))]


def landlock(folder, libc=None):
    """Writes outside folder, and from ABI 4 TCP binds, refused by the kernel for this process and the threads it
    starts. Below ABI 2 it refuses any rename or link from one folder to another, even inside folder: nothing in a
    replay makes one (Home Assistant's atomic writes rename within a folder), so ABI 1 is applied too. Skipped on any
    error: not Linux, a kernel that doesn't build Landlock (HA OS's, an old one: ENOSYS) or doesn't enable it
    (EOPNOTSUPP), or a container that refuses the call (seccomp: EPERM or ENOSYS). libc: tests' stand-in. What
    happened, to log."""
    if libc is None:
        if not sys.platform.startswith("linux"):
            return "Landlock skipped: not Linux"
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long

    def call(*args):
        found = libc.syscall(*(ctypes.c_long(a) if isinstance(a, int) else a for a in args))
        if found < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        return found
    try:
        abi = call(_CREATE_RULESET, None, 0, _CREATE_RULESET_VERSION)
    except OSError as err:
        return f"Landlock skipped: {os.strerror(err.errno)}"
    handled, net, allowed = rules(abi, folder)
    attr = struct.pack("=QQ", handled, net) if abi >= 4 else struct.pack("=Q", handled)
    try:
        ruleset = call(_CREATE_RULESET, ctypes.create_string_buffer(attr, len(attr)), len(attr), 0)
        try:
            for path, rights in allowed:
                opened = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
                try:
                    rule = struct.pack("=Qi", rights, opened)
                    call(_ADD_RULE, ruleset, _RULE_PATH_BENEATH, ctypes.create_string_buffer(rule, len(rule)), 0)
                finally:
                    os.close(opened)
            if libc.prctl(_PR_SET_NO_NEW_PRIVS, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0),
                          ctypes.c_ulong(0)) != 0:
                number = ctypes.get_errno()
                raise OSError(number, os.strerror(number))
            call(_RESTRICT_SELF, ruleset, 0)
        finally:
            os.close(ruleset)
    except OSError as err:
        return f"Landlock skipped: {os.strerror(err.errno)}"
    return f"Landlock applied (ABI {abi})"
