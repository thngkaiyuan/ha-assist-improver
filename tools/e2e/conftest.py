"""The end-to-end suite's harness: a real Home Assistant server running Assist Improver, driven through Home
Assistant's real frontend in a headless browser, with only the AI model stood in (fake_anthropic.py).

Fixtures (see each one's docstring):
- fake_model (session): the stand-in Anthropic Messages API on 127.0.0.1, answering Home Assistant's real anthropic
  integration (ANTHROPIC_BASE_URL points the HA process, and its replay workers, at it).
- golden (session): a throwaway Home Assistant set up once (once per pytest-xdist worker, each in its own runs folder,
  for its own tests), as a household would set it up: onboarded with a test user, the simulated devices
  (config/configuration.yaml) in their rooms and exposed to Assist, the anthropic integration added through its config
  flow (the fake answers its key check) with its assistant on a model chosen explicitly, Assist using that assistant,
  and Assist Improver added through its own config flow (its AI roles on the anthropic AI Tasks it sets up). Then
  it's stopped, its log checked as each test's is (below), and its config folder is where every test's Home Assistant
  starts from.
- ha (function): a fresh Home Assistant from the golden copy, on its own free port, logged in; stopped, with every
  process it started, when the test ends, and its log checked: the test fails if Home Assistant logged an error of
  Assist Improver's or caught a blocking call in its code on the event loop (tools/bench/logcheck.py). One per test,
  so no journey sees another's conversations or improvements; it starts in a few seconds. Its helpers
  (HomeAssistant) read and change Home Assistant's state over REST and the websocket API.
- page (function, parametrized): a headless Chromium page logged in to that Home Assistant, at phone size (402x874,
  the design target) and desktop size (1280x800): every test that takes `page` runs at both
  (narrow one with @pytest.mark.parametrize("page", ["phone"], indirect=True)). Its helpers (Screen) do what the
  household does: talk to Assist in its dialog, open the panel, find a conversation, read a chip.

The harness keeps to the machine: every server binds 127.0.0.1 only, on free ports (Home Assistant's HTTP settings
are written to its store before each start, see _set_port); the browser is Playwright's headless Chromium from
tools/ui/browsers, and it refuses any request beyond 127.0.0.1; Home Assistant runs on the project's uv-managed .venv;
no model call is paid. The test user's password lives only in the golden fixture's memory and Home Assistant's own
auth store. Every wait is on a condition (Playwright's expect, or polling Home Assistant), never a fixed sleep.

On the bench (tools/bench), each Home Assistant runs in its official image instead (BENCH_IMAGE), as the Supervisor
runs it (tools/bench/ha.sh), its config folder mounted as /config. On Docker it shares this machine's network, as
above. On Apple's container each one is a small VM of its own: Home Assistant listens on the VM's interfaces and is
published on 127.0.0.1, and the fake model listens on the container network's address for this machine (BENCH_HOST),
which only this machine and its containers reach.

On HA OS (tools/bench/haos.sh), Home Assistant is HA OS's own, in a VM, its Supervisor starting and stopping it
(over ssh, BENCH_HAOS: ssh's config for the VM, as `haos`): each config folder is copied in as its /config before it
starts and back after it stops, and read there while it runs (HomeAssistant.read). It listens on 8123, forwarded from
127.0.0.1, and reaches the fake model at the VM's address for this machine (e2e_support's model_url).

Environment: E2E_RUNS_DIR (where config folders go; a temp folder by default), E2E_KEEP=1 (keep them), E2E_SHOTS=1
(Screen.shot saves screenshots), E2E_HA_PYTHON (Home Assistant's Python; the checkout's .venv by default),
PLAYWRIGHT_BROWSERS_PATH (tools/ui/browsers by default), IMPROVER_COVERAGE (tools/check.sh --coverage: the panel's
JavaScript is measured too, tools/ui/jscov.py), and the bench's BENCH_IMAGE, BENCH_RUNTIME, BENCH_HOST and
BENCH_LANDLOCK (tools/bench/run.sh sets them), or BENCH_HAOS (tools/bench/haos.sh). A failing test keeps its Home
Assistant log, a screenshot and the fake model's requests for that test (e2e-model-requests.jsonl) in its folder, and
prints where.
"""

import atexit
import base64
import itertools
import json
import os
import random
import re
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # The checkout under test.
sys.path.insert(0, str(HERE))
sys.path.append(str(ROOT / "tools/ui"))
sys.path.append(str(ROOT / "tools/bench"))
from fake_anthropic import FakeAnthropic  # noqa: E402
import jscov  # noqa: E402
import logcheck  # noqa: E402


def _find(relative):
    """A path in this checkout, or in the checkout a worktree hangs from (its .venv and browsers aren't copied)."""
    for base in (ROOT, *ROOT.parents):
        if (base / relative).exists():
            return base / relative
    return None


HA_PYTHON = Path(os.environ.get("E2E_HA_PYTHON") or _find(".venv/bin/python") or ".venv/bin/python")
IMAGE = os.environ.get("BENCH_IMAGE")  # Home Assistant's official image, through tools/bench/ha.sh, instead.
OWN_VM = bool(IMAGE) and os.environ.get("BENCH_RUNTIME") == "container"  # Each a VM of its own: published ports.
MODEL_HOST = os.environ.get("BENCH_HOST") or "127.0.0.1"  # Where Home Assistant reaches the fake model.
HAOS = os.environ.get("BENCH_HAOS")  # HA OS in a VM instead: ssh's config for it (tools/bench/haos.sh).
HAOS_CONFIG = "/mnt/data/supervisor/homeassistant"  # Home Assistant's /config there.
HAOS_HOST = "10.0.2.2"  # This machine, from the VM (QEMU's user network).
HAOS_BUSY = 300  # Seconds an `ha core` command waits for the Supervisor's job on Home Assistant to end (_ha_core).
BROWSERS = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(_find("tools/ui/browsers") or "")
if BROWSERS:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS
from playwright.sync_api import expect, sync_playwright  # noqa: E402

PHONE = {"width": 402, "height": 874}  # The design target.
DESKTOP = {"width": 1280, "height": 800}
SIZES = {"phone": PHONE, "desktop": DESKTOP}
CONVERSATION_TIMEOUT = 6  # Seconds an idle chat session lasts in the throwaway HA (e2e_support).
ASSISTANT_MODEL = "claude-haiku-4-5"  # The household's assistant's model, chosen explicitly (assistant_model).
AREAS = {"Study": ["light.desk_lamp"], "Living room": ["light.ceiling_light", "light.floor_lamp"],
         "Kitchen": ["switch.kettle"]}
DEVICES = [e for entities in AREAS.values() for e in entities]  # Exposed to Assist; their helpers aren't.
HELPERS = ["input_boolean.desk_lamp_power", "input_boolean.ceiling_light_power", "input_boolean.floor_lamp_power",
           "input_boolean.kettle_power"]
STARTUP_SECONDS = 180
EXPECT_TIMEOUT = 60_000  # ms: the longest a screen waits for the pipeline (a run takes seconds with the fake).
expect.set_options(timeout=EXPECT_TIMEOUT)

_RUNNING = set()  # Home Assistant processes still running: stopped at exit, whatever happens.
_NUMBERS = itertools.count(1)  # Each test's config folder, numbered in the order the tests ran.
# Being stopped (SIGTERM: tools/check.sh stops a suite so) is an interrupt, so the session still stops every Home
# Assistant it started.
signal.signal(signal.SIGTERM, signal.default_int_handler)


@atexit.register
def _stop_all():
    for process in list(_RUNNING):
        _stop_process(process)


_WORKER = int(os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw"))  # With -n: gw0, gw1, ...
_WORKERS = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1"))
# The first port of the range the system hands out to bind(0) and outgoing connections: Linux's own setting (32768 by
# default); elsewhere 32768, below macOS's 49152.
_EPHEMERAL = (int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0]) if sys.platform == "linux"
              else 32768)


def _share(worker, workers):
    """The ports worker `worker` of `workers` draws from (free_port): its share of 10081 up to the system's range, by
    its number modulo theirs, as xdist numbers a worker started in place of a crashed one past the last."""
    size = (_EPHEMERAL - 10081) // workers
    if size < 1:
        raise RuntimeError(f"The system hands out ports from {_EPHEMERAL}: none left above 10080 for {workers} workers")
    start = 10081 + size * (worker % workers)
    return range(start, start + size)


def free_port():
    """A port for a Home Assistant: on HA OS its own, 8123; else a free one at random from this worker's share of the
    ports from 10081 up to the range the system hands out to bind(0) and outgoing connections (_EPHEMERAL): above the
    ports browsers refuse (ERR_UNSAFE_PORT; the highest is 10080), below that range. One from that range could go to
    another process (another suite's server: the bench's suites share the machine's network) in the tens of seconds
    before Home Assistant binds it on a slow machine, and that process would get Home Assistant's requests. Shares keep
    a run's workers apart; at random, two runs on one machine rarely meet."""
    if HAOS:
        return 8123
    ports = _share(_WORKER, _WORKERS)
    for _ in range(100):
        port = random.choice(ports)
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:  # Taken.
                continue
        return port
    raise RuntimeError("No free port for a Home Assistant")


def haos(command, **kwargs):
    """A shell command on HA OS's host, over ssh (BENCH_HAOS)."""
    return subprocess.run(["ssh", "-F", HAOS, "haos", command], check=True, **kwargs)


def _haos_push(folder):
    """The config folder, copied in as Home Assistant's /config on HA OS (stopped), for nothing else to be there."""
    with subprocess.Popen(["tar", "-C", str(folder), "--exclude=./e2e-*", "-cf", "-", "."],
                          stdout=subprocess.PIPE) as tar:
        haos(f"cd {HAOS_CONFIG} && rm -rf ./* ./.[!.]* && tar -xf -", stdin=tar.stdout)
    assert tar.returncode == 0, tar.returncode


def _haos_pull(folder):
    """Home Assistant's /config on HA OS (stopped), copied back into the folder, but for the harness's own files."""
    for path in folder.iterdir():
        if not path.name.startswith("e2e-"):
            shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    with subprocess.Popen(["tar", "-C", str(folder), "-xf", "-"], stdin=subprocess.PIPE) as tar:
        haos(f"tar -C {HAOS_CONFIG} -cf - .", stdout=tar.stdin)
        tar.stdin.close()
    assert tar.wait() == 0, tar.returncode


def wait_until(check, timeout=60, interval=0.25, what="the condition"):
    """Poll check() until it returns something truthy (returned), or fail after timeout seconds."""
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            if result := check():
                return result
        except Exception as err:  # noqa: BLE001 - not yet: Home Assistant may still be starting.
            last = err
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout} s waiting for {what}"
                         + (f" (last error: {last!r})" if last else ""))


# HTTP and the websocket API, with the standard library only (the runner needs only playwright and pytest).

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 only: never through a proxy.


def http(method, url, body=None, token=None, form=False, ok=(200, 201)):
    """One request to Home Assistant: the decoded JSON reply (or text), raising on a status not in ok."""
    data, headers = None, {}
    if body is not None:
        data = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=30) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as err:
        status, text = err.code, err.read().decode()
    if status not in ok:
        raise RuntimeError(f"{method} {url}: {status} {text[:500]}")
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


class WebSocket:
    """Home Assistant's websocket API (RFC 6455 over a plain socket): call(type, **fields) returns a command's result,
    raising with Home Assistant's error. Thread-safe; one connection per Home Assistant."""

    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("Home Assistant closed the websocket handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.decode(errors="replace"))
        self.lock, self.ids = threading.Lock(), iter(range(1, 1 << 30))
        assert self._recv()["type"] == "auth_required"
        self._send({"type": "auth", "access_token": token})
        reply = self._recv()
        if reply["type"] != "auth_ok":
            raise ConnectionError(f"Websocket auth failed: {reply}")

    def _send(self, message):
        payload = json.dumps(message).encode()
        header = bytes([0x81])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _exactly(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Home Assistant closed the websocket")
            data += chunk
        return data

    def _recv(self):
        message = b""
        while True:
            first, second = self._exactly(2)
            opcode, n = first & 0x0F, second & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._exactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._exactly(8))[0]
            data = self._exactly(n)
            if opcode == 0x8:
                raise ConnectionError("Home Assistant closed the websocket")
            if opcode == 0x9:  # Ping: answered, and keep reading.
                self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + b"\0\0\0\0" + data)
                continue
            message += data
            if first & 0x80:
                return json.loads(message)

    def call(self, type, **fields):
        with self.lock:
            number = next(self.ids)
            self._send({"id": number, "type": type, **fields})
            while True:
                reply = self._recv()
                if reply.get("id") == number and reply.get("type") == "result":
                    if not reply.get("success"):
                        raise RuntimeError(f"{type}: {reply.get('error')}")
                    return reply.get("result")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# Home Assistant's process.

def _write_config(folder, port, model_url):
    """The throwaway config (config/), with the checkout's Assist Improver copied in."""
    shutil.copytree(HERE / "config", folder, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    text = (HERE / "config/configuration.yaml").read_text()
    (folder / "configuration.yaml").write_text(
        text.replace("{port}", str(port)).replace("{conversation_timeout}", str(CONVERSATION_TIMEOUT))
        .replace("{model_url}", model_url.replace("//127.0.0.1:", f"//{HAOS_HOST}:") if HAOS else ""))
    shutil.copytree(ROOT / "custom_components/assist_improver", folder / "custom_components/assist_improver",
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))


def _set_port(folder, port):
    """Home Assistant listens on 127.0.0.1:port only (in a VM of its own, on every interface: its own address
    and the Supervisor's network reach it there). Home Assistant 2026.10 keeps its HTTP settings in .storage/http
    (YAML is migrated once, as a trial that falls back to the defaults, every interface on 8123, after five minutes),
    so the store is written before every start: the settings as confirmed ("stable"), with no trial pending."""
    path = folder / "configuration.yaml"
    path.write_text("\n".join(f"  internal_url: http://127.0.0.1:{port}" if line.strip().startswith("internal_url:")
                              else line for line in path.read_text().splitlines()) + "\n")
    host = "0.0.0.0" if OWN_VM or HAOS else "127.0.0.1"
    stable = {"server_host": [host], "server_port": port, "cors_allowed_origins": [], "login_attempts_threshold": -1,
              "ip_ban_enabled": False, "ssl_profile": "modern", "use_x_frame_options": True,
              "created_at": "2026-01-01T00:00:00+00:00", "error": None, "error_message": None}
    store = folder / ".storage" / "http"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"version": 2, "minor_version": 2, "key": "http",
                                 "data": {"stable": stable, "pending": None, "yaml_migration_done": True}}, indent=2))


def _onboarded(folder):
    """Onboarding marked done. Its core_config step would add integrations that reach the internet (weather, Google's
    text-to-speech), so the steps after the user's are recorded as done here, while Home Assistant is stopped, rather
    than run."""
    path = folder / ".storage" / "onboarding"
    stored = json.loads(path.read_text())
    stored["data"]["done"] = ["user", "core_config", "analytics", "integration"]
    path.write_text(json.dumps(stored, indent=2))


def _start_process(folder, model_url, port):
    """Home Assistant on the config folder, in a session of its own (_stop_process). SETUP_PORT makes Home Assistant's
    last-resort HTTP default (recovery mode) the same port, never 8123. That default listens on every interface, so
    only losing the port to another listener between free_port() and Home Assistant binding it could reach it. On the
    bench, Home Assistant's image instead (its container's lifetime is the process's), in the household's time zone,
    as the Supervisor gives Core the system's; or HA OS's own, the folder copied in first, started by its Supervisor
    (the process follows its log until it stops)."""
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    own = dict(ANTHROPIC_BASE_URL=model_url, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
               SETUP_PORT=str(port))
    log = open(folder / "e2e-hass.log", "ab")
    stop = None
    if HAOS:
        _haos_push(folder)
        command = ["ssh", "-F", HAOS, "haos",
                   f"since=$(date +%s); {_ha_core('start')}; exec docker logs -f --since $since homeassistant"]
        stop = ["ssh", "-F", HAOS, "haos", _ha_core("stop")]
    elif IMAGE:
        name = f"assist-improver-e2e-{os.getpid()}-{port}"
        command = [str(ROOT / "tools/bench/ha.sh"), "run", name, "--config", str(folder), "--port", str(port),
                   "--cpus", "2", "--memory", "3G", "-e", "TZ=Asia/Singapore",
                   *(part for item in own.items() for part in ("-e", "=".join(item)))]
        stop = [str(ROOT / "tools/bench/ha.sh"), "stop", name]
    else:
        command = [str(HA_PYTHON), "-m", "homeassistant", "--config", str(folder), "--skip-pip"]
        env.update(own, PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True,
                               cwd=folder)
    process.log, process.stop, process.folder = log, stop, folder
    _RUNNING.add(process)
    return process


def _ha_core(command):
    """`ha core <command>` on HA OS's host, as a shell command. While its Supervisor runs a job on Home Assistant (a
    start goes on until it sees Home Assistant running, which it checks every 5 s), it refuses another: tried again
    every 5 s, for up to HAOS_BUSY seconds."""
    return (f'i=0; until out=$(ha core {command} 2>&1); do case $out in *"Another job is running"*) '
            f'[ $((i += 5)) -le {HAOS_BUSY} ] && sleep 5 && continue;; esac; echo "$out" >&2; exit 1; done')


def _stop_process(process, timeout=45):
    """Stop Home Assistant gently, then anything left in its process group by force. Its replay workers, each in a
    session of its own, end with it: their input closes. On the bench, its container is stopped (s6 stops Home
    Assistant gently); on HA OS, its Supervisor stops it, and its /config is copied back into the folder."""
    if process.stop and (HAOS or process.poll() is None):  # HA OS's Home Assistant may outlive its log's ssh.
        subprocess.run(process.stop, timeout=timeout + 15 + (HAOS_BUSY if HAOS else 0), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    if process.poll() is None:
        try:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.1)
    if process.poll() is None:
        process.kill()
        process.wait(10)
    process.log.close()
    _RUNNING.discard(process)
    if HAOS:
        _haos_pull(process.folder)


def _client_id(port):
    return f"http://127.0.0.1:{port}/"


def _login(port, login):
    """The test user's tokens for this Home Assistant (its login flow, as the frontend logs in)."""
    base, client = f"http://127.0.0.1:{port}", _client_id(port)
    flow = http("POST", f"{base}/auth/login_flow", {"client_id": client, "handler": ["homeassistant", None],
                                                     "redirect_uri": client})
    done = http("POST", f"{base}/auth/login_flow/{flow['flow_id']}",
                {"client_id": client, "username": login["username"], "password": login["password"]})
    return http("POST", f"{base}/auth/token", {"grant_type": "authorization_code", "code": done["result"],
                                               "client_id": client}, form=True)


class HomeAssistant:
    """One running throwaway Home Assistant: its port, config folder, tokens, and helpers to read its state.

    - url: http://127.0.0.1:<port>; folder: its config folder; tokens: the test user's (access_token, ...); since:
      how many requests the fake model had answered when it started (fake_model.roles(ha.since): who it asked since).
    - ws.call(type, **fields): a websocket command; rest(method, path, body): a REST call.
    - state(entity_id): the state object (None if there's none); entity(entity_id): its entity registry entry.
    - aliases(entity_id): the entity's other names (aliases); exposed(entity_id): whether Assist may see it.
    - options(): Assist Improver's options (as saved); storage(key): a .storage file's data, read from disk.
    - wait(check, timeout, what): poll check() until truthy (wait_until).
    - assistant_model(model): the household's assistant on that model, or (None) on HA's recommended settings;
      assistant_instructions(text): those lines added to its instructions.
    - run(): Assist Improver's current or last run; wait_for_run(trigger): one with that trigger, once finished.
    """

    def __init__(self, folder, port, process, login):
        self.folder, self.port, self.process = folder, port, process
        self.url = f"http://127.0.0.1:{port}"
        self.tokens = _login(port, login)
        self.ws = WebSocket(port, self.tokens["access_token"])

    def rest(self, method, path, body=None, **kwargs):
        return http(method, self.url + path, body, token=self.tokens["access_token"], **kwargs)

    def state(self, entity_id):
        return self.rest("GET", f"/api/states/{entity_id}", ok=(200, 404)) if entity_id else None

    def entity(self, entity_id):
        return self.ws.call("config/entity_registry/get", entity_id=entity_id)

    def aliases(self, entity_id):
        """The entity's aliases besides its own name (the registry lists that as None: Assist knows it by its name)."""
        return sorted(a for a in self.entity(entity_id).get("aliases") or [] if a)

    def exposed(self, entity_id):
        listed = self.ws.call("homeassistant/expose_entity/list")["exposed_entities"]
        return bool((listed.get(entity_id) or {}).get("conversation"))

    def entries(self, domain=None):
        return [e for e in self.rest("GET", "/api/config/config_entries/entry") if domain in (None, e["domain"])]

    def options(self):
        """Assist Improver's options as Home Assistant last saved them (.storage/core.config_entries)."""
        entries = (self.storage("core.config_entries") or {}).get("entries") or []
        return next((e.get("options") for e in entries if e["domain"] == "assist_improver"), None)

    def conversations(self):
        """The rows Assist Improver lists: the list's, then the archived ones it shows below Show archived."""
        listed = lambda **fields: self.ws.call("assist_improver/conversations", **fields)["conversations"]
        return [*listed(), *listed(archived=True)]

    def read(self, relative):
        """A file of its config folder as it is now (on HA OS, read there), or None."""
        if HAOS:
            done = subprocess.run(["ssh", "-F", HAOS, "haos", f"cat {HAOS_CONFIG}/{shlex.quote(relative)}"],
                                  capture_output=True, text=True, errors="replace", check=False)
            return done.stdout if done.returncode == 0 else None
        path = self.folder / relative
        return path.read_text(errors="replace") if path.exists() else None

    def storage(self, key):
        """A store's data as Home Assistant last wrote it (.storage/<key>), or None."""
        text = self.read(f".storage/{key}")
        return json.loads(text)["data"] if text is not None else None

    def wait(self, check, timeout=60, what="the condition"):
        return wait_until(check, timeout=timeout, what=what)

    def assistant_model(self, model):
        """The household's assistant (the anthropic conversation agent Assist uses) set up through its Reconfigure
        screen, as the household would: on an explicitly chosen model (Recommended model settings off, then the
        model), or, with model None, on Home Assistant's recommended model settings. Waits for anthropic to reload."""
        self._reconfigure_assistant(init={"recommended": model is None, **({"chat_model": model} if model else {})})

    def assistant_instructions(self, text):
        """The household's assistant's instructions, set through its Reconfigure screen as the household would: these
        lines after those it has. Waits for anthropic to reload."""
        self._reconfigure_assistant(init=lambda form: {"prompt": f"{_suggested(form, 'prompt') or ''}\n{text}".strip()})

    def _reconfigure_assistant(self, **steps):
        """The anthropic conversation agent's Reconfigure flow, answered as its forms suggest, with each step's
        overrides (a dict, or a function of the form)."""
        entry = self.entries("anthropic")[0]["entry_id"]
        subentry = next(s for s in self.ws.call("config_entries/subentries/list", entry_id=entry)
                        if s["subentry_type"] == "conversation")
        result = self.rest("POST", "/api/config/config_entries/subentries/flow",
                           {"handler": [entry, "conversation"], "subentry_id": subentry["subentry_id"]})
        for _ in range(6):
            if result.get("type") != "form":
                break
            overrides = steps.get(result["step_id"]) or {}
            reply = _answer(result, **(overrides(result) if callable(overrides) else overrides))
            result = self.rest("POST", f"/api/config/config_entries/subentries/flow/{result['flow_id']}", reply)
        assert result.get("type") == "abort" and result.get("reason") == "reconfigure_successful", result
        self.wait(lambda: all(e["state"] == "loaded" for e in self.entries("anthropic")), what="anthropic to reload")

    def run(self):
        """Assist Improver's run in progress, or its last one (assist_improver/run: trigger, status, ...)."""
        return self.ws.call("assist_improver/run")["run"]

    def wait_for_run(self, trigger, timeout=60):
        """The first run with this trigger (filed: the fast lane; manual; revision; ...) once it has finished."""
        return self.wait(lambda: (run := self.run()) and run.get("trigger") == trigger
                         and run.get("status") != "running" and run, timeout=timeout,
                         what=f"a {trigger} run to finish")

    def log(self):
        return self.read("home-assistant.log") or ""


def _ready(port):
    """Home Assistant is up and every integration has started."""
    try:
        http("GET", f"http://127.0.0.1:{port}/manifest.json")
        return True
    except Exception:  # noqa: BLE001 - not listening yet.
        return False


def _running(ha):
    return ha.ws.call("get_config").get("state") == "RUNNING"


def _flow(ha, handler, answer=None):
    """A config flow through Home Assistant's REST API, as the integrations page drives it: each form answered by
    answer(form) (its defaults, by default). The final result."""
    result = ha.rest("POST", "/api/config/config_entries/flow", {"handler": handler, "show_advanced_options": False})
    for _ in range(6):
        if result.get("type") != "form":
            return result
        reply = answer(result) if answer else {f["name"]: f["default"] for f in result.get("data_schema") or []
                                               if "default" in f}
        result = ha.rest("POST", f"/api/config/config_entries/flow/{result['flow_id']}", reply)
    return result


def _suggested(form, name):
    """What a form suggests for a field (its current value, on a Reconfigure form)."""
    field = next((f for f in form.get("data_schema") or [] if f["name"] == name), {})
    return (field.get("description") or {}).get("suggested_value", field.get("default"))


def _answer(form, **overrides):
    """A form's answer: each field as the form suggests or defaults it, with these overrides."""
    answer = {}
    for field in form.get("data_schema") or []:
        suggested = (field.get("description") or {}).get("suggested_value")
        if field["name"] in overrides:
            answer[field["name"]] = overrides[field["name"]]
        elif suggested is not None:
            answer[field["name"]] = suggested
        elif "default" in field:
            answer[field["name"]] = field["default"]
    return answer


def _set_up_household(ha, api_key):
    """The household's setup, as they'd do it in the UI: rooms, devices in them and exposed to Assist (helpers not),
    the anthropic integration, Assist on its conversation agent, then Assist Improver."""
    areas = {a["name"].casefold(): a["area_id"] for a in ha.ws.call("config/area_registry/list")}  # Onboarding's too.
    for name, entities in AREAS.items():
        area_id = areas.get(name.casefold()) or ha.ws.call("config/area_registry/create", name=name)["area_id"]
        for entity_id in entities:
            ha.ws.call("config/entity_registry/update", entity_id=entity_id, area_id=area_id)
    ha.ws.call("homeassistant/expose_entity", assistants=["conversation"], entity_ids=DEVICES, should_expose=True)
    ha.ws.call("homeassistant/expose_entity", assistants=["conversation"], entity_ids=HELPERS, should_expose=False)

    result = _flow(ha, "anthropic", lambda form: {"api_key": api_key})
    assert result["type"] == "create_entry", result
    agents = ha.wait(lambda: [e for s in ha.rest("GET", "/api/states") if (e := s["entity_id"]).startswith(
        "conversation.") and e != "conversation.home_assistant"], what="the anthropic conversation agent")
    ha.wait(lambda: any(s["entity_id"].startswith("ai_task.") for s in ha.rest("GET", "/api/states")),
            what="the anthropic AI Task")
    ha.assistant_model(ASSISTANT_MODEL)  # Chosen explicitly.
    pipelines = ha.ws.call("assist_pipeline/pipeline/list")
    preferred = next(p for p in pipelines["pipelines"] if p["id"] == pipelines["preferred_pipeline"])
    fields = {k: v for k, v in preferred.items() if k != "id"}
    ha.ws.call("assist_pipeline/pipeline/update", pipeline_id=preferred["id"],
               **{**fields, "conversation_engine": agents[0], "conversation_language": "en", "language": "en",
                  "prefer_local_intents": False})

    result = _flow(ha, "assist_improver")
    if result.get("type") == "menu":  # A weak choice: use it anyway (never expected with the recommended entry).
        result = ha.rest("POST", f"/api/config/config_entries/flow/{result['flow_id']}", {"next_step_id": "use_anyway"})
    assert result["type"] == "create_entry", result


@pytest.fixture(scope="session")
def fake_model():
    """The stand-in Anthropic Messages API (fake_anthropic.FakeAnthropic), on 127.0.0.1, for the whole session. Its
    requests log says who was asked what; a failing test's report gets the part of it the test made."""
    fake = FakeAnthropic(api_key="sk-ant-e2e-" + secrets.token_hex(12), host=MODEL_HOST).start()
    yield fake
    fake.stop()


@pytest.fixture(scope="session")
def runs_dir(request):
    """Where this session's Home Assistant config folders go (E2E_RUNS_DIR, else a new temp folder); removed at the
    end unless E2E_KEEP=1 or a test failed (its artifacts are in there)."""
    base = Path(os.environ["E2E_RUNS_DIR"]) if os.environ.get("E2E_RUNS_DIR") else None
    if base:
        base.mkdir(parents=True, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER")  # gw0, gw1, ... with -n: each worker's session has its own folder.
    folder = Path(tempfile.mkdtemp(prefix=f"assist-improver-e2e-{worker + '-' if worker else ''}", dir=base))
    yield folder
    if not os.environ.get("E2E_KEEP") and not getattr(request.session, "e2e_failed", False):
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(scope="session")
def golden(fake_model, runs_dir):
    """A household's Home Assistant, set up once (see the module docstring), then stopped, its log checked as each
    test's is (ha): {"folder", "login"}. Each test's Home Assistant starts from a copy of its folder. The login
    (username, password) is generated here and never printed or written anywhere but Home Assistant's own auth
    store."""
    folder = runs_dir / "golden"
    port = free_port()
    _write_config(folder, port, fake_model.url)
    _set_port(folder, port)
    process = _start_process(folder, fake_model.url, port)
    try:
        wait_until(lambda: _ready(port) or process.poll() is not None, timeout=STARTUP_SECONDS, what="Home Assistant")
        assert process.poll() is None, f"Home Assistant exited at startup; see {folder}/e2e-hass.log"
        base, client = f"http://127.0.0.1:{port}", _client_id(port)
        login = {"username": "alex", "password": "e2e-" + secrets.token_urlsafe(18)}
        wait_until(lambda: http("GET", f"{base}/api/onboarding"), what="onboarding")
        http("POST", f"{base}/api/onboarding/users", {"client_id": client, "name": "Alex", "language": "en", **login})
        ha = HomeAssistant(folder, port, process, login)
        wait_until(lambda: _running(ha), timeout=STARTUP_SECONDS, what="Home Assistant to finish starting")
        _set_up_household(ha, fake_model.api_key)
        ha.wait(lambda: all(e["state"] == "loaded" for e in ha.entries() if e["domain"] in
                            ("anthropic", "assist_improver")), what="the integrations to load")
        ha.ws.close()
    finally:
        _stop_process(process)
    log = folder / "e2e-hass.log"
    logged = logcheck.improvers(log.read_text(errors="replace")) if log.exists() else []
    assert not logged, (f"Home Assistant logged what's Assist Improver's to fix as the household set it up ({log}):"
                        "\n\n" + "\n\n".join(logged))
    _onboarded(folder)
    for path in ("home-assistant.log", "home-assistant.log.1", "e2e-hass.log", ".ha_run.lock"):
        (folder / path).unlink(missing_ok=True)
    return {"folder": folder, "login": login}


@pytest.fixture
def ha(golden, fake_model, runs_dir, request):
    """A fresh Home Assistant for this test, started from the golden household on a free port, logged in as the test
    user (HomeAssistant: its helpers read state). Stopped when the test ends, with its replay workers, and its log
    checked (logcheck.improvers: an error of the improver's, or a blocking call in its code, fails the test). On
    failure, its log and the fake model's requests for this test are kept beside its config folder; a test that passed
    leaves no folder (E2E_KEEP=1 keeps them all)."""
    folder = runs_dir / f"t{next(_NUMBERS):02d}-{request.node.name}"[:80].replace("/", "_")
    shutil.copytree(golden["folder"], folder)
    port = free_port()
    _set_port(folder, port)
    since = len(fake_model.requests)
    process = _start_process(folder, fake_model.url, port)
    try:
        wait_until(lambda: _ready(port) or process.poll() is not None, timeout=STARTUP_SECONDS, what="Home Assistant")
        assert process.poll() is None, f"Home Assistant exited at startup; see {folder}/e2e-hass.log"
        instance = wait_until(lambda: HomeAssistant(folder, port, process, golden["login"]), what="the login")
        wait_until(lambda: _running(instance), timeout=STARTUP_SECONDS, what="Home Assistant to finish starting")
        instance.wait(lambda: all(e["state"] == "loaded" for e in instance.entries()
                                  if e["domain"] in ("anthropic", "assist_improver")), what="the integrations to load")
        instance.since = since
        yield instance
    finally:
        try:
            if "instance" in locals():
                instance.ws.close()
        finally:
            _stop_process(process)
        log = folder / "e2e-hass.log"
        logged = logcheck.improvers(log.read_text(errors="replace")) if log.exists() else []
        failed = getattr(request.node, "e2e_failed", False) or bool(logged)
        if failed:
            fake_model.dump(folder / "e2e-model-requests.jsonl", since)
            print(f"\nE2E artifacts for {request.node.name}: {folder}")
        elif not os.environ.get("E2E_KEEP"):
            shutil.rmtree(folder, ignore_errors=True)
    assert not logged, "Home Assistant logged what's Assist Improver's to fix:\n\n" + "\n\n".join(logged)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        item.e2e_failed = item.session.e2e_failed = True


# The browser.

@pytest.fixture(scope="session")
def browser():
    """Playwright's headless Chromium (from tools/ui/browsers), for the session."""
    with sync_playwright() as playwright:
        chromium = jscov.launch(playwright.chromium, headless=True)
        yield chromium
        chromium.close()
        jscov.dump("e2e")


class Screen:
    """What the household sees and does in the browser, logged in to `ha`, at one size.

    - page: the Playwright page; size: "phone" or "desktop"; phone: whether it's the phone size; errors: the page's
      uncaught script errors so far, and rejections: what each unhandled promise rejection was; blocked: requests it
      tried beyond 127.0.0.1 (refused).
    - goto(path): open a page of the frontend (the dashboard by default).
    - assist(text): type a request in the Assist dialog (opened from the dashboard, open_assist(), if it
      isn't open) and send it: the locator of Assist's reply, once it's shown. close_assist() closes the dialog.
    - open_panel(): Assist Improver from the sidebar (on the phone, behind the sidebar toggle): the panel's locator.
    - conversation_row(words): the panel's row ("Your conversations") for the conversation whose words include
      `words`; chip(row) its status chip ("Sent in", "Up for review", ...).
    - shot(name): with E2E_SHOTS=1, a full-page screenshot in the test's Home Assistant folder (kept with E2E_KEEP=1),
      named <size>-<name>.png; a failing test gets one of where it stopped either way.
    - show_archived(): tick "Show archived (n)" under the list (conversations up for review or settled are left out of
      the list until then: DETAILS P14.4.1).
    - elsewhere(): another screen logged in as the same person (their phone, say): a second tab at the same size, so
      this one can stay where it is.
    Playwright's locators reach into the frontend's and the panel's shadow roots, so plain CSS works throughout.
    """

    def __init__(self, page, ha, size):
        self.page, self.ha, self.size = page, ha, size
        self.phone = size == "phone"
        self.errors, self.blocked = [], []  # The page's uncaught errors; requests beyond 127.0.0.1, refused.
        self.rejections = []  # What each unhandled promise rejection was, as the page logged it.

    def goto(self, path="/"):
        self.page.goto(self.ha.url + path)
        expect(self.page.locator("home-assistant")).to_be_attached()

    @property
    def dialog(self):
        return self.page.locator("ha-voice-command-dialog")

    def open_assist(self):
        """The Assist dialog, opened as the dashboard offers it: its Assist button where the toolbar has room (desktop),
        else its menu's Assist item (phone)."""
        chat = self.dialog.locator("ha-assist-chat")
        if not chat.is_visible():
            button = self.page.get_by_role("button", name=re.compile(r"^Assist\b"))  # "Assist (A)"
            menu = self.page.get_by_role("button", name="Open dashboard menu")
            expect(button.or_(menu).first).to_be_visible()
            if button.first.is_visible():
                button.first.click()
            else:
                menu.click()
                self.page.get_by_role("menuitem", name=re.compile(r"^Assist\b")).click()
        expect(chat).to_be_visible()
        return chat

    def assist(self, text):
        """Type one request in the Assist dialog and send it: the locator of Assist's reply, once the turn has ended."""
        chat = self.open_assist()
        replies = chat.locator(".message.hass")
        before = replies.count()
        field = self.dialog.get_by_role("textbox")
        field.fill(text)
        field.press("Enter")
        expect(chat.locator(".message.user").last).to_have_text(text)
        expect(replies).to_have_count(before + 1)
        reply = replies.nth(before)
        expect(reply).to_have_text(re.compile(r"\w"))
        # A streamed reply ends in "…" until its turn ends, when the dialog learns the conversation's ID. The field
        # takes a next request sooner, which would start a new conversation, so wait as a person would.
        expect(reply).not_to_have_text(re.compile(r"…\s*$"))
        return reply

    def close_assist(self):
        chat = self.dialog.locator("ha-assist-chat")
        if chat.is_visible():
            self.page.keyboard.press("Escape")
            expect(chat).to_be_hidden()

    def open_panel(self):
        """Assist Improver, from the sidebar (on the phone, behind the sidebar toggle)."""
        if self.phone:
            self.page.get_by_role("button", name="Sidebar toggle").click()
        self.page.locator("ha-sidebar").get_by_text("Assist Improver").first.click()
        expect(self.page).to_have_url(re.compile(re.escape(f"{self.ha.url}/assist-improver")))
        panel = self.page.locator("assist-improver-panel")
        expect(panel).to_be_attached()
        return panel

    @property
    def panel(self):
        return self.page.locator("assist-improver-panel")

    def conversation_row(self, words):
        return self.panel.locator(".cs-row").filter(has_text=words)

    @staticmethod
    def chip(row):
        return row.locator(".cs-line .chip")

    def shot(self, name):
        if os.environ.get("E2E_SHOTS"):
            self.page.screenshot(path=str(self.ha.folder / f"{self.size}-{name}.png"), full_page=True,
                                 animations="disabled")  # What a fade-in settles to, not its first frame.

    def show_archived(self):
        box = self.panel.locator("#cs-archived")
        box.check()
        expect(box).to_be_checked()

    def elsewhere(self):
        tab = self.page.context.new_page()
        jscov.start(tab)
        tab.set_default_timeout(EXPECT_TIMEOUT)
        return Screen(tab, self.ha, self.size)


@pytest.fixture(params=list(SIZES), ids=list(SIZES))
def page(browser, ha, request):
    """The frontend in a fresh browser context, logged in as the test user (Home Assistant's own token storage),
    at phone size (402x874) and desktop size (1280x800): each test taking it runs at both (Screen: its helpers)."""
    size = request.param
    tokens = {**ha.tokens, "hassUrl": ha.url, "clientId": _client_id(ha.port),
              "expires": int(time.time() * 1000) + ha.tokens["expires_in"] * 1000}
    context = browser.new_context(viewport=SIZES[size], device_scale_factor=1, locale="en-US",
                                  timezone_id="Asia/Singapore", color_scheme="dark")
    context.add_init_script(  # What an unhandled rejection was (the page error says only "Object" for a non-Error).
        "window.addEventListener('unhandledrejection', (e) => { let r = e.reason; try { r = JSON.stringify(r); } "
        "catch (_) {} console.error('e2e: unhandled rejection', r, (e.reason && e.reason.stack) || ''); });")
    context.add_init_script(  # Logged in as the frontend keeps a login; tokens it refreshed since are kept.
        f"if (!window.localStorage.getItem('hassTokens')) "
        f"window.localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))});")
    tab = context.new_page()
    jscov.start(tab)
    tab.set_default_timeout(EXPECT_TIMEOUT)
    screen = Screen(tab, ha, size)
    tab.on("pageerror", lambda err: screen.errors.append(f"{err.name}: {err.message} {(err.stack or '')[:600]}"))
    tab.on("console", lambda msg: msg.type == "error" and msg.text.startswith("e2e:") and screen.rejections.append(
        f"{msg.text} at {msg.location.get('url', '')}:{msg.location.get('lineNumber', '')}"))

    def offline(route):  # Nothing beyond 127.0.0.1 (the frontend's brand images, say): refused, and noted.
        screen.blocked.append(route.request.url)
        route.abort()
    context.route(re.compile(r"^(?!https?://127\.0\.0\.1[:/]).*$"), offline)
    try:
        yield screen
    finally:
        if getattr(request.node, "e2e_failed", False):
            try:
                tab.screenshot(path=str(ha.folder / f"e2e-{size}.png"), full_page=True)
            except Exception:  # noqa: BLE001 - a screenshot is a courtesy.
                pass
        jscov.collect(context)
        context.close()
