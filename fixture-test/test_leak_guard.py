"""The leak guard (tools/leak_guard.py), on a made-up household: a household term in a tree, a commit's message or a
path is refused, whether the terms come from the private folder or from a hashed list of them; generic words pass; an
image passes only once someone has looked at it; without the private folder it stops. Git stands in, reading made-up
commits, and so does the system's word list."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("leak_guard", ROOT / "tools/leak_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

GENERIC = ("a alone and ask audio box close dim door garage heater in it kitchen light living media morgan of on play "
           "player room speaker study switch the to turn")
RECORDING = "5f3a9c0e" + "7" * 56
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
LOOK = "Open each image named, check it by eye, then: .venv/bin/python tools/leak_guard.py looked-at IMAGE..."


@pytest.fixture
def household(tmp_path, monkeypatch):
    """A made-up household's private folder, and a small word list."""
    (tmp_path / "words").write_text("\n".join(GENERIC.split() + ["Zentro"]) + "\n")  # A capital: not generic.
    (tmp_path / "propernames").write_text("Morgan\nRobin\n")
    monkeypatch.setattr(guard, "WORDS", tmp_path / "words")
    monkeypatch.setattr(guard, "NAMES", tmp_path / "propernames")
    folder = tmp_path / "private"
    (folder / "stores").mkdir(parents=True)
    (folder / "publish-denylist.txt").write_text("# Refused as written.\nLakeview Lane\n")
    states = {"person.robin_vale": "Robin Vale", "person.kai": "Kai", "light.kitchen": "Kitchen light",
              "media_player.zentro_box": "Living room speaker", "switch.lumo_heater": "Heater",
              "cover.garage_door": "Quillon"}
    (folder / "stores/assist_improver.cases").write_text(json.dumps({"data": {
        "recording_id": RECORDING,
        "states": {i: {"entity_id": i, "attributes": {"friendly_name": name}} for i, name in states.items()},
        "devices": {"d1": {"name": "Living room box", "manufacturer": "Zentro Audio", "model": "Halo QX 9",
                           "sw_version": "1.2.3.4", "connections": [["mac", "02:00:00:00:00:01"]]}},
        "areas": {"a1": {"name": "Morgan's Room"}, "a2": {"name": "Garage"}},
        "network": {"dns": "http://1.1.1.1:53", "lan": "10.0.0.20",
                    "seen": {"2606:4700:4700::1111": "12:30:00", "::ffff:8.8.8.8": "dead::beef"}}}}))
    monkeypatch.setenv("ASSIST_IMPROVER_PRIVATE", str(folder))
    return guard.private_terms()


def hits(terms, text, shipped=False, hashed=False):
    return [hit.split(": ", 1)[1] for hit in guard.check(terms, [("file", text, shipped)], hashed)]


def test_the_households_terms_are_refused_and_generic_words_pass(household):
    """The line: a name or ID counts only with a word that isn't generic; people's names, a maker's words, recording
    IDs and the denylist always do. A term that holds a shorter one is found by that one."""
    assert hits(household, "Turn on the kitchen light (light.kitchen), close the garage door, dim the Living Room "
                           "speaker. Play it in the study.") == []
    assert hits(household, "Ask Robin to dim it.") == ["'robin'"]
    assert hits(household, "A Zentro Audio speaker") == ["'zentro'"]
    assert hits(household, "The Halo QX.") == ["'halo qx'"]  # A model, its words before its number.
    assert hits(household, "media_player.zentro_box") == ["'zentro'"]
    assert hits(household, "switch.lumo_heater, and Lumo alone") == ["'switch lumo heater'"]  # An ID, whole.
    assert hits(household, "Open Quillon, then the heater.") == ["'quillon'"]  # A name of one word, as one of more.
    assert hits(household, "Turn on Morgan's Room.") == ["'morgan'"]  # An owner: a first name, though generic.
    assert hits(household, f"Recording {RECORDING[:8]}, in full {RECORDING}") == [f"'{RECORDING[:8]}'"] * 2
    assert hits(household, "We live on Lakeview Lane.") == ["'lakeview lane'"]


def test_mac_addresses_and_global_ip_addresses_are_refused(household):
    """Wherever the stored data holds them: a MAC however it's written, and a global IP address, an IPv4 one inside an
    IPv6 one too; a private address, a time, hex words or a firmware's version tells nothing."""
    assert hits(household, "02:00:00:00:00:01, 02-00-00-00-00-01 or 020000000001") == [
        "'02 00 00 00 00 01'", "'02 00 00 00 00 01'", "'020000000001'"]
    assert hits(household, "Ask 1.1.1.1, 8.8.8.8 or 2606:4700:4700::1111, not 10.0.0.20, at 12:30:00; dead beef, "
                           "version 1.2.3.4.") == ["'1 1 1 1'", "'8 8 8 8'", "'2606 4700 4700 1111'"]


def test_the_maintainers_name_is_allowed_outside_the_integration_and_refused_inside(household):
    assert hits(household, "Kai's note: a citation.", shipped=False) == []
    assert hits(household, "Kai's note: a citation.", shipped=True) == ["'kai'"]


def test_a_hashed_list_refuses_without_holding_or_naming_the_terms(household, tmp_path, monkeypatch, capsys):
    assert guard.main(["hashes"]) == 0
    listed = capsys.readouterr().out
    assert "robin" not in listed.casefold() and len(listed.splitlines()) == len(household.digests)
    hashed = guard.Terms.hashed(listed.splitlines())
    assert hits(hashed, "Ask Robin.\nA Zentro speaker.", hashed=True) == ["a household term (hashed)"] * 2
    assert hits(hashed, "Turn on the kitchen light.", hashed=True) == []
    assert hits(hashed, "Kai's note.", hashed=True) == [] and hits(hashed, "Kai", shipped=True, hashed=True)
    (tmp_path / "terms").write_text(listed)
    monkeypatch.setattr(guard, "git", Git([("c1", "Ask Robin.", {"README.md": b"Generic words."})]))
    assert guard.main(["--hashed", str(tmp_path / "terms"), "commits", "HEAD"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "commit c1's message: a household term (hashed)", "leak guard: 1 found"]


def test_an_empty_hashed_list_skips_the_check(tmp_path, capsys):
    """CI on a fork's pull request has no secret, so the list it passes is empty."""
    (tmp_path / "terms").write_text("\n")
    assert guard.main(["--hashed", str(tmp_path / "terms"), "commits", "HEAD"]) == 0
    assert "skipped" in capsys.readouterr().out


class Git:
    """A stand-in for the git commands the guard runs, on made-up commits: (sha, message, {path: bytes}) each."""

    def __init__(self, commits, files=(), tags=None):
        self.commits, self.files, self.tags = commits, files, tags or {}

    def __call__(self, *args, data=None):
        blob = {hashlib.sha1(content).hexdigest(): content for _, _, tree in self.commits for content in tree.values()}
        if args[0] == "ls-files":
            return "".join(f"{name}\0" for name in self.files).encode()
        if args[0] == "log":
            return "".join(f"{sha}\n{message}\n\0" for sha, message, _ in self.commits).encode()
        if args[0] == "ls-tree":
            tree = next(tree for sha, _, tree in self.commits if sha == args[-1])
            return "".join(f"100644 blob {hashlib.sha1(c).hexdigest()}\t{p}\0" for p, c in tree.items()).encode()
        if args[:2] == ("cat-file", "-t"):
            return b"tag\n" if args[2] in self.tags else b"commit\n"
        if args[:2] == ("cat-file", "tag"):
            return self.tags[args[2]].encode()
        if args[0] == "cat-file":
            return b"".join(f"{b} blob {len(blob[b])}\n".encode() + blob[b] + b"\n" for b in data.decode().split())
        raise AssertionError(args)


def test_each_commit_is_checked_its_message_its_paths_and_its_files(household, monkeypatch, capsys):
    shipped = "custom_components/assist_improver/prompts.py"
    monkeypatch.setattr(guard, "git", Git([
        ("c2", "Tune Robin's study.", {"README.md": b"Generic words.", "docs/zentro.md": b"Close the door.",
                                        "assets/panel.png": PNG}),
        ("c1", "Add the speaker card.\n\nTurn on the kitchen light.", {"README.md": b"Generic words.",
                                                                       shipped: b"# Kai's example\n"})]))
    assert guard.main(["commits", "HEAD"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "commit c2's message: 'robin'", "docs/zentro.md (its path): 'zentro'",
        "assets/panel.png: an image nobody has looked at", f"{shipped}: 'kai'", LOOK, "leak guard: 4 found"]


def test_the_tree_is_checked_tracked_files_and_new_ones(household, tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "checkout"
    (checkout / "docs").mkdir(parents=True)
    (checkout / "docs/guide.md").write_text("Turn on the kitchen light.\n\nAsk Robin first.\n")
    (checkout / "README.md").write_text("Close the garage door.\n")
    monkeypatch.setattr(guard, "ROOT", checkout)
    monkeypatch.setattr(guard, "git", Git([], files=["README.md", "docs/guide.md"]))
    assert guard.main(["tree"]) == 1
    assert capsys.readouterr().out.splitlines() == ["docs/guide.md:3: 'robin'", "leak guard: 1 found"]
    (checkout / "docs/guide.md").write_text("Ask first.\n")
    assert guard.main([]) == 0


def test_an_image_is_refused_until_someone_has_looked_at_its_exact_bytes(household, tmp_path, monkeypatch, capsys):
    """An image has no words to match: a person opens it, checks it by eye and records its bytes (looked-at, which
    needs no private folder); new bytes need a new look."""
    checkout = tmp_path / "checkout"
    (checkout / "assets").mkdir(parents=True)
    (checkout / "tools").mkdir()
    image = checkout / "assets/panel.png"
    image.write_bytes(PNG)
    monkeypatch.setattr(guard, "ROOT", checkout)
    monkeypatch.setattr(guard, "git", Git([], files=["assets/panel.png"]))
    assert guard.main(["tree"]) == 1
    assert capsys.readouterr().out.splitlines() == ["assets/panel.png: an image nobody has looked at", LOOK,
                                                    "leak guard: 1 found"]
    monkeypatch.setenv("ASSIST_IMPROVER_PRIVATE", str(tmp_path / "missing"))
    with pytest.raises(SystemExit) as stopped:
        guard.main(["looked-at"])
    assert str(stopped.value) == guard.__doc__  # No image named: the usage, not the missing private folder.
    assert guard.main(["looked-at", str(image)]) == guard.main(["looked-at", str(image)]) == 0
    assert capsys.readouterr().out.splitlines() == [f"Recorded {n} image(s) as looked at in tools/looked-at.txt."
                                                    for n in (1, 0)]
    assert (checkout / "tools/looked-at.txt").read_text().split()[:2] == [hashlib.sha256(PNG).hexdigest(),
                                                                          "assets/panel.png"]
    monkeypatch.setattr(guard, "private_terms", lambda: household)
    assert guard.main(["tree"]) == 0
    image.write_bytes(PNG + b"changed")
    assert guard.main(["tree"]) == 1


def test_the_pre_push_hook_checks_only_pushes_to_the_public_repository():
    """The hook gets the remote, its URL, and a line a ref on stdin: what's checked is each commit pushed that the
    remote doesn't have. The public repository is tools/passed.py's REPO."""
    sha, none = "1" * 40, guard.NONE
    refs = [f"refs/heads/main {sha} refs/heads/main {none}", f"(delete) {none} refs/heads/old {sha}"]
    public = "git@github.com:someone/assist-improver.git"
    assert guard.pushed("public", public, refs, "someone/assist-improver") == [sha, "--not", "--remotes=public"]
    assert guard.pushed("origin", "git@github.com:someone/private.git", refs, "someone/assist-improver") is None
    assert guard.pushed("public", public, refs[1:], "someone/assist-improver") == []  # A delete alone: nothing.


def test_an_annotated_tag_pushed_has_its_message_checked_too(household, monkeypatch, capsys):
    """git log reads a tag as its commit, so the hook reads an annotated tag's own message as well."""
    tag, public = "2" * 40, "git@github.com:someone/assist-improver.git"
    monkeypatch.setattr(guard, "git", Git([("c1", "Generic words.", {"README.md": b"Generic words."})],
                                          tags={tag: "object c1\ntype commit\ntag v1\ntagger Someone\n\nFor Robin.\n"}))
    tool = guard.tool
    monkeypatch.setattr(guard, "tool", lambda name: SimpleNamespace(REPO="someone/assist-improver")
                        if name == "passed" else tool(name))
    monkeypatch.setattr("sys.stdin", io.StringIO(f"refs/tags/v1 {tag} refs/tags/v1 {guard.NONE}\n"))
    assert guard.main(["push", "public", public]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "tag v1's message: 'robin'", "leak guard: 1 found"]


def test_without_the_private_folder_it_stops_with_one_line(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSIST_IMPROVER_PRIVATE", str(tmp_path / "missing"))
    with pytest.raises(SystemExit) as stopped:
        guard.main(["tree"])
    assert str(stopped.value).startswith(f"No private folder at {tmp_path / 'missing'}")
    assert "\n" not in str(stopped.value)


def test_the_hook_and_ci_run_the_guard_as_tested_here():
    """The pre-push hook hands its remote, URL and refs to `push`; CI pipes the secret's hashed list, empty on a fork's
    pull request, to `--hashed -`, over the commits a push or pull request adds: history already public isn't checked
    again."""
    import yaml
    hook = (ROOT / "tools/git-hooks/leak-guard").read_text()
    assert 'tools/leak_guard.py" push "$@"' in hook
    job = yaml.safe_load((ROOT / ".github/workflows/leak-guard.yml").read_text())["jobs"]["leak-guard"]
    assert job["if"] == "${{ !github.event.repository.private && !github.event.deleted }}"
    steps = job["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert steps[1]["env"] == {"TERMS": "${{ secrets.HOUSEHOLD_TERMS }}", "BASE": "${{ github.base_ref }}",
                               "BEFORE": "${{ github.event.before }}",
                               "DEFAULT": "${{ github.event.repository.default_branch }}"}
    run = steps[1]["run"].splitlines()
    assert [line.split("  #")[0] for line in run] == [
        'if [ -n "$BASE" ]; then had="origin/$BASE"',
        f'elif [ "$BEFORE" = {guard.NONE} ]; then had="origin/$DEFAULT"',
        'else had="$BEFORE"',
        '  git cat-file -e "$had^{commit}" 2>/dev/null || git fetch -q origin "$had"; fi',
        """printf '%s\\n' "$TERMS" | python3 tools/leak_guard.py --hashed - commits HEAD --not "$had\""""]
