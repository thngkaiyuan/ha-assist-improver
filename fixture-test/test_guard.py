"""The replay worker's containment guard (DETAILS P11.8.4): in a real worker, a sandbox that writes outside its folder,
starts a process, signals any process but its own and those it started, listens, or connects anywhere but the internet
and its provider's own endpoints is refused, and its replay stops as the machinery's fault, naming what was tried; what
a replay needs goes through, glibc Linux's library lookups and a hung ffmpeg check killed too. Landlock's probe and
rules, with the kernel faked; the real one on the bench (tools/bench)."""

import ctypes
import errno
import ipaddress
import os
import signal
import socket
import struct
import sys
import threading

import pytest

from kit import improver  # noqa: F401 - a fixture.
from recordings import before
from test_worker import POPEN
from test_worker import provider_process, real_worker  # noqa: F401 - fixtures.
from custom_components.assist_improver import guard
from custom_components.assist_improver import worker as worker_module
from custom_components.assist_improver.simulate import replay_recording

# Python run in the worker as the replay's first turn starts, inside its sandbox's loop (what an integration's code
# there would do).
AT_THE_TURN = """from custom_components.assist_improver import sandbox as _sandbox
_run_turn = _sandbox.run_turn
async def _trying(*args, **kwargs):
    import os, signal, socket, subprocess, sys, tempfile
{tries}
    return await _run_turn(*args, **kwargs)
_sandbox.run_turn = _trying
"""
# glibc Linux's ctypes.util.find_library as far as its first step (the suites run on macOS): it asks ldconfig, which
# only prints, then answers as here. Defined in ctypes.util, as there. (Home Assistant's own image, musl, starts none.)
LINUX_FIND_LIBRARY = '''import ctypes.util
ctypes.util._here = ctypes.util.find_library
exec("""def find_library(name):
    try:
        with subprocess.Popen(["/sbin/ldconfig", "-p"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as found:
            found.stdout.read()
    except OSError:
        pass
    return _here(name)""", vars(ctypes.util))
'''
# Linux's signal.pidfd_send_signal, before the guard is installed (the suites run on macOS, which has none).
LINUX_PIDFD = "import signal\nsignal.pidfd_send_signal = lambda *args: None\n"


@pytest.fixture
def listener():
    """A server on 127.0.0.1 in this process, which nothing in the household's setup names."""
    server = socket.create_server(("127.0.0.1", 0))
    server.setblocking(False)
    yield server
    server.close()


@pytest.fixture
def received():
    """Each SIGURG this process (the worker's parent, as Home Assistant is) receives. The sandboxes here send that
    one: no process acts on it unless it asks to, so none they might reach is harmed, and no terminal sends it (as one
    sends SIGWINCH when resized)."""
    found = []
    previous = signal.signal(signal.SIGURG, lambda number, frame: found.append(number))
    yield found
    signal.signal(signal.SIGURG, previous)


async def replayed(improver, provider, monkeypatch, tries):
    """The friction conversation's first turn replayed in a real worker, its sandbox trying this as the turn starts."""
    main = "from custom_components.assist_improver.worker import main"
    lines = "\n".join("    " + line for line in tries.splitlines())
    monkeypatch.setattr(worker_module, "BOOT", worker_module.BOOT.replace(main, AT_THE_TURN.format(tries=lines) + main))
    improver.client = provider
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction)
    return await replay_recording(improver.hass, improver.recorder, "friction01", last_turn=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("tries, refused", [
    ("open({outside!r}, 'w')", "write {outside}"),
    ("subprocess.Popen([sys.executable, '-c', 'pass'])", "start a process (subprocess.Popen: ["),
    ("socket.create_connection(('10.255.255.1', 80), timeout=5)", "connect to ('10.255.255.1', 80)"),
    ("socket.create_connection(('127.0.0.1', {port}), timeout=5)", "connect to ('127.0.0.1', {port})"),
    ("socket.create_connection(('169.254.169.254', 80), timeout=5)", "connect to ('169.254.169.254', 80)"),
    ("socket.create_server(('0.0.0.0', 0))", "listen on ('0.0.0.0', 0)"),
    ("socket.socket().listen()", "listen on ('0.0.0.0', 0)"),
    ("os.kill(os.getppid(), signal.SIGURG)", "signal process {parent}"),
    ("os.killpg(os.getpgid(0), signal.SIGURG)", "signal process group "),
], ids=["a write outside its folder", "a process", "a private address", "a loopback address", "a link-local address",
        "a listening socket", "listening with no bind", "a signal to Home Assistant", "a signal to its process group"])
async def test_what_the_guard_refuses_stops_the_replay_as_the_machinerys_fault_naming_it(
        improver, provider_process, monkeypatch, tmp_path, listener, received, tries, refused):
    outside, port = tmp_path / "outside.txt", listener.getsockname()[1]
    found = await replayed(improver, provider_process, monkeypatch, tries.format(outside=str(outside), port=port))
    assert found["status"] == "infrastructure_failure", found  # The machinery's fault, never a verdict: runs again.
    assert found["error"]["type"] == "Refused"
    assert found["error"]["message"].startswith("The replay's sandbox may not " + refused.format(
        outside=outside, port=port, parent=os.getpid())), found["error"]
    assert not outside.exists()
    with pytest.raises(BlockingIOError):  # Nothing reached this process's server.
        listener.accept()
    assert received == []  # Nor this process (Home Assistant, the worker's parent).


@pytest.mark.asyncio
async def test_a_replay_writes_in_its_folder_signals_itself_and_reaches_the_host_its_providers_entry_names(
        improver, provider_process, monkeypatch, listener, real_worker):
    """A local model's URL in the provider's config entry: that address, at that port, is the sandbox's to reach, as
    the folder is to write in and its own process to signal; the replay goes as recorded."""
    port = listener.getsockname()[1]
    entry = improver.hass.config_entries.async_get_entry(
        improver.friction["turns"][0]["prepared"]["provider_config"][0]["config_entry_id"])
    improver.hass.config_entries.async_update_entry(entry, data={**entry.data, "url": f"http://127.0.0.1:{port}/v1"})
    found = await replayed(improver, provider_process, monkeypatch, "open(tempfile.mkstemp()[1], 'w').write('kept')\n"
                           "os.kill(os.getpid(), signal.SIGURG)\n"
                           f"socket.create_connection(('127.0.0.1', {port}), timeout=5).close()")
    assert found["status"] == "reproduced", found
    listener.accept()[0].close()  # Reached.
    # Installed, with Landlock as the run expects: on Linux, the bench says which (tools/bench: BENCH_LANDLOCK).
    assert any(line.startswith(f"Replay guard: {expected_landlock()}") for line in real_worker[-1].stderr), \
        real_worker[-1].stderr


def expected_landlock():
    """What the guard logs of Landlock in this run: applied (a kernel that has it), skipped as on HA OS, whose kernels
    don't build it (its calls answer ENOSYS), or skipped off Linux."""
    if not sys.platform.startswith("linux"):
        return "Landlock skipped: not Linux"
    expected = os.environ.get("BENCH_LANDLOCK")
    assert expected in ("applied", "missing"), "A Linux run says which Landlock it has: BENCH_LANDLOCK (tools/bench)"
    return "Landlock applied (ABI " if expected == "applied" else f"Landlock skipped: {os.strerror(errno.ENOSYS)}"


@pytest.mark.asyncio
async def test_libraries_are_looked_up_as_on_linux(improver, provider_process, monkeypatch, real_worker):
    """On glibc Linux, ctypes.util.find_library starts ldconfig: Home Assistant's network helpers look up the C
    library as they load (ifaddr), camera libjpeg-turbo's. The worker looks them up so from its start, and the replay
    goes as recorded."""
    install = "from custom_components.assist_improver.guard import install"
    monkeypatch.setattr(worker_module, "BOOT", worker_module.BOOT.replace(install, LINUX_FIND_LIBRARY + install))
    found = await replayed(improver, provider_process, monkeypatch, "import ctypes.util\n"
                           "ctypes.util.find_library('turbojpeg')")
    assert found["status"] == "reproduced", found
    assert not any("may not" in line for line in real_worker[-1].stderr)


@pytest.mark.asyncio
async def test_a_hung_ffmpeg_check_is_killed_and_the_replay_goes_as_recorded(
        improver, provider_process, monkeypatch, tmp_path, real_worker):
    """Home Assistant's ffmpeg integration kills its version check when ffmpeg doesn't answer in time (haffmpeg's
    own, with its timeout cut short here): a process the worker started is its to signal."""
    hung = tmp_path / "bin" / "ffmpeg"
    hung.parent.mkdir()
    hung.write_text("#!/bin/sh\nexec sleep 30\n")
    hung.chmod(0o755)
    monkeypatch.setenv("PATH", f"{hung.parent}{os.pathsep}{os.environ['PATH']}")
    found = await replayed(improver, provider_process, monkeypatch, "from haffmpeg.tools import FFVersion\n"
                           "assert await FFVersion('ffmpeg').get_version(timeout=0.5) is None")
    assert found["status"] == "reproduced", found
    assert not any("may not" in line for line in real_worker[-1].stderr)


@pytest.mark.asyncio
async def test_a_signal_by_pidfd_is_refused_as_on_linux(improver, provider_process, monkeypatch, received):
    """Linux's signal.pidfd_send_signal raises no audit event: the worker's guard replaces it with a refusal, whatever
    process the pidfd is (Home Assistant's, from os.pidfd_open, which raises none either)."""
    install = "from custom_components.assist_improver.guard import install"
    monkeypatch.setattr(worker_module, "BOOT", worker_module.BOOT.replace(install, LINUX_PIDFD + install))
    found = await replayed(improver, provider_process, monkeypatch, "signal.pidfd_send_signal(0, signal.SIGURG)")
    assert found["status"] == "infrastructure_failure" and found["error"]["message"].startswith(
        "The replay's sandbox may not signal a process by pidfd"), found
    assert received == []


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """The guard's rules in this process, over a folder of its own, without installing it: the folder."""
    folder = tmp_path / "worker"
    folder.mkdir()
    monkeypatch.setattr(guard, "FOLDER", os.path.realpath(folder))
    for name, value in (("ALLOWED", set()), ("LINKS", ()), ("REFUSED", []), ("_LOGGED", set())):
        monkeypatch.setattr(guard, name, value)
    return folder


def test_writes_are_the_folders_alone_whichever_way_a_path_reaches_it(fresh, tmp_path):
    (fresh / "out").symlink_to(tmp_path)
    outside = str(tmp_path / "d.db")
    allowed = [("open", (str(fresh / "a.json"), "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)),
               ("os.remove", (str(fresh / "out"), -1)),  # The link itself, not what it points to.
               ("os.rename", (str(fresh / "a"), str(fresh / "b"), -1, -1)),
               ("open", (str(tmp_path / "elsewhere.txt"), "r", os.O_RDONLY)),  # Reads go anywhere.
               ("open", (os.devnull, None, os.O_RDWR)),  # A process's output thrown away.
               ("sqlite3.connect", (str(fresh / "d.db"),)), ("sqlite3.connect", (":memory:",)),
               ("sqlite3.connect", ("",)), ("sqlite3.connect", (f"file:{outside}?mode=ro",)),
               ("sqlite3.connect", ("file:shared?mode=memory&cache=shared",))]
    refused = [("open", (str(fresh / "out" / "x"), "a", os.O_WRONLY | os.O_APPEND | os.O_CREAT)),
               ("os.rmdir", (str(fresh / ".."), -1)), ("os.rename", (str(fresh / "a"), str(tmp_path / "a"), -1, -1)),
               ("shutil.copyfile", (str(fresh / "a"), str(tmp_path / "a"))), ("os.chmod", ("relative", 0o600, -1)),
               ("sqlite3.connect", (outside,)), ("sqlite3.connect", (f"file://{outside}",)),
               ("os.chflags", (str(tmp_path), 0)), ("os.setxattr", (str(tmp_path), "user.x", b"1", 0)),
               ("os.removexattr", (str(tmp_path), "user.x"))]
    folder, elsewhere, (read, write) = os.open(fresh, os.O_RDONLY), os.open(tmp_path, os.O_RDONLY), os.pipe()
    try:
        allowed += [("os.remove", ("a.json", folder)),  # As shutil.rmtree removes, by the folder's fd.
                    ("os.chmod", (folder, 0o700, -1)), ("os.truncate", (write, 0))]  # Through an open file, or pipe.
        refused += [("os.chmod", (elsewhere, 0o777, -1)), ("os.utime", (elsewhere, None, None, -1))]  # Opened to read.
        for event, args in allowed:
            guard._audit(event, args)
        for event, args in refused:
            with pytest.raises(PermissionError, match="The replay's sandbox may not write"):
                guard._audit(event, args)
    finally:
        for fd in (folder, elsewhere, read, write):
            os.close(fd)
    assert len(guard.REFUSED) == len(refused)


def test_a_write_through_an_open_file_is_judged_by_where_linux_says_it_is(fresh, tmp_path, monkeypatch):
    """Linux names an open file in /proc/self/fd (faked here: the suites run on macOS): a file outside the folder is
    refused; one in it, a pipe, and a file with no name left (deleted, or made in memory: memfd_create's) aren't, as
    nothing outside keeps what's written there."""
    readlink, links = os.readlink, {}
    monkeypatch.setattr(os, "readlink", lambda path, **kw: links[int(path.rsplit("/", 1)[1])] if path.startswith(
        "/proc/self/fd/") else readlink(path, **kw))

    def opened(path, link, deleted=False):
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        if deleted:
            os.unlink(path)
        links[fd] = link
        return fd
    (read, write) = os.pipe()
    links[write] = "pipe:[4026]"
    allowed = [opened(fresh / "own", str(fresh / "own")), write,
               opened(tmp_path / "gone", f"{tmp_path / 'gone'} (deleted)", deleted=True),
               opened(tmp_path / "memory", "/memfd:fixture (deleted)", deleted=True)]
    kept = opened(tmp_path / "kept", str(tmp_path / "kept"))
    try:
        for fd in allowed:
            guard._audit("os.truncate", (fd, 0))
        with pytest.raises(PermissionError, match=f"The replay's sandbox may not write {tmp_path / 'kept'}"):
            guard._audit("os.chmod", (kept, 0o600, -1))
    finally:
        for fd in (*allowed, read, kept):
            os.close(fd)
    assert len(guard.REFUSED) == 1


def test_listening_is_in_the_folder_alone(fresh, monkeypatch):
    """A Unix socket bound in the folder listens; one unbound would get an abstract name anyone nearby can reach
    (Linux), and a TCP one a port."""
    monkeypatch.chdir(fresh)  # A Unix socket's path is short (macOS: 104 bytes).
    with socket.socket(socket.AF_UNIX) as inside, socket.socket(socket.AF_UNIX) as unbound, socket.socket() as tcp:
        inside.bind("own.sock")
        guard._listening(inside)
        for sock in (unbound, tcp):
            with pytest.raises(PermissionError, match="The replay's sandbox may not listen on"):
                guard._listening(sock)


def test_the_processes_are_home_assistants_ffmpeg_version_check_and_pythons_library_lookups(fresh):
    """Text-to-speech depends on Home Assistant's ffmpeg integration, which reads ffmpeg's version as it's set up; on
    glibc Linux, ctypes.util.find_library asks ldconfig where a library is."""
    version = ["ffmpeg", "-version", ""]
    guard._audit("subprocess.Popen", ("ffmpeg", tuple(version), None, None))
    guard._audit("_posixsubprocess.fork_exec", ((b"/usr/bin/ffmpeg",), version, None))  # Popen's own, after it.
    guard._audit("os.posix_spawn", ("/usr/bin/ffmpeg", version, {}))
    lookup = {"__name__": "ctypes.util", "audit": guard._audit}
    exec("def find_library():\n    audit('subprocess.Popen', ('/sbin/ldconfig', ['/sbin/ldconfig', '-p'], None, None))",
         lookup)
    lookup["find_library"]()
    for event, args in (("subprocess.Popen", (None, ["ffmpeg", "-i", "rtsp://camera"], None, None)),
                        ("os.posix_spawn", ("/bin/sh", ["sh", "-c", "ffmpeg -version"], {})),
                        # multiprocessing starts its processes so, with no subprocess.Popen event.
                        ("_posixsubprocess.fork_exec", ((b"/usr/bin/python3",), [b"python3", b"-c", b"pass"], None)),
                        ("os.system", ("ffmpeg -version",)), ("os.fork", ())):
        with pytest.raises(PermissionError, match="The replay's sandbox may not start a process"):
            guard._audit(event, args)


def test_a_signal_reaches_the_worker_and_what_it_started_alone(fresh):
    """Its own pid passes, a process it started's until that's reaped (its pid is then free for another's), and
    signal.pthread_kill, which reaches only its own threads. Refused: Home Assistant's pid (its parent's), any other, a
    process group or every process (os.killpg's target, os.kill's 0 or negative pid), and Linux's
    signal.pidfd_send_signal, which raises no audit event, whatever it targets."""
    guard._audit("os.kill", (os.getpid(), signal.SIGTERM))
    guard._audit("signal.pthread_kill", (threading.get_ident(), signal.SIGTERM))
    child = POPEN(["sleep", "30"])
    try:
        guard._audit("os.kill", (child.pid, signal.SIGKILL))  # Running.
    finally:
        child.kill()
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
    guard._audit("os.kill", (child.pid, signal.SIGKILL))  # Ended, not yet reaped.
    child.wait()
    refused = [("os.kill", child.pid), ("os.kill", os.getppid()), ("os.kill", 1), ("os.kill", 0), ("os.kill", -1),
               ("os.kill", -os.getpgid(0)), ("os.killpg", os.getpgid(0)), ("os.killpg", os.getpid())]
    for event, target in refused:
        with pytest.raises(PermissionError, match=f"^The replay's sandbox may not signal process (group )?{target}$"):
            guard._audit(event, (target, signal.SIGTERM))
    with pytest.raises(PermissionError, match=r"^The replay's sandbox may not signal a process by pidfd \(3\)$"):
        guard._pidfd_signal(3, signal.SIGTERM)
    assert len(guard.REFUSED) == len(refused) + 1


def test_connections_reach_the_internet_and_the_providers_own_endpoints_only(fresh, monkeypatch):
    for name in [n for n in os.environ if n.upper().endswith(("_BASE_URL", "_PROXY"))]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8000")  # What an SDK reads.
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.2:3128")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    guard.allow({"data": {"api_key": "sk-fixture", "url": "http://172.20.1.20:11434"},
                 "options": {"base": "https://[fd00::5]/v1"},
                 "subentries": [{"data": {"prompt": "Look at http://172.20.1.30 for help."}}]})
    reached = [(socket.AF_INET, ("1.1.1.1", 443)), (socket.AF_INET6, ("2606:4700::1111", 443, 0, 0)),
               (socket.AF_INET, ("172.20.1.20", 11434)), (socket.AF_INET6, ("fd00::5", 443, 0, 0)),
               (socket.AF_INET, ("127.0.0.1", 8000)), (socket.AF_INET, ("10.0.0.2", 3128)),
               (socket.AF_INET6, ("64:ff9b::101:101", 443, 0, 0))]  # 1.1.1.1 over NAT64.
    unreached = [(socket.AF_INET, ("172.20.1.20", 22)), (socket.AF_INET, ("172.20.1.30", 80)),
                 (socket.AF_INET, ("127.0.0.1", 8123)), (socket.AF_INET, ("10.0.0.1", 80)),
                 (socket.AF_INET, ("172.30.32.2", 80)),  # The Supervisor's.
                 (socket.AF_INET, ("169.254.169.254", 80)), (socket.AF_INET, ("100.100.100.100", 80)),
                 (socket.AF_INET, ("224.0.0.251", 5353)), (socket.AF_INET, ("0.0.0.0", 80)),
                 (socket.AF_INET6, ("::ffff:172.20.1.1", 80, 0, 0)), (socket.AF_INET6, ("fe80::1%lo0", 80, 0, 1)),
                 (socket.AF_INET6, ("::1", 8000, 0, 0)), (socket.AF_INET6, ("::127.0.0.1", 80, 0, 0)),
                 (socket.AF_INET6, ("64:ff9b::ac14:101", 80, 0, 0)),  # 172.20.1.1 over NAT64.
                 (socket.AF_UNIX, "/run/docker.sock"),
                 (socket.AF_UNIX, "\0abstract")]
    assert [address for family, address in reached if not guard._reachable(family, address)] == []
    assert [address for family, address in unreached if guard._reachable(family, address)] == []
    assert guard._reachable(socket.AF_UNIX, str(fresh / "own.sock"))


def test_the_hosts_own_networks_stay_unreached_public_or_not(fresh):
    """An IPv6 LAN's addresses are public ones: the household's devices and the real Home Assistant there, and in the
    household's other networks (its prefix's), stay unreached all the same; a local model its provider's entry names
    is reached. Public resolvers' addresses stand in for the host's."""
    import ifaddr
    eth = [ifaddr.IP("1.1.1.1", 24, "eth0"), ifaddr.IP(("2606:4700:4700::1111", 0, 0), 64, "eth0"),
           ifaddr.IP(("fe80::10", 0, 2), 64, "eth0")]
    guard.LINKS = guard._links([ifaddr.Adapter("eth0", "eth0", eth), ifaddr.Adapter(
        "lo", "lo", [ifaddr.IP("127.0.0.1", 8, "lo"), ifaddr.IP(("::1", 0, 0), 128, "lo")])])
    guard.ALLOWED.add((ipaddress.ip_address("2606:4700:4700::20"), 11434))
    reached = [(socket.AF_INET, ("1.0.0.1", 443)), (socket.AF_INET6, ("2606:4700:4701::1", 443, 0, 0)),
               (socket.AF_INET6, ("2606:4700::1111", 443, 0, 0)),
               (socket.AF_INET6, ("2606:4700:4700::20", 11434, 0, 0))]
    unreached = [(socket.AF_INET, ("1.1.1.1", 8123)), (socket.AF_INET, ("1.1.1.160", 80)),
                 (socket.AF_INET6, ("2606:4700:4700::1111", 8123, 0, 0)),
                 (socket.AF_INET6, ("2606:4700:4700::1", 80, 0, 0)),
                 (socket.AF_INET6, ("2606:4700:4700:ff::30", 80, 0, 0)),
                 (socket.AF_INET6, ("2606:4700:4700::20", 8123, 0, 0))]
    assert [address for family, address in reached if not guard._reachable(family, address)] == []
    assert [address for family, address in unreached if guard._reachable(family, address)] == []


class Kernel:
    """libc for guard.landlock, Landlock's system calls answered here: abi, or the errno each call fails with. What
    was called, decoded (a rule's folder by its path)."""

    def __init__(self, abi=None, error=None):
        self.abi, self.error, self.calls = abi, error, []

    def syscall(self, *args):
        number, *args = [a.value if isinstance(a, ctypes.c_long) else a.raw if isinstance(a, ctypes.Array) else a
                         for a in args]
        if number == 445:  # A rule: its rights, and the path it's open on.
            rights, fd = struct.unpack("=Qi", args[2])
            args[2] = (rights, guard._fd_path(fd))
        self.calls.append((number, *args))
        if self.error:
            ctypes.set_errno(self.error)
            return -1
        if number == 444:
            return self.abi if args[2] == 1 else os.open(os.devnull, os.O_RDONLY)
        return 0

    def prctl(self, *args):
        self.calls.append(("prctl", *(getattr(a, "value", a) for a in args)))
        return 0


@pytest.mark.parametrize("error", [errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP],
                         ids=["seccomp refuses it", "no such call", "turned off"])
def test_landlock_missing_is_skipped(tmp_path, error):
    kernel = Kernel(error=error)
    assert guard.landlock(str(tmp_path), kernel) == f"Landlock skipped: {os.strerror(error)}"
    assert kernel.calls == [(444, None, 0, 1)]  # Only the probe: LANDLOCK_CREATE_RULESET_VERSION.


def test_landlock_is_skipped_off_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert guard.landlock(str(tmp_path)) == "Landlock skipped: not Linux"


# Landlock's rights (linux/landlock.h): every write right of ABI 1 (write a file; remove a folder or file; make a
# device, folder, file, socket, pipe or link), then refer (ABI 2), truncate (3); TCP bind (4).
WRITES = {1: 0x1FF2, 2: 0x3FF2, 3: 0x7FF2, 4: 0x7FF2, 6: 0x7FF2}


@pytest.mark.parametrize("abi", sorted(WRITES))
def test_landlock_holds_writes_to_the_folder_and_from_abi_4_refuses_tcp_binds(tmp_path, abi):
    kernel = Kernel(abi=abi)
    assert guard.landlock(str(tmp_path), kernel) == f"Landlock applied (ABI {abi})"
    probe, create, folder, null, no_new_privs, restrict = kernel.calls
    attr = struct.pack("=QQ", WRITES[abi], 1) if abi >= 4 else struct.pack("=Q", WRITES[abi])
    ruleset = folder[1]
    assert probe == (444, None, 0, 1) and create == (444, attr, len(attr), 0)
    assert folder == (445, ruleset, 1, (WRITES[abi], str(tmp_path.resolve())), 0)  # LANDLOCK_RULE_PATH_BENEATH.
    assert null == (445, ruleset, 1, (0x4002 if abi >= 3 else 0x2, "/dev/null"), 0)  # Write, and truncate.
    assert no_new_privs == ("prctl", 38, 1, 0, 0, 0) and restrict == (446, ruleset, 0)
