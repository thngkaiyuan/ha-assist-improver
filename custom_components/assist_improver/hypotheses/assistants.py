"""The assistants the sessions used, for the proposer and the reviewer: each one's instructions as its newest
conversation ran with them and as Home Assistant has them now, the recommended facts they lack, and an edited
template as Home Assistant renders it in a sandbox (instructions.py).

Code derives all of it: which assistant each session used (its brief), the template, whether a fact is there, and
whether an edited template renders. The roles judge whether a friction's cause lies there.
"""

from copy import deepcopy

from .. import instructions


class Assistants:
    """Mixed into Sessions (households.py): the assistants of its sessions, by target ("config entry/subentry")."""

    _assistants = None
    user_names = None  # {user id: name}, from Home Assistant (load_assistants).
    renders = None  # {(target, template): {"rendered"} or {"error"}}: edited instructions, rendered.

    def assistants(self):
        """{target: assistant}: name (unique among them), title, how many sessions, the newest and the newest one
        that can be replayed, its instructions as the newest ran with them (and as the model read them), Home
        Assistant's current ones once loaded (load_assistants), and the facts' values there (the speaker's name,
        the asking device's name, the time zone)."""
        if self._assistants is None:
            self._assistants = self._gather()
        return self._assistants

    def _gather(self):
        by_target = {}
        for session, brief in sorted(self.briefs.items(), key=lambda kv: kv[1]["started"] or "", reverse=True):
            if brief.get("assistant_target"):
                by_target.setdefault(brief["assistant_target"], []).append((session, brief))
        out, titles = {}, {}
        for target, rows in by_target.items():
            newest, brief = rows[0]
            record = {"target": target, "title": brief.get("assistant_title") or "Assist", "sessions": len(rows),
                      "newest": newest, "replayable": next((s for s, b in rows if b["replayable"]), None),
                      "ran_with": None, "rendered": None, "values": {}, "current": None, "set_up": None}
            view = self._view(newest)
            if view is not None:
                self._read_view(record, view)
            out[target] = record
            titles.setdefault(record["title"], []).append(target)
        for title, targets in titles.items():
            for n, target in enumerate(targets, 1):
                out[target]["name"] = title if len(targets) == 1 else f"{title} ({n})"
        return out

    def _read_view(self, record, view):
        ran = instructions.ran_with(view.recording) or {}
        source = view.source or {}
        device = (source.get("devices") or {}).get(ran.get("device_id") or "") or {}
        record.update(ran_with=ran.get("template"), rendered=ran.get("rendered"), user_id=ran.get("user_id"),
                      values={"speaker": (self.user_names or {}).get(ran.get("user_id")),
                              "device": device.get("name_by_user") or device.get("name"),
                              "time_zone": source.get("time_zone")})

    async def load_assistants(self, hass):
        """Each assistant's newest conversation read, its current instructions and name from Home Assistant (None
        when it's no longer set up), and the speakers' names, for the roles and the checks."""
        self.user_names = dict(self.user_names or {})
        self._assistants = None
        records = self.assistants()
        for record in records.values():
            view = await self.load(record["newest"])
            if view is not None:
                self._read_view(record, view)
            user_id = record.get("user_id")
            if user_id and user_id not in self.user_names and getattr(hass, "auth", None):
                user = await hass.auth.async_get_user(user_id)
                self.user_names[user_id] = getattr(user, "name", None)
            record["values"]["speaker"] = self.user_names.get(user_id)
            live = instructions.live(hass, record["target"])
            record["set_up"] = live is not None
            if live is not None:
                record["current"], record["title"] = live["template"], live["title"] or record["title"]

    def current_template(self, record):
        """An assistant's instructions now: Home Assistant's, else as its newest conversation ran with them."""
        return record["current"] if record.get("set_up") else record.get("ran_with")

    def _rendered(self, record):
        """What the model read of its current instructions, where its newest conversation ran with those."""
        return record.get("rendered") if self.current_template(record) == record.get("ran_with") else None

    def facts_missing(self, record):
        return instructions.missing(self.current_template(record), self._rendered(record), record.get("values"))

    def facts_given(self, record, template=None):
        """{fact: how} for the recommended facts its instructions (or this version of them) give."""
        current = self.current_template(record)
        template = current if template is None else template
        return instructions.given(template, self._rendered(record) if template == current else None,
                                  record.get("values"))

    def assistant_of(self, session):
        """The assistant a session used, or None."""
        brief = self.briefs.get((session or "")[:8]) or {}
        return self.assistants().get(brief.get("assistant_target"))

    def assistant_named(self, name):
        """The assistant a role named: by its name, its title or its target; None if none, or ambiguous."""
        name = (name or "").strip()
        records = list(self.assistants().values())
        for key in ("name", "title", "target"):
            found = [r for r in records if (r.get(key) or "").casefold() == name.casefold()]
            if len(found) == 1:
                return found[0]
        found = [r for r in records if r["target"].split("/")[-1] == name]
        return found[0] if len(found) == 1 else None

    def assistant_names(self):
        return ", ".join(sorted(r["name"] for r in self.assistants().values())) or "none recorded"

    def with_targets(self, changes):
        """The changes, each edit of an assistant's instructions with the target code found for the assistant it
        names (none when it names none it knows); a target given without an assistant stays only when it's one of
        theirs."""
        out = []
        for change in changes or []:
            change = deepcopy(change)
            if change.get("kind") == "prompt":
                given = change.pop("target", None)
                record = self.assistant_named(change["assistant"]) if change.get("assistant") else None
                if record is not None:
                    change["target"] = record["target"]
                elif not change.get("assistant") and given in self.assistants():
                    change["target"] = given
            out.append(change)
        return out

    def edited(self, target, changes):
        """(template, problems): an assistant's current instructions with the changes' edits on it."""
        record = self.assistants()[target]
        return instructions.edit(self.current_template(record),
                                 [c for c in changes if instructions.is_edit(c) and c.get("target") == target])

    async def render_edits(self, hass, hypotheses):
        """Each assistant's instructions as the hypotheses' edits make them, rendered by Home Assistant in a sandbox
        (render; kept in renders)."""
        self.renders = self.renders if self.renders is not None else {}
        for hypothesis in hypotheses or []:
            changes = self.with_targets(hypothesis.get("changes"))
            for target in {c["target"] for c in changes if instructions.is_edit(c) and c.get("target")}:
                template, _ = self.edited(target, changes)
                await self.render(hass, target, template)

    async def render(self, hass, target, template):
        """{"rendered"} or {"error"}: an assistant's (edited) instructions as Home Assistant renders them for its
        newest conversation that can be replayed, and for a request without a signed-in person or an asking device,
        in a sandbox. Without a conversation of its own to seed one, the household's newest that can, for the latter
        only ({"renders_quietly"}); parsed only ({"parsed"}) without any."""
        self.renders = self.renders if self.renders is not None else {}
        key = (target, template)
        if key in self.renders:
            return self.renders[key]
        record = self.assistants().get(target) or {}
        view = await self.load(record["replayable"]) if record.get("replayable") else None
        found = None
        if view is not None and self.evidence is not None:
            user_id = (instructions.ran_with(view.recording) or {}).get("user_id")
            found = await self.evidence.render(view, [template], (self.user_names or {}).get(user_id))
            found = found[0] if found else None
        elif self.household is not None and self.evidence is not None:
            found = await self.evidence.render(self.household, [template], quiet_only=True)
            found = found[0] if found else None
        if found is None and hass is not None:
            from homeassistant.helpers.template import Template
            try:
                Template(template, hass).ensure_valid()
                found = {"parsed": True}
            except Exception as err:  # noqa: BLE001 - a template Home Assistant can't read is the fact.
                found = {"error": str(err)[:300] or type(err).__name__}
        if found is not None:
            self.renders[key] = found
        return found

    def assistant_rows(self):
        """Each assistant as the roles get it upfront: its name, how many sessions used it, and the recommended
        facts its instructions lack."""
        rows = []
        for record in sorted(self.assistants().values(), key=lambda r: -r["sessions"]):
            missing = [f["what"] for f in self.facts_missing(record)]
            rows.append({"assistant": record["name"], "conversations": record["sessions"],
                         **({"recommended_facts_missing": missing} if missing else {}),
                         **({"no_longer_set_up": True} if record.get("set_up") is False else {})})
        return rows

    def instructions_of(self, record, session=None):
        """One assistant's instructions as read_assistant_instructions shows them: now, and as a session ran with
        them; the recommended facts they lack."""
        current = self.current_template(record)
        out = {"assistant": record["name"], "conversations": record["sessions"],
               "current_instructions": instructions.effective(current)}
        if not (current or "").strip():
            out["note"] = "It has no instructions of its own: Home Assistant's default ones (shown) apply."
        if record.get("set_up") is False:
            out["no_longer_set_up"] = "It's no longer set up in Home Assistant: shown as its newest conversation ran."
        if session is not None:
            out["session"] = session[0]
            out["instructions_it_ran_with"] = instructions.effective(session[1])
            out["same_as_now"] = (session[1] or "") == (current or "")
        missing = self.facts_missing(record)
        out["recommended_facts_missing"] = missing or "none: its instructions give every recommended fact"
        return out
