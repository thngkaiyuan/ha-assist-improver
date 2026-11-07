"""The suite's only stand-in: Anthropic's Messages API, answered on 127.0.0.1 by deterministic scripts.

Home Assistant's real anthropic integration talks to it (the anthropic SDK reads ANTHROPIC_BASE_URL, and Home
Assistant passes no base_url of its own), so everything between the screen and the model is the product's and Home
Assistant's own code. The server answers:

- GET /v1/models (the integration's key check, and its model list) with a few Claude models, each with its
  capabilities as the Models API gives them;
- POST /v1/messages, streamed or not, with the key the throwaway config was given (any other key is refused, as
  Anthropic refuses it).

Each request is answered only from what it carries, as a model would be: its tools, its system prompt, its messages
and the structured output it asks for. Nothing is shared between requests, so the answer to a request is the same
however often it's sent. Who is asking is told apart by the tools it offers (each AI role answers through its own
submit tool) or the output it asks for; anything else is the household's assistant (Assist, or Simulate's replay of
it). The roles follow the stand-ins the offline suites use (fixture-test/kit.py, fixture-test/graph/harness/roles.py):
each plays its part through its real tools, so every answer passes the product's own checks.

A role is a plain function of (ctx, payload). ctx.call(name, args) / ctx.calls([...]) is one model message with tool
calls: the first time the script reaches it, the server answers with those tool calls and stops; Home Assistant runs
them and sends the conversation back with their results, and the script runs again from the top, getting each result
it already has from the conversation, until it reaches a message not yet sent. Its return value is the final reply
(a dict for a role asked for structured output: sent as JSON text).

The household's assistant (household()) calls an intent tool only when the name the user said is one of a device's
names or aliases in Assist's prompt (the Static Context Home Assistant gives it), or a household knowledge line there
says which device the name means; it says it can't find it otherwise, and files the conversation with the improver's
tool when asked to file an improvement and that tool is offered. Asked whether a device is on, it guesses the other
way unless a household line asks it to check, when it reads Home Assistant's live context. The log
(FakeAnthropic.requests) keeps every request with who answered and what, for a test's failure report.
"""

from datetime import UTC, datetime
import itertools
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Each model, with the effort levels and thinking types the Models API says it takes: Home Assistant's anthropic flow
# offers those (Opus 5.5 always thinks, adaptively; Haiku 4.5 takes a thinking budget and no effort).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODELS = (("claude-opus-5-5", "Claude Opus 5.5", EFFORTS, ("adaptive",)),
          ("claude-sonnet-5", "Claude Sonnet 5", EFFORTS, ("adaptive", "disabled")),
          ("claude-haiku-4-5", "Claude Haiku 4.5", (), ("enabled", "disabled")),
          ("claude-haiku-5-5", "Claude Haiku 5.5", EFFORTS, ("adaptive", "disabled")))
# s: how long a household reply's stream stays open after its words, as a real model's does (its usage comes last).
# The Assist dialog shows the words before the turn ends, so every journey with a next turn crosses that window.
TAIL = 0.5
FAILURE = re.compile(r"\b(couldn't|could not|can't|cannot|sorry|unable|not able|don't know)\b", re.I)
FILE_REQUEST = re.compile(r"\bfile\b.*\bimprovement\b", re.I)
# The household's wording, as the stand-ins read it. An assistant whose instructions ask it to say what to expect next
# promises it in a reply (PROMISE); a household line asking for only what it did (TERSE) makes it say just "Done.".
# A user's turn that thanks it (THANKS), or objects to how it behaved (OBJECTION: the words after a "but", if any).
EXPECT_NEXT = "Tell the user what to expect next."
PROMISE = re.compile(r"\bit will be ready\b", re.I)
TERSE = re.compile(r"^say only what you did\b", re.I)
THANKS = re.compile(r"^\s*(?:thanks|thank you)\b", re.I)
LIST_ADD = re.compile(r"^\s*add (?P<item>.+?) to (?:the |my )?(?P<list>.+?)[.?!]?\s*$", re.I)
LIST_READ = re.compile(r"\bwhat'?s on (?:the |my )?(?P<list>.+?)[.?!]?\s*$", re.I)
SCRIPT_RUN = re.compile(r"^\s*run (?:the |my )?(?P<script>.+?)[.?!]?\s*$", re.I)
OBJECTION = re.compile(r"\b(?:just say|without|less|too|didn't need|don't need)\b", re.I)
# A question about a device ("Is the desk lamp on?"): Assist guesses the other way, unless a household line asks it to
# check (CHECK), when it reads Home Assistant's live context and answers from it.
ASKED = re.compile(r"^\s*is\s+(?:the\s+|my\s+)?(.+?)\s+(on|off)\b.*$", re.I)
CHECK = re.compile(r"^check a device's state before\b", re.I)
TURN = re.compile(r"^\s*(?:please\s+|can you\s+|could you\s+)*(?:turn|switch)\s+(on|off)\s+(?:the\s+|my\s+)?(.+?)"
                  r"(?:,?\s+please)?[.!?\s]*$", re.I)
TURN_AFTER = re.compile(r"^\s*(?:please\s+)?(?:turn|switch)\s+(?:the\s+|my\s+)?(.+?)\s+(on|off)(?:,?\s+please)?"
                        r"[.!?\s]*$", re.I)


class Turn(Exception):
    """A role's script reached a model message not yet sent: its blocks go out now."""

    def __init__(self, blocks):
        super().__init__("next message")
        self.blocks = blocks


class Ctx:
    """One request's conversation, for a role's script to call tools through (see the module's docstring)."""

    def __init__(self, request):
        self.request = request
        self.tools = {t.get("name"): t for t in request.get("tools") or [] if t.get("name")}
        messages = request.get("messages") or []
        self.sent = [m for m in messages if m.get("role") == "assistant"]
        self.results = {}
        for message in messages:
            if message.get("role") == "user" and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        self.results[block.get("tool_use_id")] = _parsed(block.get("content"))
        self.step = 0

    def calls(self, calls):
        """One model message with these tool calls ([(name, args)]): their results, in order."""
        if self.step < len(self.sent):
            message = self.sent[self.step]
            self.step += 1
            uses = [b for b in _blocks(message) if b.get("type") == "tool_use"]
            return [self.results.get(b.get("id")) for b in uses]
        number = self.step
        raise Turn([{"type": "tool_use", "id": f"toolu_e2e_{number:03d}_{n}", "name": name, "input": args or {}}
                    for n, (name, args) in enumerate(calls)])

    def call(self, name, args=None):
        return self.calls([(name, args)])[0]


def _blocks(message):
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _parsed(content):
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


def _text(message):
    """A user message's own words (None for one carrying tool results)."""
    blocks = _blocks(message)
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "".join(texts) if texts else None


def _system(request):
    system = request.get("system") or ""
    if isinstance(system, list):
        system = "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return system


def payload_of(request):
    """An AI role's input: the JSON after "Input (JSON):" in its first message (reflective.run_role)."""
    first = next((m for m in request.get("messages") or [] if m.get("role") == "user"), None)
    text = _text(first) if first else None
    if not text or "Input (JSON):\n" not in text:
        return {}
    return json.loads(text.split("Input (JSON):\n", 1)[1])


def said_target(words):
    """(on|off, the device the words name) for a request to turn something on or off, else None."""
    if match := TURN.match(words or ""):
        return match.group(1).lower(), match.group(2).strip()
    if match := TURN_AFTER.match(words or ""):
        return match.group(2).lower(), match.group(1).strip()
    return None


def asked_about(words):
    """(the device the words ask about, on|off) for a question about a device's state, else None."""
    if match := ASKED.match(words or ""):
        return match.group(1).strip(), match.group(2).lower()
    return None


def _live_answer(name, result):
    """Assist's answer to whether a device is on, from Home Assistant's live context (GetLiveContext's result)."""
    text = (result.get("result") if isinstance(result, dict) else None) or json.dumps(result)
    for entry in re.split(r"\n- ", text):  # One device each: its names, then its state.
        names = re.search(r"^(?:- )?names: (.+)$", entry, re.M)
        state = re.search(r"^\s*state: '?([^'\n]+)'?$", entry, re.M)
        if names and state and any(_same(name, n.strip()) for n in names.group(1).split(",")):
            return f"The {name} is {state.group(1)}."
    return f"Sorry, I couldn't find the {name}."


def static_context(system):
    """The devices Assist's prompt lists (Home Assistant's Static Context): [{names: [...], domain, areas}]."""
    if "Static Context:" not in system:
        return []
    devices, current = [], None
    for line in system.split("Static Context:", 1)[1].splitlines()[1:]:
        if not line.strip():
            continue
        if not line.startswith(("- ", "  ")):
            break
        if line.startswith("- "):
            current = {}
            devices.append(current)
            line = line[2:]
        key, _, value = line.strip().partition(":")
        value = value.strip().strip("'\"")
        if current is not None and key:
            current[key] = value
    return [{"names": [n.strip() for n in d.get("names", "").split(",") if n.strip()], "domain": d.get("domain"),
             "areas": d.get("areas", "")} for d in devices]


def _same(a, b):
    return " ".join((a or "").casefold().split()) == " ".join((b or "").casefold().split())


KNOWLEDGE = "Household knowledge (facts and preferences the household has confirmed; follow them):"
MEANS = re.compile(r"^(?:the\s+)?(.+?)\s+(?:is|means)\s+(?:the\s+)?(.+?)[.!]?$", re.I)


def household_knowledge(system):
    """The household knowledge lines Assist's prompt carries (the improver adds them, household.render): [line]."""
    if KNOWLEDGE not in system:
        return []
    lines = []
    for line in system.split(KNOWLEDGE, 1)[1].splitlines()[1:]:
        if not line.startswith("- "):
            break
        lines.append(line[2:].strip())
    return lines


def known_as(name, lines, devices):
    """The device name a household knowledge line says this name means ("The reading lamp is the desk lamp."), when
    the line names one of the devices Assist's prompt lists, else None."""
    for line in lines:
        if (found := MEANS.match(line)) and _same(found.group(1), name):
            meant = " ".join(found.group(2).casefold().split())
            hits = sorted(((len(n), n) for d in devices for n in d["names"]
                           if re.search(rf"\b{re.escape(n.casefold())}\b", meant)), reverse=True)
            if hits:
                return hits[0][1]
    return None


# The household's assistant: Assist, and Simulate's replays of it.

def household(request):
    """Assist's answer to the latest thing the user said: blocks, as a model sends them."""
    messages = request.get("messages") or []
    tools = [t.get("name", "") for t in request.get("tools") or []]
    said_at = max((n for n, m in enumerate(messages) if m.get("role") == "user" and _text(m) is not None), default=None)
    if said_at is None:
        return [{"type": "text", "text": "How can I help?"}]
    said = _text(messages[said_at]).strip()
    after = messages[said_at + 1:]
    used = [b for m in after if m.get("role") == "assistant" for b in _blocks(m) if b.get("type") == "tool_use"]
    if used:  # Answering its tool calls' results.
        results = {b.get("tool_use_id"): _parsed(b.get("content")) for m in after if m.get("role") == "user"
                   for b in _blocks(m) if b.get("type") == "tool_result"}
        if used[-1].get("name", "").endswith("GetLiveContext") and (asked := asked_about(said)):
            return [{"type": "text", "text": _live_answer(asked[0], results.get(used[-1].get("id")))}]
        return [{"type": "text", "text": _reply(used[-1], results.get(used[-1].get("id")), _system(request))}]
    filer = next((t for t in tools if t.endswith("file_improvement")), None)
    if FILE_REQUEST.search(said):
        if filer is None:
            return [{"type": "text", "text": "Sorry, I can't file improvements."}]
        note = re.split(r"\bimprovement\b[\s:,.-]*", said, maxsplit=1, flags=re.I)[-1].strip()
        return [{"type": "tool_use", "id": "toolu_e2e_file", "name": filer,
                 "input": {"what_to_improve": note} if note else {}}]
    if THANKS.match(said):
        return [{"type": "text", "text": "You're welcome."}]
    if asked := asked_about(said):
        live = next((t for t in tools if t.endswith("GetLiveContext")), None)
        if live and any(CHECK.match(line) for line in household_knowledge(_system(request))):
            return [{"type": "tool_use", "id": "toolu_e2e_live", "name": live, "input": {}}]
        name, state = asked
        return [{"type": "text", "text": f"The {name} is {'off' if state == 'on' else 'on'}."}]  # A guess.
    for pattern, suffix, args in ((LIST_ADD, "HassListAddItem", lambda m: {"item": m["item"], "name": m["list"]}),
                                  (LIST_READ, "get_items", lambda m: {"todo_list": m["list"]})):
        if (found := pattern.search(said)) and (tool := next((t for t in tools if t.endswith(suffix)), None)):
            return [{"type": "tool_use", "id": "toolu_e2e_list", "name": tool, "input": args(found)}]
    if (found := SCRIPT_RUN.search(said)) and (tool := next((t for t in tools if t.endswith(
            "script__" + "_".join(found["script"].casefold().split()))), None)):  # An exposed script's own tool.
        return [{"type": "tool_use", "id": "toolu_e2e_script", "name": tool, "input": {}}]
    if target := said_target(said):
        verb, name = target
        devices = static_context(_system(request))
        match = next((n for d in devices for n in d["names"] if _same(n, name)), None)
        match = match or known_as(name, household_knowledge(_system(request)), devices)
        tool = next((t for t in tools if t.endswith("HassTurnOn" if verb == "on" else "HassTurnOff")), None)
        if match and tool:
            return [{"type": "tool_use", "id": "toolu_e2e_turn", "name": tool, "input": {"name": match}}]
        script = next((t for t in tools if t.endswith("script__" + "_".join(name.casefold().split()))), None)
        if verb == "on" and script:  # An exposed script named as the device was said: Assist runs it.
            return [{"type": "tool_use", "id": "toolu_e2e_script", "name": script, "input": {}}]
        return [{"type": "text", "text": f"Sorry, I couldn't find a device called {name}."}]
    return [{"type": "text", "text": "Sorry, I can't help with that."}]


def _reply(use, result, system=""):
    """What Assist says once a tool answered: as its instructions and the household knowledge ask (the wording knobs
    above)."""
    if use.get("name", "").endswith("file_improvement"):
        return ("Filed. The Assist Improver will look at it once we're done." if isinstance(result, dict)
                and result.get("filed") else "Sorry, I couldn't file it.")
    data = (result or {}).get("data") if isinstance(result, dict) else None
    if use.get("name", "").endswith("get_items"):
        items = [i.get("summary") for i in (result or {}).get("items") or []] if isinstance(result, dict) else []
        return f"On the list: {', '.join(items)}." if items else "The list is empty."
    if use.get("name", "").endswith("HassListAddItem"):
        return f"Added {use['input'].get('item')}." if isinstance(data, dict) and data.get("success") is not None \
            else "Sorry, I couldn't add that."
    if "script__" in use.get("name", ""):
        return "It's running." if isinstance(result, dict) and "result" in result else "Sorry, I couldn't run it."
    success = (data or {}).get("success") if isinstance(data, dict) else None
    if success:
        if any(TERSE.match(line) for line in household_knowledge(system)):
            return "Done."
        names = ", ".join(t.get("name", "") for t in success)
        done = f"Turned {'on' if use['name'].endswith('HassTurnOn') else 'off'} the {names}."
        return f"{done} It will be ready in a few minutes." if EXPECT_NEXT in system else done
    return "Sorry, I couldn't do that."


# The improver's AI roles.

def triage(ctx, payload):
    """Smooth only when every turn's request was done: an action Home Assistant carried out, and no reply saying it
    couldn't."""
    turns = payload.get("turns") or []
    def carried_out(returns):
        """Home Assistant's result says it did it: the intent response's success list. The quick check gets each
        tool result as the chat log keeps it, {"data": <the intent response>, "error": false}."""
        if not isinstance(returns, dict) or returns.get("error"):
            return False
        response = returns.get("data") if "error" in returns else returns
        return bool(isinstance(response, dict) and ((response.get("data") or {}).get("success")))

    def done(turn):
        steps = turn.get("steps") or []
        acted = any(carried_out(s.get("home_assistant_returns")) for s in steps)
        refused = any(FAILURE.search(s.get("assist_says") or "") for s in steps)
        return acted and not refused
    smooth = bool(turns) and all(done(t) for t in turns)
    return {"smooth": smooth, "sure": True,
            "why": "Every request was done as asked." if smooth else "Assist didn't do what the user asked."}


def _cards(results):
    """The devices devices_in_area answers listed: {entity_id: card}."""
    found = {}
    for result in results:
        for card in (result or {}).get("devices") or [] if isinstance(result, dict) else []:
            if card.get("entity_id"):
                found[card["entity_id"]] = card
    return found


def _names(card):
    return [n for n in [card.get("name"), *(card.get("aliases") or [])] if n]


def _named_in(words, cards):
    """The device whose name or alias these words say, if one is (the longest name first)."""
    text = " ".join((words or "").casefold().split())
    hits = sorted(((len(n), e) for e, c in cards.items() for n in _names(c)
                   if re.search(rf"\b{re.escape(n.casefold())}\b", text)), reverse=True)
    return hits[0][1] if hits else None


def journey_analyzer(ctx, payload):
    """The user's goals, as submit_goals takes them: each request to turn a device on or off is a goal, on the device
    whose name or alias it says, or failing that the one a later turn or the note names (a request for a device whose
    goal Assist just failed is a step towards it); anything said after it is a sub-goal of the goal it's about: the
    request to file it a wrap-up, a thanks, an objection to Assist's reply to that request (conduct, quoting the words
    after a "but", if any), anything else a step. So is the note (analyzer.FILED): a step when it names a device (it says again what
    the goal wanted), an objection to how Assist behaved otherwise, or when it asks Assist to remember something,
    quoted as it was written."""
    lines = payload.get("conversation") or []
    said = [line for line in lines if not line.get("sent_with_it") and not line.get("revision_of")]
    note = ((payload.get("filed_by_user") or {}).get("note") or "").strip()
    areas = [a.get("area") for a in payload.get("areas") or [] if a.get("area")]
    calls = [("read_turn", {"turns": [line["turn"] for line in said]})] + [
        ("devices_in_area", {"area": area}) for area in areas]
    results = ctx.calls(calls)
    cards = _cards(results[1:])
    goals, not_requests, question = [], [], ""
    root = None  # The turn of the latest root goal.
    for n, line in enumerate(said):
        words, turn = (line.get("user_says") or "").strip(), line["turn"]
        target = said_target(words)
        if asked := asked_about(words):  # A question about a device: a fact, or asked again, a step towards it.
            name, state = asked
            entity = next((e for e, c in cards.items() if any(_same(name, x) for x in _names(c))), None)
            if entity is None and root is not None:  # "Is it on now?" after a request: about that, a step.
                goals.append({"turn": turn, "summary": "Asked about it", "kind": "step", "serves": root})
                continue
            if any(g["turn"] == root and g.get("kind") == "fact" and entity in (g.get("entity_ids") or [])
                   for g in goals):
                goals.append({"turn": turn, "summary": "Asked again", "kind": "step", "serves": root})
                continue
            goals.append({"turn": turn, "summary": f"Whether the {name} is {state}", "kind": "fact",
                          "fact": f"Whether the {name} is {state}", **({"entity_ids": [entity]} if entity else {})})
            root = turn
        elif target and not FILE_REQUEST.search(words):
            state, name = target
            entity = next((e for e, c in cards.items() if any(_same(name, x) for x in _names(c))), None)
            if entity is None:  # Which device the words meant: the one a later turn or the note names.
                later = " ".join([*(l.get("user_says") or "" for l in said[n + 1:]), note])
                entity = _named_in(later, cards)
            if entity is None:
                question = f"Which device did you mean by “{name}”?"
                not_requests.append({"turn": turn, "what": f"Asked for “{name}”, which isn't a device here"})
                continue
            again = next((g for g in goals if g["turn"] == root and g.get("entity_id") == entity), None)
            if again and FAILURE.search(said[n - 1].get("assistant_says") or ""):  # Asked again after Assist failed.
                goals.append({"turn": turn, "summary": "Asked again", "kind": "step", "serves": root})
                continue
            goals.append({"turn": turn, "summary": f"{cards[entity].get('name') or entity} {state}", "kind": "state",
                          "entity_id": entity, "state": state})
            root = turn
        elif root is not None:  # Filing it, or anything else said about it: a reaction to the goal before.
            if THANKS.match(words):
                goals.append({"turn": turn, "summary": "Thanked Assist", "kind": "thanks", "serves": root})
            if FILE_REQUEST.search(words):
                goals.append({"turn": turn, "summary": "Filed it for improvement", "kind": "wrap-up", "serves": root})
            elif OBJECTION.search(words):
                quote = re.split(r"\bbut\b[\s,]*", words, maxsplit=1, flags=re.I)[-1].strip(" .!")
                goals.append({"turn": turn, "summary": "Objected to the reply", "kind": "conduct", "about": root,
                              "quote": quote, "serves": root})
            elif not THANKS.match(words):
                goals.append({"turn": turn, "summary": "Said more about it", "kind": "step", "serves": root})
        else:
            not_requests.append({"turn": turn, "what": "Not a request for the home"})
    if note:
        step = _named_in(note, cards) and not REMEMBER.match(note)
        goals.append({"turn": len(said) + 1, "summary": f"The note: {note}",
                      **({"kind": "step"} if step else {"kind": "conduct", "about": "all", "quote": note}),
                      **({"serves": root} if root is not None else {})})
    fixing = []
    for line in lines:  # Each revision note (analyzer.REVISION): a step when it names a device, an objection when it
        if revised := line.get("revision_of"):  # objects, else only about the fix itself, with no goal.
            words, serves = line["user_says"].strip(), revised["for_goals_at_turns"][0]
            if _named_in(words, cards) and not REMEMBER.match(words):
                goals.append({"turn": line["turn"], "summary": f"Revision: {words}", "kind": "step", "serves": serves})
            elif OBJECTION.search(words):
                goals.append({"turn": line["turn"], "summary": f"Revision: {words}", "kind": "conduct", "about": "all",
                              "quote": words, "serves": serves})
            else:
                fixing.append({"turn": line["turn"], "what": words})
    answer = {"goals": goals, "not_requests": not_requests, "question_for_user": question,
              **({"about_the_fix": fixing} if fixing else {})}
    accepted = ctx.call("submit_goals", answer)
    if not (isinstance(accepted, dict) and accepted.get("accepted")):
        return "My answer wasn't accepted."
    if "mark_duplicate" in ctx.tools:
        for goal in [g for g in goals if g.get("serves") is None]:
            if covering := _covering(payload.get("improvements_on_these_devices") or [], goal, said):
                ctx.call("mark_duplicate", {"goal_turn": goal["turn"], "improvement": covering,
                                            "why": "It already gives the device the name the user said."})
    return "Done."


def _covering(improvements, goal, said):
    """An existing improvement that gives the goal's device the name the user said for it."""
    words = next((l.get("user_says") for l in said if l["turn"] == goal["turn"]), "")
    target = said_target(words)
    for item in improvements:
        for change in item.get("changes") or []:
            if (change.get("kind") == "registry" and change.get("entity_id") == goal.get("entity_id") and target
                    and any(_same(a, target[1]) for a in change.get("aliases") or [])):
                return item.get("improvement")
    return None


def _alias_idea(scenario, turn, revision_note=None, known=None, moved=None):
    """An idea for one goal: the name the user said for its device, as an alias beside those it has, when it isn't one
    of its names (known: {entity_id: card}, as devices_in_friction lists them). A revision note's quoted names (“…”)
    are what the household asked for besides: aliases too, beside the name said, which the conversation still needs.
    moved: the household's devices now, when a conflict sent back says the device is no longer there ({entity_id:
    card}, _moved): the alias goes on the one with the device's name."""
    line = next((t for t in scenario.get("turns") or [] if t.get("turn") == turn), None)
    goal = next(iter((line or {}).get("goals_started") or []), None)
    target = said_target((line or {}).get("user") or "")
    if not goal or not goal.get("entity_id") or not target:
        return None
    name = target[1]
    device = goal.get("summary", "").rsplit(" ", 1)[0] or goal["entity_id"]
    if any(_same(name, n) for n in [device, *_names((known or {}).get(goal["entity_id"]) or {})]):
        return None  # One of its own names: not a name Assist didn't know.
    names = [name]
    for quoted in re.findall(r"[“\"]([^”\"]+)[”\"]", revision_note or ""):
        if quoted.strip() and not any(_same(quoted, n) for n in names):
            names.append(quoted.strip())
    aliases = [n[:1].upper() + n[1:] for n in names]
    entity_id = (_named_in(device, moved) if moved else None) or goal["entity_id"]
    card = (moved or {}).get(entity_id) or (known or {}).get(entity_id) or {}
    now = [a for a in card.get("aliases") or [] if a]
    return {"title": f"Call the {device.lower()} the {' or the '.join(a.lower() for a in aliases)}",
            "cause": f"The user called the {device.lower()} the “{name}”, which isn't one of its names",
            "goals": [{"session": scenario["session"], "goal_turn": turn}],
            "expected": f"Asking for the {name} turns on the {device.lower()} in one turn",
            "support": [{"session": scenario["session"], "turn": turn, "note": f"The user said “{name}”"}],
            "conflicts": [],
            "changes": [{"kind": "registry", "entity_id": entity_id,
                         "aliases": now + [a for a in aliases if not any(_same(a, n) for n in now)]}]}


def _moved(ctx, payload):
    """When the household sent back a conflict over a device no longer in Home Assistant: its devices as it has them
    now, in every area (devices_in_area), to find that device under its new entity ID by its name; else None."""
    gone = [c for c in (payload.get("sent_back") or {}).get("conflicts") or []
            if "no longer in Home Assistant" in (c.get("why") or "")]
    if not gone or "devices_in_area" not in ctx.tools:
        return None
    areas = [a.get("area") for a in payload.get("areas") or [] if a.get("area")]
    return _cards(ctx.calls([("devices_in_area", {"area": area}) for area in areas])) if areas else None


REMEMBER = re.compile(r"^\s*remember\b[\s:,-]*(?:that\b\s*)?", re.I)
SCRIPT = re.compile(r"\bscript\b", re.I)


def _script_idea(scenario, turn):
    """An idea for one goal to turn a device on, whose conversation was sent in with a note asking for a script (“a
    reading lamp script ...”): a new script named as the user said the device, turning it on, exposed to Assist, so
    asking for it by that name runs the script."""
    line = next((t for t in scenario.get("turns") or [] if t.get("turn") == turn), None)
    goal = next(iter((line or {}).get("goals_started") or []), None)
    target = said_target((line or {}).get("user") or "")
    note = " ".join((scenario.get("note") or {}).get("served") or [])
    if not goal or goal.get("kind") != "state" or goal.get("state") != "on" or not target or not SCRIPT.search(note):
        return None
    name, entity_id = target[1], goal["entity_id"]
    return {"title": f"Add a {name} script",
            "cause": f"Nothing is called the “{name}”, and the household asked for a script by that name",
            "goals": [{"session": scenario["session"], "goal_turn": turn}],
            "expected": f"Asking for the {name} runs the script in one turn",
            "support": [{"session": scenario["session"], "turn": turn, "note": f"The note asked for a {name} script"}],
            "conflicts": [],
            "changes": [{"kind": "script", "id": re.sub(r"\W+", "_", name.lower()).strip("_"), "config": {
                "alias": name[:1].upper() + name[1:], "description": f"Turns on {entity_id}.",
                "sequence": [{"action": f"{entity_id.split('.', 1)[0]}.turn_on", "target": {"entity_id": entity_id}}]}}]}


SCENE = re.compile(r"\bscene\b", re.I)
AT = re.compile(r"\bat (\d{1,2}):(\d{2})\b")


def _scene_idea(scenario, turn):
    """An idea for one goal to turn a device on, whose conversation was sent in with a note asking for a scene and when
    to set it (“a reading lamp scene ... set every evening at 21:00”): a new scene named as the user said the device,
    the device on, exposed to Assist, so asking for it by that name sets it; and a new automation setting it then."""
    line = next((t for t in scenario.get("turns") or [] if t.get("turn") == turn), None)
    goal = next(iter((line or {}).get("goals_started") or []), None)
    target = said_target((line or {}).get("user") or "")
    note = " ".join((scenario.get("note") or {}).get("served") or [])
    if not goal or goal.get("kind") != "state" or goal.get("state") != "on" or not target or not SCENE.search(note):
        return None
    name, entity_id = target[1], goal["entity_id"]
    scene_id = re.sub(r"\W+", "_", name.lower()).strip("_")
    at = AT.search(note)
    at = f"{int(at[1]):02d}:{at[2]}:00" if at else "21:00:00"
    title = name[:1].upper() + name[1:]
    return {"title": f"Add a {name} scene, set at {at[:5]}",
            "cause": f"Nothing is called the “{name}”, and the household asked for a scene by that name",
            "goals": [{"session": scenario["session"], "goal_turn": turn}],
            "expected": f"Asking for the {name} sets the scene in one turn",
            "support": [{"session": scenario["session"], "turn": turn, "note": f"The note asked for a {name} scene"}],
            "conflicts": [],
            "changes": [{"kind": "scene", "id": scene_id, "config": {"name": title, "entities": {
                            entity_id: {"state": "on"}}}},
                        {"kind": "automation", "id": f"{scene_id}_at_{at[:2]}", "config": {
                            "alias": f"{title} at {at[:5]}", "triggers": [{"trigger": "time", "at": at}],
                            "actions": [{"action": "scene.turn_on", "target": {"entity_id": f"scene.{scene_id}"}}]}}]}


def _knowledge_idea(scenario, turn):
    """An idea for one goal whose conversation was sent in with a note asking Assist to remember a fact ("remember that
    the reading lamp is the desk lamp"): that fact, as a household knowledge line for everyone."""
    line = next((t for t in scenario.get("turns") or [] if t.get("turn") == turn), None)
    goal = next(iter((line or {}).get("goals_started") or []), None)
    note = next((c["quote"] for c in (scenario.get("note") or {}).get("conduct") or []
                 if REMEMBER.match(c.get("quote") or "")), None)
    fact = REMEMBER.sub("", note or "", count=1).strip().rstrip(".!")
    if not goal or not fact:
        return None
    text = fact[:1].upper() + fact[1:] + "."
    return {"title": f"Remember that {fact}",
            "cause": "Assist didn't know what the household told it to remember",
            "goals": [{"session": scenario["session"], "goal_turn": turn}],
            "expected": f"Asking for it again works in one turn, since Assist knows: {text}",
            "support": [{"session": scenario["session"], "turn": turn, "note": f"The note said: {note}"}],
            "conflicts": [],
            "changes": [{"kind": "preference", "for": "everyone", "text": text}]}


def _conduct_idea(scenario, turn):
    """An idea for one goal the household objected to Assist's promises on (a conduct goal, in a turn or the note): a
    household line asking Assist to say only what it did. Other objections get no idea of this kind."""
    objected = [c for t in scenario.get("turns") or [] for c in t.get("conduct") or []] + list(
        (scenario.get("note") or {}).get("conduct") or [])
    quotes = [c["quote"] for c in objected if c.get("goal_turn") == turn and re.search(r"\bpromis", c["quote"], re.I)]
    if not quotes:
        return None
    return {"title": "Say only what was done",
            "cause": "Assist's replies say more than what it did",
            "goals": [{"session": scenario["session"], "goal_turn": turn}],
            "expected": "Assist says only what it did, so the household needn't object",
            "support": [{"session": scenario["session"], "turn": turn, "note": f"They said: {quotes[0]}"}],
            "conflicts": [],
            "changes": [{"kind": "preference", "for": "everyone",
                         "text": "Say only what you did, without promising what happens next."}]}


def _check_idea(scenario, turn):
    """An idea for a question about a device that Assist answered wrongly (a fact goal left unmet): a household line
    asking it to check the device's state before answering."""
    line = next((t for t in scenario.get("turns") or [] if t.get("turn") == turn), None)
    goal = next(iter((line or {}).get("goals_started") or []), None)
    if not goal or goal.get("kind") != "fact" or goal.get("reached") is not False:
        return None
    return {"title": "Check before answering",
            "cause": "Assist said whether a device was on without checking it",
            "goals": [{"session": scenario["session"], "goal_turn": turn}],
            "expected": "Assist checks the device and answers right the first time",
            "support": [{"session": scenario["session"], "turn": turn, "note": goal.get("why") or "Answered wrongly"}],
            "conflicts": [],
            "changes": [{"kind": "preference", "for": "everyone",
                         "text": "Check a device's state before saying whether it's on or off."}]}


def hypothesis_generator(ctx, payload):
    """One idea per goal in trouble (a household line for the fact a note asked Assist to remember, asking it to say
    only what it did where the household objected to its promises, or asking it to check a device before answering a
    question it got wrong; a scene and an automation setting it, or a script, named as the device was said, for a note
    asking for one; else an alias for the name the user said), with both stamps: the reviewer's approval
    (answering its blocking comments) and Simulate's validation; sent on when validated, else not fixed."""
    note = (payload.get("sent_back") or {}).get("note")
    names = {d.get("entity_id"): d for d in payload.get("devices_in_friction") or []}
    moved = _moved(ctx, payload)
    sent, unfixed = [], []
    for scenario in payload.get("friction_scenarios") or []:
        for turn in scenario.get("goals_with_friction") or []:
            idea = (_knowledge_idea(scenario, turn) or _conduct_idea(scenario, turn) or _check_idea(scenario, turn)
                    or _scene_idea(scenario, turn) or _script_idea(scenario, turn)
                    or _alias_idea(scenario, turn, note, names, moved))
            if idea is None:
                unfixed.append({"session": scenario["session"], "goal_turn": turn,
                                "why": "It isn't about a name Assist didn't know."})
                continue
            review = ctx.call("ask_reviewer", idea)
            for _ in range(3):
                if not isinstance(review, dict) or review.get("outcome") != "request_changes":
                    break
                responses = [{"comment": c["comment"], "resolution": "resolved", "note": "Kept to the name said"}
                             for c in review.get("comments") or [] if "blocking" in c.get("decorations", [])]
                review = ctx.call("ask_reviewer", {**idea, "responses": responses})
            approved = isinstance(review, dict) and review.get("outcome") == "approve"
            test = ctx.call("test_idea", idea) if approved else None
            if isinstance(test, dict) and test.get("validated"):
                sent.append(idea)
            else:
                unfixed.append({"session": scenario["session"], "goal_turn": turn,
                                "why": "No idea for it was validated."})
    answer = {"hypotheses": sent, **({"not_fixed": unfixed} if unfixed else {})}
    accepted = ctx.call("submit_hypotheses", answer)
    return "Submitted." if isinstance(accepted, dict) and accepted.get("accepted") else "My answer wasn't accepted."


def hypothesis_reviewer(ctx, payload):
    """Reviews each draft: an alias that another device already answers to is blocked; anything else is approved."""
    while True:
        got = ctx.call("wait_for_draft")
        if not isinstance(got, dict) or "draft" not in got:
            return "Finished."
        draft = got["draft"]
        clash = _clash(draft)
        comment = ({"label": "issue", "decorations": ["blocking"], "subject": clash,
                    "evidence": [{"session": draft["goals"][0]["session"], "note": clash}]} if clash else
                   {"label": "praise", "decorations": ["non-blocking"], "subject": "Just the name the user said"})
        ctx.call("submit_review", {"comments": [comment]})


def _clash(draft):
    """Why an alias would make requests ambiguous, from the draft's own checks: what Home Assistant matched each new
    alias to (checks.implementation: alias_for, matches {alias: [entities]}, ambiguous)."""
    for check in (draft.get("checks") or {}).get("implementation") or []:
        if not isinstance(check, dict) or "alias_for" not in check:
            continue
        others = sorted({e for found in (check.get("matches") or {}).values() for e in found} - {check["alias_for"]})
        if check.get("ambiguous") or others:
            names = ", ".join(map(str, check.get("ambiguous") or others))
            return f"The new name would also match {names}"
    return None


def editor(ctx, payload):
    """The improvement in the household's words, from its changes as the editor's input words them ("Give “Desk lamp”
    the alias “Reading lamp”"): every new name the first device gets."""
    text = " ".join(payload.get("changes") or [])
    if found := re.findall(r"“([^”]+)” the alias “([^”]+)”", text):
        device = found[0][0].lower()
        alias = " or the ".join(a.lower() for d, a in found if d.lower() == device)
        headline = f"Call the {device} the {alias}"
        benefit = f"Ask for the {alias} and Assist knows you mean the {device}, so you don't have to explain."
    else:
        idea = (payload.get("idea") or "Make requests like these go better").rstrip(".")
        headline, benefit = idea[:1].upper() + idea[1:60], "Requests like these go better, as the tests showed."
    accepted = ctx.call("submit_copy", {"headline": headline[:60], "benefit": benefit})
    return "Written." if isinstance(accepted, dict) and accepted.get("accepted") else "My answer wasn't accepted."


def feedback_check(ctx, payload):
    """The judge: whether each replay turn it's asked about shows what the household objected to: a promise of what
    happens next, for an objection to promising; a reply saying it couldn't do what was asked, for any other. Asked
    about what the user wanted to know, whether the reply says the device's state as the home had it."""
    if "questions" in payload:
        replies = {turn[0]["turn"]: turn[3]["assistant_replied"] or "" for turn in payload.get("turns") or []}
        answers = []
        for question in payload["questions"]:
            reply = replies.get(question["turn"], "")
            truth = [d.get("state") for d in question.get("home") or []]
            gives = bool(truth) and all(re.search(rf"\bis {re.escape(str(t))}\b", reply, re.I) for t in truth)
            why = "It says the device's state." if gives else f"The home had it {', '.join(map(str, truth))}."
            answers.append({"goal": question["goal"], "turn": question["turn"], "gives": gives, "why": why})
        return {"answers": answers}
    replies = {turn[0]["turn"]: turn[3]["assistant_replied"] or "" for turn in payload.get("replay") or []}
    answers = []
    for objection in payload.get("objections") or []:
        shown = PROMISE if re.search(r"\bpromis", objection.get("quote") or "", re.I) else FAILURE
        answers += [{"goal": objection["goal"], "turn": n, "shows": bool(shown.search(replies.get(n, ""))),
                     "why": "It still does that." if shown.search(replies.get(n, "")) else "It doesn't do that."}
                    for n in objection.get("judge") or []]
    return {"answers": answers}


ROLES = {"submit_goals": journey_analyzer, "submit_hypotheses": hypothesis_generator,
         "wait_for_draft": hypothesis_reviewer, "submit_copy": editor}


def role_of(request):
    """Who is asking, from what the request carries: the role's name, or "household"."""
    tools = {t.get("name") for t in request.get("tools") or []}
    for tool, role in ROLES.items():
        if tool in tools:
            return role.__name__
    schema = (((request.get("output_config") or {}).get("format") or {}).get("schema") or {})
    properties = set((schema.get("properties") or {}))
    if {"smooth", "sure"} <= properties:
        return "triage"
    if "answers" in properties:
        return "feedback_check"
    return "household"


def answer(request):
    """(role, blocks) for one Messages API request."""
    role = role_of(request)
    if role == "household":
        return role, household(request)
    script = {"triage": triage, "feedback_check": feedback_check}.get(role) or next(
        r for r in ROLES.values() if r.__name__ == role)
    ctx = Ctx(request)
    try:
        reply = script(ctx, payload_of(request))
    except Turn as turn:
        return role, turn.blocks
    return role, [{"type": "text", "text": json.dumps(reply) if isinstance(reply, dict) else reply}]


# The server.

def _capabilities(efforts, thinking):
    """A model's capabilities as the Models API gives them, every key present: the effort levels and thinking types
    it takes, and the rest supported (the structured output each AI role asks for among them)."""
    yes = {"supported": True}
    return {"batch": yes, "citations": yes, "code_execution": yes, "image_input": yes, "pdf_input": yes,
            "structured_outputs": yes, "context_management": {"supported": True},
            "server_tools": {"supported": True, "code_execution": yes, "web_search": yes},
            "effort": {"supported": bool(efforts), **{e: {"supported": e in efforts} for e in EFFORTS}},
            "thinking": {"supported": bool({"adaptive", "enabled"} & set(thinking)), "types": {
                t: {"supported": t in thinking} for t in ("adaptive", "between_tools", "disabled", "enabled")}}}


def _message(blocks, model, number):
    calls = any(b["type"] == "tool_use" for b in blocks)
    return {"id": f"msg_e2e_{number:06d}", "type": "message", "role": "assistant", "model": model,
            "content": blocks, "stop_reason": "tool_use" if calls else "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 20}}


def _events(message):
    """The message as Anthropic streams it: tool inputs as JSON deltas, text as text deltas."""
    start = {**message, "content": [], "stop_reason": None, "usage": {"input_tokens": 100, "output_tokens": 1}}
    yield "message_start", {"type": "message_start", "message": start}
    for n, block in enumerate(message["content"]):
        if block["type"] == "tool_use":
            yield "content_block_start", {"type": "content_block_start", "index": n,
                                          "content_block": {**block, "input": {}}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            yield "content_block_delta", {"type": "content_block_delta", "index": n, "delta": delta}
        else:
            yield "content_block_start", {"type": "content_block_start", "index": n,
                                          "content_block": {"type": "text", "text": ""}}
            yield "content_block_delta", {"type": "content_block_delta", "index": n,
                                          "delta": {"type": "text_delta", "text": block["text"]}}
        yield "content_block_stop", {"type": "content_block_stop", "index": n}
    yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"],
                                                               "stop_sequence": None},
                            "usage": {"output_tokens": 20}}
    yield "message_stop", {"type": "message_stop"}


class FakeAnthropic:
    """The stand-in Messages API on 127.0.0.1, or the address given (a free port): start(), url, requests (the log),
    stop()."""

    def __init__(self, api_key, host="127.0.0.1"):
        self.api_key, self.host = api_key, host
        self.requests = []  # {at, path, role, request, reply}: every request, as answered.
        self.lock = threading.Lock()
        self.numbers = itertools.count(1)
        self.server = self.thread = None

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # Kept in fake.requests instead.
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):  # The key as x-api-key, or as a bearer token (the anthropic SDK's from 2.x).
                key = self.headers.get("x-api-key") or (self.headers.get("authorization") or "").removeprefix("Bearer ")
                if key == fake.api_key:
                    return True
                self._send(401, {"type": "error", "error": {"type": "authentication_error",
                                                            "message": "invalid x-api-key"}})
                return False

            def do_GET(self):
                if not self._authorized():
                    return
                path = self.path.split("?", 1)[0].rstrip("/")
                models = [{"type": "model", "id": i, "display_name": name, "created_at": "2026-01-01T00:00:00Z",
                           "lifecycle": "active", "capabilities": _capabilities(efforts, thinking)}
                          for i, name, efforts, thinking in MODELS]
                if path == "/v1/models":
                    self._send(200, {"data": models, "has_more": False, "first_id": models[0]["id"],
                                     "last_id": models[-1]["id"]})
                elif (found := next((m for m in models if path == f"/v1/models/{m['id']}"), None)) is not None:
                    self._send(200, found)
                else:
                    self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self._authorized():
                    return
                if self.path.split("?", 1)[0].rstrip("/") != "/v1/messages":
                    self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
                    return
                try:
                    role, blocks = answer(body)
                except Exception as err:  # noqa: BLE001 - the harness's own bug: reported as the API reports one.
                    fake._log(self.path, "error", body, repr(err))
                    self._send(500, {"type": "error", "error": {"type": "api_error", "message": repr(err)}})
                    return
                message = _message(blocks, body.get("model", MODELS[0][0]), next(fake.numbers))
                fake._log(self.path, role, body, blocks)
                if not body.get("stream"):
                    self._send(200, message)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for event, data in _events(message):
                    if event == "message_delta" and role == "household" and message["stop_reason"] == "end_turn":
                        time.sleep(TAIL)
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
                self.close_connection = True

        self.server = ThreadingHTTPServer((self.host, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-anthropic", daemon=True)
        self.thread.start()
        return self

    def _log(self, path, role, request, reply):
        with self.lock:
            self.requests.append({"at": datetime.now(UTC).isoformat(), "path": path, "role": role,
                                  "request": request, "reply": reply})

    def roles(self, since=0):
        """Who was asked, in order, from request number `since` on."""
        with self.lock:
            return [r["role"] for r in self.requests[since:]]

    def dump(self, path, since=0):
        """The log from request number `since` on, as JSON lines (for a failing test's report)."""
        with self.lock, open(path, "w") as out:
            for record in self.requests[since:]:
                out.write(json.dumps(record) + "\n")

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
