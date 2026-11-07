"""The leak guard: no household detail in a tree or a commit message that goes public.

  tools/leak_guard.py [tree]               the working copy's files: tracked ones, and new ones not ignored
  tools/leak_guard.py commits REV...       each commit `git rev-list REV...` names: its message and its whole tree
  tools/leak_guard.py push REMOTE URL      the pre-push hook (tools/git-hooks/leak-guard): each commit pushed to the
                                           public repository (tools/passed.py's REPO), and an annotated tag's
                                           message; pushes elsewhere pass
  tools/leak_guard.py hashes               the terms, hashed, for CI's HOUSEHOLD_TERMS secret
  tools/leak_guard.py looked-at IMAGE...   record that you opened these images and checked them by eye
  --hashed FILE (- for stdin), before any of these: the terms from such a list, not the private folder; an empty one
  (no secret, as on a fork's pull request) skips the check

An image (a file that isn't text) has no words to match, so it's refused until a person has opened it, checked it by
eye, and recorded its exact bytes with `looked-at` (in tools/looked-at.txt; no OCR).

The terms come from the private folder (tools/private.py): publish-denylist.txt as written, and terms derived from the
household's own stored data, every JSON file there:
- recording IDs, by the first 8 characters they're cited by;
- people's names, whole and word by word, and the owners in names like "Sam's Room";
- the names of its devices, entities, rooms, scripts, automations and assistants, and the home's, whole;
- its devices' makers and models, whole, a model's words before its number too ("Halo QX 9": "Halo QX"), and each word
  of a maker's name (brands);
- its entity IDs, whole;
- every MAC address in it (a 64-bit one too), as written and as its hex digits alone, and every global IP address
  (ipaddress's is_global, and an IPv6 one in 2000::/3), but not a version ("sw_version": "1.2.3.4").

Where the line is: a derived name or ID counts only when one of its words isn't generic, and a word alone only with 3
letters or more. Generic is a word in the system's word list (WORDS, its lowercase entries, plural and -ed/-ing endings
taken off: "kitchen", "garage", "doors"), a number, a word of one or two letters, one of Home Assistant's entity
domains ("tts"), or one in VOCABULARY. People's names never are. So "Garage door", "Living Room" and "light.kitchen"
stay allowed, while a brand, a model, a made-up name, a name with a person's in it, or an ID with one of these is
refused. In a maker's or model's name a short word in capitals counts ("Halo X", "QX"). A possessive's owner counts when
it isn't generic or is a first name (the word list's propernames). Recording IDs, people's names, addresses and the
denylist always count. The maintainer's own name (MAINTAINER) is allowed outside the shipped integration, as in
citations and SPEC.md, and refused inside it, whose code names no one.

Matching goes by words: a text's words (letters and digits, lowercased) are compared a run at a time with each term's,
and a run of more than 8 hex digits also by its first 8. A term that holds a shorter one is left out, as that one finds
it. The hashed list holds each term's words as sha256 (its first 16 hex digits, so the list fits a GitHub secret), so CI
checks without the terms; a hit there says where, never what. A change to VOCABULARY or the rules needs a new list.
"""

import hashlib
import importlib.util
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = "custom_components/"
MAINTAINER = {"kai"}
# Generic too: words the 1934 word list lacks, the AI providers and models the project names, and its own names.
VOCABULARY = {"app", "dryer", "homeassistant", "inc", "llc", "ltd", "plugin", "ssh", "wifi", "xyz",
              "anthropic", "claude", "haiku", "deepseek", "google", "openai", "openrouter", "hacs", "thngkaiyuan"}
WORDS = Path("/usr/share/dict/words")
NAMES = Path("/usr/share/dict/propernames")
WORD = re.compile(r"[^\W_]+")
HEX = re.compile(r"[0-9a-f]{8,}")
ENTITY_ID = re.compile(r"[a-z_]+\.[a-z0-9_]+")
MAC = re.compile(r"(?<![0-9a-f:-])[0-9a-f]{2}([:-])[0-9a-f]{2}(?:\1[0-9a-f]{2}){4}(?:\1[0-9a-f]{2}\1[0-9a-f]{2})?"
                 r"(?![0-9a-f:-])", re.I)
IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|(?<![0-9a-f:])[23][0-9a-f]{3}(?::[0-9a-f]{0,4}){2,7}(?![0-9a-f:])", re.I)
NONE = "0" * 40  # A ref git creates or deletes, on a pre-push hook's stdin.
LOOKED_AT = "tools/looked-at.txt"  # The images a person has looked at, by their exact bytes: "sha256  path  date" each.
UNSEEN = "an image nobody has looked at"


def words(text):
    return [w.casefold() for w in WORD.findall(text)]


def digest(term_words):
    return hashlib.sha256(" ".join(term_words).encode()).hexdigest()[:16]


class Terms:
    """The terms to refuse: {(the digest of a term's words, how many): whether it's the maintainer's own name}."""

    def __init__(self, digests):
        self.digests = digests
        self.lengths = sorted({n for _, n in digests})

    @classmethod
    def of(cls, refused, mine=()):
        """From plain terms, strings or word tuples: those refused everywhere, and the maintainer's own name (MAINTAINER
        is, wherever it's listed). A term holding a shorter one refused everywhere is left out."""
        def split(terms):
            return {tuple(words(t)) if isinstance(t, str) else tuple(t) for t in terms} - {()}
        mine = split(mine) | {t for t in split(refused) if " ".join(t) in MAINTAINER}
        refused = split(refused) - mine
        kept = {t for t in refused if not any(t[i:i + n] in refused for n in range(1, len(t))
                                              for i in range(len(t) - n + 1))}
        return cls({(digest(t), len(t)): t in mine for t in kept | mine})

    @classmethod
    def hashed(cls, lines):
        """From a hashed list: "digest words [maintainer]" a line (lines())."""
        listed = {}
        for line in lines:
            if (parts := line.split()) and not parts[0].startswith("#"):
                listed[(parts[0], int(parts[1]))] = parts[2:] == ["maintainer"]
        return cls(listed)

    def lines(self):
        return [f"{sha} {n}" + (" maintainer" if mine else "") for (sha, n), mine in sorted(self.digests.items())]

    def found(self, text, shipped):
        """Where the text holds a term: [(offset, the words matched)]. The maintainer's name only in shipped code."""
        spans = [(m.start(), m.group().casefold()) for m in WORD.finditer(text)]
        tokens = [token for _, token in spans]
        hits = []
        for i, (at, token) in enumerate(spans):
            runs = [tokens[i:i + n] for n in self.lengths if i + n <= len(tokens)]
            if HEX.fullmatch(token) and len(token) > 8:
                runs.append([token[:8]])
            for run in runs:
                mine = self.digests.get((digest(run), len(run)))
                if mine is not None and (shipped or not mine):
                    hits.append((at, " ".join(run)))
        return hits


def check(terms, items, hashed=False):
    """Every hit in items, (where, text, shipped) each: one line per hit, saying where (its line, in a text of more
    than one) and what, unless hashed. An image, its bytes in place of a text, is a hit until someone looks at it."""
    out, seen = [], looked_at()
    for where, text, shipped in items:
        if isinstance(text, bytes):
            out += [f"{where}: {UNSEEN}"] if hashlib.sha256(text).hexdigest() not in seen else []
            continue
        for at, matched in terms.found(text, shipped):
            line = f":{text.count(chr(10), 0, at) + 1}" if "\n" in text.rstrip("\n") else ""
            out.append(f"{where}{line}: " + ("a household term (hashed)" if hashed else f"'{matched}'"))
    return out


def paths(names):
    """Items to check: each file's path."""
    return [(f"{name} (its path)", name, name.startswith(SHIPPED)) for name in names]


def texts(found):
    """Items to check: each file's text, from (path, its bytes) each; an image's bytes as they are."""
    for name, data in found:
        try:
            text = data.decode()
        except UnicodeDecodeError:  # An image.
            text = data
        yield name, text, name.startswith(SHIPPED)


def looked_at():
    """The digests of the images someone has looked at (LOOKED_AT)."""
    record = ROOT / LOOKED_AT
    lines = record.read_text().splitlines() if record.exists() else []
    return {line.split()[0] for line in lines if line.strip() and not line.startswith("#")}


def look(images, today=None):
    """Record that a person opened these images and checked them by eye, as they are now: the lines added."""
    from datetime import date
    root, seen, added = ROOT.resolve(), looked_at(), []
    for path in map(Path, images):
        if (sha := hashlib.sha256(path.read_bytes()).hexdigest()) not in seen:
            seen.add(sha)
            name = path.resolve().relative_to(root) if path.resolve().is_relative_to(root) else path
            added.append(f"{sha}  {name}  {today or date.today().isoformat()}")
    with (ROOT / LOOKED_AT).open("a") as record:
        record.write("".join(f"{line}\n" for line in added))
    return added


# --- The terms, from the private folder -------------------------------------------------------------------------------

def vocabulary():
    """Generic words: the word list's lowercase entries, and VOCABULARY."""
    if not WORDS.exists():
        sys.exit(f"No word list at {WORDS} to tell generic words from household ones (tools/leak_guard.py).")
    return {w for w in WORDS.read_text().split() if w.islower()} | VOCABULARY


def generic(word, known, strict=False):
    """Whether a word, as written, tells nothing of a household (the module's docstring); strict: a maker's or a
    model's, where a short word in capitals tells ("Halo X", "QX")."""
    if word.isdigit():
        return True
    if len(word) <= 2:
        return not (strict and word.isupper())
    word = word.casefold()
    stems = [word] + [word[:-n] for n in (1, 2, 3) if word.endswith(("s", "es", "ing")[n - 1])]
    stems += [word[:-1], word[:-2]] if word.endswith("ed") else []
    stems += [word[:-3] + "y"] if word.endswith("ies") else []
    return any(stem in known for stem in stems)


def telling(name, known, strict=False):
    """A name's words, lowercased, if one of them isn't generic; else None."""
    found = WORD.findall(name)
    return [w.casefold() for w in found] if any(not generic(w, known, strict) for w in found) else None


def addresses(text):
    """The MAC addresses and global IP addresses a text holds, as terms: a MAC's words, and its hex digits as one."""
    found = {term for m in MAC.finditer(text) for term in (tuple(words(m[0])), ("".join(words(m[0])),))}
    for m in IP.finditer(text):
        try:
            found |= {tuple(words(m[0]))} if ipaddress.ip_address(m[0]).is_global else set()
        except ValueError:  # Not an address: a time, say.
            pass
    return found


class Household:
    """What the household's stored data names, by kind, read from every JSON file in a folder."""

    def __init__(self):
        self.people, self.names, self.makers, self.brands, self.ids, self.recordings, self.addresses = (
            set() for _ in range(7))

    def read(self, folder):
        for path in sorted(Path(folder).rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                try:
                    data = json.loads(path.read_text())
                except (UnicodeDecodeError, ValueError):
                    continue
                self.walk(data)
                if HEX.fullmatch(rid := path.name.removeprefix("assist_improver.session_").removesuffix(".json")):
                    self.recordings.add(rid[:8])
        return self

    def walk(self, value):
        if isinstance(value, str):
            self.addresses |= addresses(value)
            return
        if isinstance(value, list):
            for item in value:
                self.walk(item)
            return
        if not isinstance(value, dict):
            return
        if isinstance(entity_id := value.get("entity_id"), str) and ENTITY_ID.fullmatch(entity_id):
            self.ids.add(entity_id)
            if isinstance(value.get("attributes"), dict):  # A state.
                name = value["attributes"].get("friendly_name")
                (self.people if entity_id.startswith("person.") else self.names).add(str(name or ""))
        for key, item in value.items():
            self.walk(key)
            if key in ("states", "entities") and isinstance(item, dict):
                self.ids |= {k for k in item if ENTITY_ID.fullmatch(k)}
            if key in ("entities", "devices", "areas", "scripts") and isinstance(item, (dict, list)):
                for entry in item.values() if isinstance(item, dict) else item:
                    if isinstance(entry, dict):
                        self.names |= {str(entry.get(k) or "") for k in ("name", "name_by_user", "original_name",
                                                                         "alias")}
                        self.names |= {str(a) for a in entry.get("aliases") or [] if isinstance(a, str)}
                        self.makers |= {str(entry.get(k) or "") for k in ("manufacturer", "model")}
                        self.brands.add(str(entry.get("manufacturer") or ""))
            if key == "automations" and isinstance(item, list):
                self.names |= {str(a.get("alias") or "") for a in item if isinstance(a, dict)}
            if key == "provider_config" and isinstance(item, list):
                self.names |= {str(c.get("title") or "") for c in item if isinstance(c, dict)}
            if key == "home_name" and isinstance(item, str):
                self.names.add(item)
            if key == "recording_id" and isinstance(item, str) and HEX.fullmatch(item):
                self.recordings.add(item[:8])
            if isinstance(item, str) and key.endswith("version"):  # A firmware's "1.2.3.4" is no address.
                continue
            self.walk(item)

    def terms(self, known, first_names=frozenset()):
        """The terms, as word tuples: (refused everywhere, the maintainer's own name). Home Assistant's own words, its
        entity domains, are generic; the people's names aren't, wherever they're in a name."""
        known = known | {w for i in self.ids for w in words(i.split(".", 1)[0])}
        refused, mine = {(rid,) for rid in self.recordings} | self.addresses, {(w,) for w in MAINTAINER}
        for person in filter(None, self.people):  # Whole, and word by word.
            found = words(person)
            (mine if MAINTAINER & set(found) else refused).update([tuple(found), *((w,) for w in found if w[1:])])
        for name in self.names | self.people:  # The owners in names like "Sam's Room".
            found = words(name)
            refused |= {(owner,) for owner, s in zip(found, found[1:])
                        if s == "s" and (not generic(owner, known) or owner in first_names)}
        names = {w for term in refused | mine if len(term) == 1 for w in term}
        known = known - names
        for name in self.names:
            if found := telling(name, known):
                refused.add(tuple(found))
        for maker in self.makers:  # Whole, and a model's words before its number ("Halo QX 9": "Halo QX").
            found = WORD.findall(maker)
            cut = next((n for n, w in enumerate(found) if any(c.isdigit() for c in w)), len(found))
            refused |= {tuple(t) for t in (telling(" ".join(found), known, True),
                                           cut > 1 and telling(" ".join(found[:cut]), known, True)) if t}
        for maker in self.brands:  # And a maker's own words.
            refused |= {(w,) for w in words(maker) if not generic(w, known)}
        refused |= {tuple(words(i)) for i in self.ids if telling(i.split(".", 1)[1], known)}
        return {term for term in refused if term[1:] or term[0][2:]} - mine, mine  # A word alone: 3 letters or more.


def derived(folder, known, first_names=frozenset()):
    """The terms the household's stored data in folder gives: (refused everywhere, the maintainer's own name)."""
    return Household().read(folder).terms(known, first_names)


def tool(name):
    """One of tools/: private.py, passed.py."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_terms():
    """The private folder's terms: its denylist, and those its stored data gives."""
    private = tool("private")
    folder = private.path()
    denylist = private.path("publish-denylist.txt").read_text().splitlines()
    known = vocabulary()
    first_names = {w.casefold() for w in NAMES.read_text().split()} if NAMES.exists() else set()
    refused, mine = derived(folder, known, first_names)
    return Terms.of([*refused, *(line.strip() for line in denylist if line.strip() and not line.startswith("#"))],
                    mine)


# --- What's checked ---------------------------------------------------------------------------------------------------

def git(*args, data=None):
    return subprocess.run(["git", "-C", str(ROOT), *args], input=data, capture_output=True, check=True).stdout


def tree_items():
    """The working copy's files: tracked ones, and new ones not ignored."""
    names = [n for n in git("ls-files", "-z", "-co", "--exclude-standard").decode().split("\0")
             if n and (ROOT / n).is_file()]
    return [*paths(names), *texts((n, (ROOT / n).read_bytes()) for n in names)]


def commit_items(revs):
    """Each commit's message, then every path in their trees, then each file's text once for each content (and
    whether it ships)."""
    commits = [r.split("\n", 1) for r in git("log", "-z", "--format=%H%n%B", *revs).decode().split("\0") if r.strip()]
    names, blobs = set(), {}
    for sha, message in commits:
        yield f"commit {sha[:12]}'s message", message, False
        for entry in filter(None, git("ls-tree", "-r", "-z", sha).decode().split("\0")):
            meta, name = entry.split("\t", 1)
            if meta.split()[1] == "blob":
                names.add(name)
                blobs.setdefault((meta.split()[2], name.startswith(SHIPPED)), name)
    yield from paths(sorted(names))
    out = git("cat-file", "--batch", data="".join(f"{blob}\n" for blob, _ in blobs).encode()) if blobs else b""
    found, at = [], 0
    for name in blobs.values():  # Each: "<blob> blob <size>\n<content>\n".
        start = out.index(b"\n", at) + 1
        end = start + int(out[at:start].split()[2])
        found.append((name, out[start:end]))
        at = end + 1
    yield from texts(found)


def pushed(remote, url, refs, repo):
    """The pre-push hook's revisions: each ref pushed (refs: the hook's stdin lines), less what the remote has. None
    for a push to anywhere but the public repository (repo, "owner/name")."""
    if repo not in url:
        return None
    revs = [parts[1] for parts in map(str.split, refs) if len(parts) == 4 and parts[1] != NONE]
    return revs + ["--not", f"--remotes={remote}"] if revs else []


def tag_items(refs):
    """Items to check: each annotated tag pushed, its message, which git log (reading a tag as its commit) doesn't
    show."""
    for parts in map(str.split, refs):
        if len(parts) == 4 and parts[0].startswith("refs/tags/") and parts[1] != NONE \
                and git("cat-file", "-t", parts[1]).strip() == b"tag":
            message = git("cat-file", "tag", parts[1]).decode().partition("\n\n")[2]
            yield f"tag {parts[0].removeprefix('refs/tags/')}'s message", message, False


def main(args):
    hashed = None
    if args[:1] == ["--hashed"]:
        listed = sys.stdin.read() if args[1] == "-" else Path(args[1]).read_text()
        hashed, args = [line for line in listed.splitlines() if line.strip()], args[2:]
        if not hashed:
            print("leak guard: no hashed terms given (none for a fork's pull request): skipped")
            return 0
    command, rest = (args[0], args[1:]) if args else ("tree", [])
    if command == "looked-at":
        if not rest:
            sys.exit(__doc__)
        print(f"Recorded {len(look(rest))} image(s) as looked at in {LOOKED_AT}.")
        return 0
    tags = []
    if command == "push":
        refs = sys.stdin.read().splitlines()
        revs = pushed(rest[0], rest[1], refs, tool("passed").REPO)
        if revs is None:
            return 0
        command, rest, tags = "commits", revs, list(tag_items(refs))
    terms = Terms.hashed(hashed) if hashed else private_terms()
    if command == "hashes":
        print("\n".join(terms.lines()))
        return 0
    if command == "tree":
        items = tree_items()
    elif command == "commits":
        items = [*tags, *(commit_items(rest) if rest else ())]
    else:
        sys.exit(__doc__)
    hits = check(terms, items, hashed=bool(hashed))
    for hit in hits:
        print(hit)
    if any(hit.endswith(UNSEEN) for hit in hits):
        print("Open each image named, check it by eye, then: .venv/bin/python tools/leak_guard.py looked-at IMAGE...")
    print(f"leak guard: {len(hits)} found" if hits else "leak guard: nothing found")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
