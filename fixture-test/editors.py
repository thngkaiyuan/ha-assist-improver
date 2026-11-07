"""The panel's half of saving a script, automation or scene, in offline tests: Home Assistant's own editor endpoints
(/api/config/<kind>/config/<id>), HA's real views (homeassistant.components.config.script, .automation, .scene) called
without HTTP, as the panel calls them with the signed-in admin's session; and the panel on a websocket connection,
saving what the improver asks it to (panel.EditorChannel) and answering, as www/panel/app.js does.
"""

import asyncio
import itertools
import json
from types import SimpleNamespace

from homeassistant.components.http import KEY_HASS, KEY_HASS_USER
from homeassistant.exceptions import HomeAssistantError


class Request(dict):
    """An admin's request, as aiohttp hands HA's view one: its user, the app's hass, and its JSON body."""

    def __init__(self, hass, body=None):
        super().__init__({KEY_HASS_USER: SimpleNamespace(is_admin=True)})
        self.app, self._body = {KEY_HASS: hass}, body

    async def json(self):
        return json.loads(json.dumps(browser(self._body)))  # As the browser sends it.


def browser(value):
    """A value as the panel's JavaScript sends it: JSON there has one kind of number, so a whole-number float (30.0)
    goes as an integer (30)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: browser(v) for k, v in value.items()}
    if isinstance(value, list):
        return [browser(v) for v in value]
    return value


class Editors:
    """HA's script, automation and scene editor endpoints. save(kind, item_id, config) is what the panel does for one
    save it's asked for: POST the config (or DELETE, for None) to that kind's editor; a refusal raises HA's reason.
    calls: (method, kind, item_id), in order. fail: called with (method, kind, item_id) before each call; what it
    raises stands in for a call that failed (nothing saved)."""

    def __init__(self, hass, fail=None):
        from homeassistant.components.config import automation, scene, script
        views = []
        http, hass.http = getattr(hass, "http", None), SimpleNamespace(register_view=views.append)
        try:
            for editor in (script, automation, scene):
                editor.async_setup(hass)
        finally:
            hass.http = http
        self.hass, self.views, self.fail, self.calls = hass, dict(zip(("script", "automation", "scene"), views)), fail, []

    async def save(self, kind, item_id, config):
        method = "DELETE" if config is None else "POST"
        self.calls.append((method, kind, item_id))
        if self.fail:
            await self.fail(method, kind, item_id)
        view = self.views[kind]
        response = await (view.delete(Request(self.hass), item_id) if config is None
                          else view.post(Request(self.hass, config), item_id))
        if response.status != 200:
            raise HomeAssistantError(json.loads(response.body)["message"])

    async def get(self, kind, item_id):
        """The item as its editor opens it (GET), or None."""
        response = await self.views[kind].get(Request(self.hass), item_id)
        return json.loads(response.body) if response.status == 200 else None


IDS = itertools.count(1000)


class Connection(SimpleNamespace):
    """A websocket connection from the panel, signed in as an admin. results: {message id: ("ok", result) or ("error",
    code, text)}; events: what the improver sent the subscriptions."""

    def __init__(self, user_id="alex"):
        super().__init__(user=SimpleNamespace(id=user_id, name="Alex", is_admin=True), subscriptions={}, results={},
                         events=[], on_event=None)

    def send_result(self, msg_id, result=None):
        self.results[msg_id] = ("ok", result)

    def send_error(self, msg_id, code, text):
        self.results[msg_id] = ("error", code, text)

    def send_message(self, message):
        self.events.append(message)
        if self.on_event:
            self.on_event(message)

    def close(self):
        """The panel went away: Home Assistant ends its subscriptions."""
        for unsubscribe in list(self.subscriptions.values()):
            unsubscribe()
        self.subscriptions.clear()


async def command(hass, connection, handler, **msg):
    """One websocket command on this connection: its answer."""
    from test_changeset import _ws
    msg_id = next(IDS)
    await _ws(handler, hass, connection, {"id": msg_id, **msg})
    return connection.results[msg_id]


async def open_panel(hass, connection, editor, answering=True):
    """The panel opens on this connection: it subscribes to save editor items (assist_improver/editors), and saves each
    one it's asked to through HA's editor for it, answering with assist_improver/editors/saved (what HA refused with,
    if it did). answering: False, it takes what it's asked and never answers (gone, or hung)."""
    from custom_components.assist_improver import panel
    asked = []

    def on_event(message):
        asked.append(message["event"])
        if answering:
            hass.async_create_task(answer(message["event"]))

    async def answer(event):
        try:
            await editor.save(event["kind"], event["id"], event["config"])
            error = None
        except Exception as err:  # noqa: BLE001  What the panel says back: HA's reason.
            error = str(err) or type(err).__name__
        await command(hass, connection, panel.ws_editors_saved, type="assist_improver/editors/saved", key=event["key"],
                      **({"error": error} if error else {}))
    connection.on_event = on_event
    assert (await command(hass, connection, panel.ws_editors, type="assist_improver/editors"))[0] == "ok"
    return asked


async def settle(hass):
    """Until what HA started for a save (its editor's reload, a registry removal) is done."""
    await hass.async_block_till_done()
    await asyncio.sleep(0)
