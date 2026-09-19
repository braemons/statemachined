# SPDX-License-Identifier: GPL-3.0-or-later
"""The UI is served, and it is an honest test of the API.

There is no build step and no framework here, so there is no compiler to catch
an element importing a module that does not exist or calling a route that does
not. These tests are that compiler, and they are cheap because the UI is files.

Four of them do work a build step would otherwise do:

  * **every module it imports exists**, and **every module parses** -- there is
    no bundler to notice a renamed file and no compiler to catch a stray brace;
  * **every `/api/` path the UI mentions is a route this daemon serves** -- the
    claim in docs/developer/daemon.md §5 that the web UI uses only this API, checked rather
    than asserted, which is what keeps the UI an honest test of it;
  * **the outcome names in the editor are the ones the store accepts** -- a menu
    offering a spelling the store refuses is a paradigm author's afternoon.

What none of them do is render a page. Nothing here has a DOM, so the panels'
drawing is checked by reading them; the diagram's layout is pure and is run for
real, in node, below.
"""

from __future__ import annotations

import json
import re
from json import dumps as json_dumps
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from statemachined.daemon.api.application import create_application
from statemachined.daemon.api.web_user_interface_routes import read_asset, web_directory
from statemachined.daemon.rig_configuration import RigConfiguration
from statemachined.model.graph_definition import TransitionPredicate
from statemachined.model.trial_outcome import DECLARABLE_TERMINAL_OUTCOMES

ELEMENT_TAG_NAMES = [
    "statemachined-device",
    "statemachined-lines",
    "statemachined-graph",
    "statemachined-configs",
    "statemachined-session",
    "statemachined-recording",
    "statemachined-trace",
    "statemachined-observers",
    "statemachined-firmware",
    "statemachined-monitor",
]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """The app, with nothing pointing at a real rig's directories.

    `connect_on_startup` off because there is no board and a test that waited
    for a serial timeout would be slow for no reading.
    """
    configuration = RigConfiguration(
        device_target="loop://",
        connect_on_startup=False,
        graph_store_directory=tmp_path / "graphs",
        state_machine_config_directory=tmp_path / "configs",
        trace_directory=tmp_path / "trace",
    )
    with TestClient(create_application(configuration)) as client:
        yield client


def element_sources() -> dict[str, str]:
    return {
        path.name: path.read_text() for path in sorted((web_directory() / "elements").glob("*.js"))
    }


# ------------------------------------------------------------------ serving ---


def test_the_index_is_served_and_loads_the_shell(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "/ui/application_shell.js" in response.text


def test_the_elements_entry_point_is_served_as_javascript(client: TestClient) -> None:
    """The one URL the console repo depends on. docs/developer/daemon.md §5."""
    response = client.get("/elements/statemachined.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    for tag_name in ELEMENT_TAG_NAMES:
        assert tag_name in response.text


def test_every_panel_registers_its_tag_name(client: TestClient) -> None:
    registered = {
        match
        for source in element_sources().values()
        for match in re.findall(r'defineElementOnce\("([a-z-]+)"', source)
    }
    assert registered == set(ELEMENT_TAG_NAMES)


def test_nothing_is_cached(client: TestClient) -> None:
    """One daemon serves the elements and the API they call, which is what keeps
    them the same version. A cached element would give that away."""
    for path in ("/", "/elements/statemachined.js", "/ui/statemachined_user_interface.css"):
        assert "no-cache" in client.get(path).headers["cache-control"]


def test_a_file_outside_the_ui_is_refused() -> None:
    """This is the only route that turns a URL into a filesystem path, and the
    daemon runs where a config file and a graph store are."""
    with pytest.raises(Exception) as refusal:
        read_asset("../daemon_configuration.py", web_directory())
    assert refusal.value.status_code == 404


def test_a_file_type_this_daemon_does_not_serve_is_refused(tmp_path: Path) -> None:
    (tmp_path / "secrets.toml").write_text("token = 'x'")
    with pytest.raises(Exception) as refusal:
        read_asset("secrets.toml", tmp_path)
    assert refusal.value.status_code == 404


def test_a_missing_asset_is_a_404_that_says_which(client: TestClient) -> None:
    response = client.get("/elements/no_such_panel.js")
    assert response.status_code == 404
    assert "no_such_panel.js" in response.text


# ------------------------------------------------ the UI against the API ---


def test_the_server_can_actually_speak_websocket() -> None:
    """Two of this UI's panels are streams, and every test of them lies.

    Starlette's TestClient implements WebSockets in process, so `WS /api/stream`
    and the trace tail pass their tests with no WebSocket library installed at
    all -- while the shipped daemon answers an upgrade with **404**, not an
    error, and the Session and Trace panels reconnect forever showing nothing.
    That is exactly what was happening until somebody connected to a running
    one.

    So the dependency is pinned by a test rather than by a comment in
    pyproject.toml, since nothing else in this suite can notice it missing.
    """
    import importlib.util

    installed = any(
        importlib.util.find_spec(implementation) is not None
        for implementation in ("websockets", "wsproto")
    )
    assert installed, (
        "uvicorn has no WebSocket implementation, so the daemon will answer every "
        "WebSocket upgrade with 404. Install uvicorn[standard]."
    )


def test_every_view_says_what_it_is() -> None:
    """"Session" and "Trace" are words this system uses in a particular way, and
    a tab label cannot teach anybody either of them. So the shell carries a
    sentence per view -- shown above the panels and as the tab's tooltip -- and
    each panel carries its own, because the panels are used inside a console
    where this shell does not exist.
    """
    shell = (web_directory() / "application_shell.js").read_text()
    described = re.findall(r'id: "([a-z]+)",\n\s+label: "[^"]+",\n\s+tags: \[', shell)
    assert set(described) == {"device", "setup", "run"}


def test_every_panel_is_reachable_from_some_view() -> None:
    """The consolidation's one real risk, asserted.

    A view holds several panels now, which is what stopped "Configs", "Lines"
    and "Paradigms" from being three tabs cutting the same material. The way
    that goes wrong is silent: a panel dropped out of every `tags` list is still
    served, still registered, still tested -- and unreachable from the rig's own
    page, which nobody notices until they go looking for it.
    """
    shell = (web_directory() / "application_shell.js").read_text()
    reachable = set(re.findall(r'"(statemachined-[a-z]+)"', shell))
    assert reachable == set(ELEMENT_TAG_NAMES)

    # And the two words that prompted this are explained in their own panels,
    # not only in the shell.
    session = (web_directory() / "elements" / "session_panel_element.js").read_text()
    assert "A session is one run of an experiment" in session
    trace = (web_directory() / "elements" / "trace_panel_element.js").read_text()
    assert "The trace is this daemon's own record" in trace


def test_every_module_the_ui_imports_exists() -> None:
    """No build step means no bundler to notice a renamed file."""
    available = set(element_sources())
    for name, source in element_sources().items():
        for imported in re.findall(r'from "\./([a-z_]+\.js)"', source):
            assert imported in available, f"{name} imports {imported}, which is not there"


def test_the_shell_imports_only_files_that_are_served(client: TestClient) -> None:
    shell = (web_directory() / "application_shell.js").read_text()
    for imported in re.findall(r'(?:from|import) "(/[^"]+)"', shell):
        assert client.get(imported).status_code == 200, f"the shell imports {imported}"


def normalised(path: str) -> str:
    """A path with its parameters blanked, so a UI template and a route match.

    `/api/graphs/${encodeURIComponent(name)}/upload` and
    `/api/graphs/{graph_name}/upload` are the same route said two ways.
    """
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    return re.sub(r"\{[^}]*\}", "{}", path)


def served_paths(app) -> set[str]:
    """Every path the app routes to, websockets included.

    Not `app.openapi()`: the two streams are WebSockets and are not in a schema,
    and they are exactly the routes a UI is most likely to misspell.
    """
    paths: set[str] = set()
    pending = list(app.routes)
    while pending:
        route = pending.pop()
        if hasattr(route, "path"):
            paths.add(normalised(route.path))
        # FastAPI wraps included routers, so the routes are one level down.
        pending.extend(getattr(getattr(route, "original_router", None), "routes", []))
        pending.extend(getattr(route, "routes", []) if not hasattr(route, "path") else [])
    return paths


def test_every_api_path_the_ui_calls_is_a_route_this_daemon_serves(client: TestClient) -> None:
    served = served_paths(client.app)
    # The walker itself is the thing most likely to be silently wrong here, so
    # it is pinned: one plain route and one WebSocket.
    assert {"/api/device", "/api/stream"} <= served
    mentioned = set()
    for source in element_sources().values():
        mentioned.update(re.findall(r'["`](/api/[^"`\s]*)["`]', source))
    assert mentioned, "no API paths found; this test would pass vacuously"
    for path in mentioned:
        assert normalised(path) in served, f"the UI calls {path}, which is not a route"


def test_every_module_parses() -> None:
    """The nearest thing to a compiler this UI has.

    There is no build step, so a stray brace ships. `node --check` parses a
    module without running it, costs milliseconds, and is skipped where node is
    not installed -- a rig does not need it, and neither does a developer who
    has not touched the JavaScript.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the UI's syntax is unchecked here")

    sources = [web_directory() / "application_shell.js", *sorted((web_directory() / "elements").glob("*.js"))]
    for source in sources:
        finished = subprocess.run(
            [node, "--check", str(source)], capture_output=True, text=True, timeout=30
        )
        assert finished.returncode == 0, f"{source.name} does not parse:\n{finished.stderr}"


UNIT_TEST_DIRECTORY = Path(__file__).resolve().parent
MINIMAL_DOM = (UNIT_TEST_DIRECTORY / "minimal_dom_for_panel_elements.mjs").as_uri()


def run_in_node(script: str) -> str:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    finished = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert finished.returncode == 0, finished.stderr
    return finished.stdout


def test_the_diagram_puts_an_unreachable_state_where_it_can_be_seen() -> None:
    """The drawing exists for the check no list of states can do: seeing that an
    outcome cannot be reached, before an animal is in the booth. So the layout
    is exercised for real -- it is pure, and needs no DOM.

    A state the entry cannot reach is *drawn*, in its own column, rather than
    dropped: a state you cannot see is a state you will not fix.
    """
    module = (web_directory() / "elements" / "graph_node_diagram.js").as_uri()
    graph = {
        "name": "orphan",
        "entry": "Start",
        "states": [
            {"name": "Start", "timeout": {"after": "d", "goto": "Middle"}, "transitions": []},
            {"name": "Middle", "transitions": [{"when": {"all": ["lever"]}, "goto": "Hit"}]},
            {"name": "Hit", "outcome": "HIT", "transitions": []},
            {"name": "Stranded", "outcome": "LATE", "transitions": []},
        ],
    }
    printed = run_in_node(
        f"import {{ layoutColumns, edgesOf }} from {module!r};\n"
        f"const graph = {json_dumps(graph)};\n"
        "const laid = layoutColumns(graph);\n"
        "console.log(JSON.stringify({\n"
        "  depths: Object.fromEntries(laid.depthByName),\n"
        "  unreachable: [...laid.unreachableNames],\n"
        "  edges: edgesOf(graph.states[1]).map((edge) => edge.label),\n"
        "}));\n"
    )
    result = json.loads(printed)
    assert result["depths"] == {"Start": 0, "Middle": 1, "Hit": 2, "Stranded": 3}
    assert result["unreachable"] == ["Stranded"]
    # The predicate is drawn as the named lines, never as the wire's masks.
    assert result["edges"] == ["all lever"]


def test_the_observers_panel_says_which_stream_and_for_how_long() -> None:
    """The two pure helpers behind the panel a person reads at two in the
    morning, when trials have stopped reaching triald.

    The distinction the wording has to carry: the trace stream loses nothing and
    says so if it ever does, while the state stream coalesces on purpose. An
    observer on the wrong one would see a rig that looks alive and no trials.
    """
    module = (web_directory() / "elements" / "observers_panel_element.js").as_uri()
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ describeStream, describeDuration }} = await import({module!r});\n"
        "console.log(JSON.stringify({\n"
        "  trace: describeStream('trace'),\n"
        "  state: describeStream('state'),\n"
        "  unknown: describeStream(undefined),\n"
        "  durations: [0, 41, 90, 3700, -1, undefined].map(describeDuration),\n"
        "}));\n"
    )
    result = json.loads(printed)
    assert "none skipped" in result["trace"]
    assert "coalesced" in result["state"]
    assert result["unknown"] == "(unknown)"
    # A negative or missing duration is a clock artefact, not something to show
    # somebody as "-1s".
    assert result["durations"] == ["0s", "41s", "1m 30s", "1h 1m", "0s", "0s"]


# ------------------------------------------------- the panels, in a fake DOM ---
#
# Enough of a DOM to answer one question no reading of the source answers
# reliably: after a poll, is the field still the same element? See
# minimal_dom_for_panel_elements.mjs.


def test_a_poll_does_not_take_the_focus_out_of_a_field_somebody_is_editing() -> None:
    """The Lines panel reads the device twice a second, and a person setting up
    a rig is typing into it at the same time.

    Rebuilding the table on each poll removes the focused `<input>` from the
    document, which takes the cursor, the selection and the keystrokes with it
    -- the field goes dead about a second after it is clicked. So the poll
    paints the live levels and nothing else, and the fields are rebuilt only
    when the draft is replaced.

    The assertion is *element identity*, because that is the property the
    browser's focus depends on: an input that is a new object is a different
    input however identical it looks.
    """
    module = (web_directory() / "elements" / "line_map_panel_element.js").as_uri()
    line_map = {
        "input_lines": [
            {"name": "lever", "line_index": 4, "pin_label": "D6", "is_high_now": False}
        ],
        "output_lines": [
            {"name": "valve", "line_index": 3, "safe_level_is_high": True, "is_high_now": False}
        ],
    }
    pressed = json.loads(json_dumps(line_map))
    pressed["input_lines"][0]["is_high_now"] = True

    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ LineMapPanelElement }} = await import({module!r});\n"
        f"const map = {json_dumps(line_map)};\n"
        f"const pressed = {json_dumps(pressed)};\n"
        "const panel = new LineMapPanelElement();\n"
        "panel.renderShell();\n"
        "panel.adoptDraft(map);\n"
        "panel.paint(map);\n"
        "const nameField = () => panel.root.find((n) => n.tagName === 'input' && n.type === 'text');\n"
        "const dot = () => panel.root.find((n) => n.className.startsWith('level'));\n"
        "const before = nameField();\n"
        "before.value = 'left_lever';\n"
        "before.dispatch('input');\n"
        "const dotBefore = dot().className;\n"
        "panel.paint(pressed);\n"
        "const survived = nameField() === before;\n"
        "const levelMoved = dot().className !== dotBefore;\n"
        "const draftKept = panel.draft.input_lines[0].name;\n"
        "panel.discardTheDraft();\n"
        "panel.adoptDraft(map);\n"
        "panel.paint(map);\n"
        "console.log(JSON.stringify({ survived, levelMoved, draftKept,\n"
        "  rebuiltAfterRevert: nameField() !== before, revertedValue: nameField().value }));\n"
    )
    result = json.loads(printed)
    assert result["survived"], "the poll rebuilt the field, so the cursor was thrown out of it"
    # And the panel still does its job: the dot moved, which is the whole point
    # of watching this page while somebody presses a lever.
    assert result["levelMoved"]
    assert result["draftKept"] == "left_lever", "the edit was recorded as it was typed"
    # A revert *must* rebuild -- the fields are showing edits the draft no
    # longer has, and a shape comparison would answer "nothing changed".
    assert result["rebuiltAfterRevert"]
    assert result["revertedValue"] == "lever"


def test_the_pin_is_chosen_from_the_boards_own_pins_and_not_typed() -> None:
    """The board owns the labels (docs/reference/protocol.md §3.6): which pin line 4 is was
    decided when the firmware was compiled, and typing a different string cannot
    move a wire. So where the board answered, the pin column offers *its* pins.

    A text box there could only be wrong -- the daemon would refuse the save,
    which is the right answer to a question the UI should not have asked.

    Choosing a pin sets the line number with it, because they are one fact said
    twice, and a label sent without its number is the disagreement the daemon
    refuses over.
    """
    module = (web_directory() / "elements" / "line_map_panel_element.js").as_uri()
    lines = {
        "input_lines": [
            {"name": "lever", "line_index": 4, "pin_label": "D6", "is_high_now": False}
        ],
        "output_lines": [],
        "board_input_pins": ["D2", "D3", "D4", "D5", "D6", "D7"],
        "board_output_pins": ["D10", "A0"],
        "pin_labels_came_from": "device",
    }
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ LineMapPanelElement }} = await import({module!r});\n"
        f"const lines = {json_dumps(lines)};\n"
        "const panel = new LineMapPanelElement();\n"
        "panel.renderShell();\n"
        "panel.adoptDraft(lines);\n"
        "panel.paint(lines);\n"
        "const chooser = panel.root.find((n) => n.tagName === 'select');\n"
        "const offered = chooser.children.map((option) => option.value);\n"
        "const before = chooser.children.find((o) => o.selected).value;\n"
        "// Somebody moves the lever to D7 and says so.\n"
        "chooser.value = 'D7';\n"
        "chooser.dispatch('change');\n"
        "const numbers = panel.root.descendants()\n"
        "  .filter((n) => n.tagName === 'td' && n.className === 'mono').map((n) => n.textContent);\n"
        "console.log(JSON.stringify({ offered, before, draft: panel.draft.input_lines[0],\n"
        "  numbers, unsaved: panel.hasUnsavedEdits }));\n"
    )
    result = json.loads(printed)
    assert result["offered"] == ["D2", "D3", "D4", "D5", "D6", "D7"]
    assert result["before"] == "D6", "the pin the config named is the one shown as chosen"
    # Both halves moved, and neither on its own.
    assert result["draft"]["pin_label"] == "D7"
    assert result["draft"]["line_index"] == 5
    # And the line number on screen followed, in place -- rebuilding the row
    # would have taken the focus out of whatever else was being edited.
    assert result["numbers"] == ["5"]
    assert result["unsaved"] is True


def test_two_lines_cannot_be_saved_as_the_same_pin() -> None:
    """The daemon refuses it -- "two names for one line is not a harmless alias:
    a graph naming both would raise one line and believe it had raised two, and
    the mistake is invisible in the record" -- so the panel must not offer to
    send it.

    Marked rather than forbidden. Preventing the choice would make swapping two
    pins impossible: moving the lever from D6 to D7 while the pedal is on D7 has
    to pass through a state where both are on D7. So a taken pin is offered with
    what has it, the conflict is named, and the save button is held until it is
    resolved -- loud and reversible, rather than a dead end.
    """
    module = (web_directory() / "elements" / "line_map_panel_element.js").as_uri()
    lines = {
        "input_lines": [
            {"name": "lever", "line_index": 4, "pin_label": "D6", "is_high_now": False},
            {"name": "pedal", "line_index": 5, "pin_label": "D7", "is_high_now": False},
        ],
        "output_lines": [],
        "board_input_pins": ["D2", "D3", "D4", "D5", "D6", "D7"],
        "board_output_pins": ["D10"],
        "pin_labels_came_from": "device",
    }
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ LineMapPanelElement }} = await import({module!r});\n"
        f"const lines = {json_dumps(lines)};\n"
        "const panel = new LineMapPanelElement();\n"
        "panel.renderShell();\n"
        "panel.adoptDraft(lines);\n"
        "panel.paint(lines);\n"
        "const choosers = panel.root.descendants().filter((n) => n.tagName === 'select');\n"
        "const failures = () => panel.root.descendants()\n"
        "  .filter((n) => n.className === 'failure').map((n) => n.textContent);\n"
        "const pedalsOptions = () => choosers[1].children.map((o) => o.textContent);\n"
        "const marked = pedalsOptions();\n"
        "choosers[1].value = 'D6';\n"
        "choosers[1].dispatch('change');\n"
        "const conflicted = { failures: failures(), saveBlocked: panel.saveButton.disabled };\n"
        "choosers[1].value = 'D7';\n"
        "choosers[1].dispatch('change');\n"
        "console.log(JSON.stringify({ marked, conflicted,\n"
        "  resolvedFailures: failures(), saveAllowed: !panel.saveButton.disabled }));\n"
    )
    result = json.loads(printed)
    # A pin already spoken for says so *before* it is chosen, not after a save
    # is refused: the option carries the name of whatever has it.
    assert "D6 — lever" in result["marked"]
    assert "D7" in result["marked"], "a free pin is offered plainly"

    assert len(result["conflicted"]["failures"]) == 1
    assert "both input line 4" in result["conflicted"]["failures"][0]
    assert result["conflicted"]["saveBlocked"] is True

    # And putting it back clears both, because a dead end is not a fix.
    assert result["resolvedFailures"] == []
    assert result["saveAllowed"] is True


def test_two_lines_cannot_be_saved_under_one_name() -> None:
    """The other duplicate the daemon refuses, shown in the same place: both
    come back as one refusal at save, so a person should see them together."""
    module = (web_directory() / "elements" / "line_map_panel_element.js").as_uri()
    lines = {
        "input_lines": [
            {"name": "lever", "line_index": 4, "pin_label": "D6"},
            {"name": "pedal", "line_index": 5, "pin_label": "D7"},
        ],
        "output_lines": [],
        "board_input_pins": ["D2", "D3", "D4", "D5", "D6", "D7"],
        "board_output_pins": [],
        "pin_labels_came_from": "device",
    }
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ LineMapPanelElement }} = await import({module!r});\n"
        f"const lines = {json_dumps(lines)};\n"
        "const panel = new LineMapPanelElement();\n"
        "panel.renderShell();\n"
        "panel.adoptDraft(lines);\n"
        "panel.paint(lines);\n"
        "const names = panel.root.descendants().filter((n) => n.type === 'text');\n"
        "names[1].value = 'lever';\n"
        "names[1].dispatch('input');\n"
        "console.log(JSON.stringify({\n"
        "  failures: panel.root.descendants().filter((n) => n.className === 'failure')\n"
        "    .map((n) => n.textContent),\n"
        "  saveBlocked: panel.saveButton.disabled,\n"
        "}));\n"
    )
    result = json.loads(printed)
    assert result["failures"] == ['two input lines are called "lever".']
    assert result["saveBlocked"] is True


def test_a_board_that_cannot_say_leaves_the_pin_a_text_box() -> None:
    """Firmware older than `pins` gives the daemon nothing to offer, and a
    chooser over an empty list would be a worse lie than a text box."""
    module = (web_directory() / "elements" / "line_map_panel_element.js").as_uri()
    lines = {
        "input_lines": [{"name": "lever", "line_index": 4, "pin_label": "D6"}],
        "output_lines": [],
        "board_input_pins": [],
        "board_output_pins": [],
        "pin_labels_came_from": "unknown",
    }
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ LineMapPanelElement }} = await import({module!r});\n"
        f"const lines = {json_dumps(lines)};\n"
        "const panel = new LineMapPanelElement();\n"
        "panel.renderShell();\n"
        "panel.adoptDraft(lines);\n"
        "panel.paint(lines);\n"
        "console.log(JSON.stringify({\n"
        "  selects: panel.root.descendants().filter((n) => n.tagName === 'select').length,\n"
        "  texts: panel.root.descendants().filter((n) => n.type === 'text').length,\n"
        "  saidSo: panel.root.descendants().some((n) => n.textContent.includes('pins unknown')),\n"
        "}));\n"
    )
    result = json.loads(printed)
    assert result["selects"] == 0
    # A name and a pin, both typed, because there is nothing to choose from.
    assert result["texts"] == 2
    assert result["saidSo"], "a panel showing unchecked labels has to say that they are unchecked"


def test_the_line_map_draft_leaves_the_live_level_behind() -> None:
    """`is_high_now` is a reading, not configuration. The daemon's LineMap
    forbids fields it does not declare, so a draft still carrying it would be
    refused on save -- and rightly: a rig cannot be told to be high."""
    module = (web_directory() / "elements" / "line_map_panel_element.js").as_uri()
    printed = run_in_node(
        "globalThis.HTMLElement = class {};\n"
        "globalThis.customElements = { get: () => undefined, define: () => {} };\n"
        f"const {{ draftOf }} = await import({module!r});\n"
        "console.log(JSON.stringify(draftOf({\n"
        "  input_lines: [{ name: 'lever', line_index: 4, is_high_now: true }],\n"
        "  output_lines: [{ name: 'valve', line_index: 3, is_high_now: false }],\n"
        "})));\n"
    )
    draft = json.loads(printed)
    assert draft["input_lines"] == [{"name": "lever", "line_index": 4}]
    assert draft["output_lines"] == [{"name": "valve", "line_index": 3}]


def test_the_focus_path_survives_a_rebuild_of_the_same_shape() -> None:
    """What the editors use where a repaint genuinely has to happen: a renamed
    state has to appear in every transition naming it, so the subtree is rebuilt
    and the cursor has to be put back by position."""
    module = (web_directory() / "elements" / "base_panel_element.js").as_uri()
    printed = run_in_node(
        "globalThis.HTMLElement = class {};\n"
        "globalThis.customElements = { get: () => undefined, define: () => {} };\n"
        f"const {{ BasePanelElement }} = await import({module!r});\n"
        "function element(children = []) {\n"
        "  const node = { children, parentNode: null };\n"
        "  for (const child of children) child.parentNode = node;\n"
        "  return node;\n"
        "}\n"
        "const field = element();\n"
        "const root = element([element(), element([element([element(), field])])]);\n"
        "const panel = { root };\n"
        "const path = BasePanelElement.prototype.pathToDescendant.call(panel, field);\n"
        "console.log(JSON.stringify({\n"
        "  path,\n"
        "  roundTrips: BasePanelElement.prototype.descendantAtPath.call(panel, path) === field,\n"
        "  detached: BasePanelElement.prototype.pathToDescendant.call(panel, element()),\n"
        "  missing: BasePanelElement.prototype.descendantAtPath.call(panel, [1, 0, 9]),\n"
        "}));\n"
    )
    result = json.loads(printed)
    assert result["roundTrips"]
    assert result["path"] == [1, 0, 1]
    # A node that is not under this panel, and a path that no longer leads
    # anywhere: both mean "there is nothing to focus", not a crash mid-repaint.
    assert result["detached"] is None
    assert result["missing"] is None


def test_a_predicate_is_said_in_words_and_its_traps_are_named() -> None:
    """Three independent masks, ANDed, shown as three columns of checkboxes --
    so a line can be ticked in two of them, and two of the three ways of doing
    that mean something nobody intends.

    The editor cannot make that visible with checkboxes alone, which is why the
    predicate is also written out in a sentence: "L, and either M or N" entered
    as all:[L] any:[L,M,N] reads back as "L is high, and at least one of L, M is
    high", and the second clause is doing nothing.
    """
    module = (web_directory() / "elements" / "transition_predicate.js").as_uri()
    printed = run_in_node(
        f"const {{ describePredicate, predicateProblems }} = await import({module!r});\n"
        "const said = describePredicate({ all: ['lever'], any: ['left', 'right'], none: ['abort'] });\n"
        "const mootAny = predicateProblems({ all: ['lever'], any: ['lever', 'pedal'] });\n"
        "const contradiction = predicateProblems({ all: ['lever'], none: ['lever'] });\n"
        "const wastedAny = predicateProblems({ any: ['lever'], none: ['lever'] });\n"
        "const clean = predicateProblems({ all: ['lever'], none: ['abort'] });\n"
        "console.log(JSON.stringify({ said, mootAny, contradiction, wastedAny, clean,\n"
        "  empty: describePredicate({}) }));\n"
    )
    result = json.loads(printed)
    assert result["said"] == (
        "fires when lever is high, and at least one of left, right is high, and abort is low"
    )

    # In `all` and `any`: legal, and the `any` column stops meaning anything.
    assert [problem["severity"] for problem in result["mootAny"]] == ["warning"]
    assert '"pedal" is ignored' in result["mootAny"][0]["detail"]

    # In `all` and `none`: provably dead, and the daemon refuses the graph.
    assert [problem["severity"] for problem in result["contradiction"]] == ["error"]

    # In `any` and `none`, with nothing else in `any`: also dead.
    assert [problem["severity"] for problem in result["wastedAny"]] == ["error"]

    # An ordinary predicate has nothing to say about it.
    assert result["clean"] == []
    assert result["empty"] is None


def test_the_editor_and_the_daemon_agree_on_which_predicates_are_refused() -> None:
    """The panel tells a person "the daemon refuses a graph with this in it".
    Two implementations of one rule, in two languages, so the agreement is
    checked rather than assumed -- an editor that warned about the wrong thing
    would send somebody looking for a fault in the graph they just fixed.
    """
    module = (web_directory() / "elements" / "transition_predicate.js").as_uri()
    predicates = [
        {"all": ["a"], "none": ["a"]},          # dead: high and low at once
        {"any": ["a"], "none": ["a"]},          # dead: nothing can satisfy `any`
        {"all": ["a"], "any": ["a", "b"]},      # legal, and the `any` does nothing
        {"all": ["a"], "none": ["b"]},          # ordinary
        {"any": ["a", "b"], "none": ["a"]},     # legal: `b` can still satisfy `any`
    ]
    printed = run_in_node(
        f"const {{ predicateProblems }} = await import({module!r});\n"
        f"const predicates = {json_dumps(predicates)};\n"
        "console.log(JSON.stringify(predicates.map((predicate) =>\n"
        "  predicateProblems(predicate).some((problem) => problem.severity === 'error'))));\n"
    )
    the_editor_calls_it_dead = json.loads(printed)

    the_daemon_refuses = []
    for predicate in predicates:
        try:
            TransitionPredicate.model_validate(predicate)
            the_daemon_refuses.append(False)
        except Exception:
            the_daemon_refuses.append(True)

    # Both dead cases, both ends, and the three legal ones left alone -- an
    # `any` column with one forbidden line and one good one can still fire.
    assert the_editor_calls_it_dead == [True, True, False, False, False]
    assert the_daemon_refuses == the_editor_calls_it_dead


def test_the_editors_outcome_names_are_the_ones_the_store_accepts() -> None:
    source = (web_directory() / "elements" / "graph_store_panel_element.js").read_text()
    block = re.search(r"const OUTCOME_NAMES = \[(.*?)\];", source, re.S).group(1)
    offered = {name for name in re.findall(r'"([A-Z_]*)"', block) if name}
    assert offered == set(DECLARABLE_TERMINAL_OUTCOMES)


def test_the_graph_editor_notices_a_line_map_that_arrived_after_it_opened() -> None:
    """A rig being set up names its lines *after* the page is open.

    The order on a new rig is: open the page, name the lines, push the wiring,
    then write a graph against it -- and the graph editor is on the same view as
    the line map. Reading the names once when the panel opened meant every line
    chooser stayed empty until somebody reloaded the browser, with nothing on
    screen saying why. So the names are polled, and until there are any, the
    "+ action" button says what is missing instead of adding a row whose line is
    the empty string -- which the store refuses.
    """
    module = (web_directory() / "elements" / "graph_store_panel_element.js").as_uri()
    lines = {
        "input_lines": [{"name": "lever", "line_index": 0}],
        "output_lines": [{"name": "reward_valve", "line_index": 0}],
    }
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ GraphStorePanelElement }} = await import({module!r});\n"
        f"const lines = {json_dumps(lines)};\n"
        "const panel = new GraphStorePanelElement();\n"
        "panel.renderShell();\n"
        "panel.graph = { name: 'g', entry: 'Start', states: [{ name: 'Start', on_entry: [] }] };\n"
        "// The board answers with nothing until the wiring is pushed next door.\n"
        "let wired = false;\n"
        "Object.defineProperty(panel, 'api', {\n"
        "  value: { readDeviceLines: async () => (wired ? lines : { input_lines: [], output_lines: [] }) },\n"
        "});\n"
        "// The diagram is not what this is about, and it wants an SVG DOM.\n"
        "let repaints = 0;\n"
        "panel.paint = () => { repaints += 1; };\n"
        "const addButton = () => panel.actionEditor(panel.graph.states[0], 'on_entry')\n"
        "  .find((node) => node.textContent === '+ action');\n"
        "const warning = () => panel.actionEditor(panel.graph.states[0], 'on_entry')\n"
        "  .find((node) => node.className === 'warn');\n"
        "await panel.readTheLineNames();\n"
        "const before = { disabled: addButton().disabled, warned: warning() !== null, repaints };\n"
        "wired = true;\n"
        "await panel.readTheLineNames({ repaint: true });\n"
        "const chooser = () => panel.actionEditor(panel.graph.states[0], 'on_entry')\n"
        "  .find((node) => node.tagName === 'select');\n"
        "const after = { disabled: addButton().disabled, warned: warning() !== null, repaints };\n"
        "addButton().dispatch('click');\n"
        "const added = panel.graph.states[0].on_entry;\n"
        "// A poll that found the same names must not rebuild the form underneath.\n"
        "const repaintsBeforeAnUnchangedPoll = repaints;\n"
        "await panel.readTheLineNames({ repaint: true });\n"
        "console.log(JSON.stringify({ before, after, added,\n"
        "  offered: chooser().children.map((option) => option.value),\n"
        "  repaintedByAnUnchangedPoll: repaints !== repaintsBeforeAnUnchangedPoll }));\n"
    )
    result = json.loads(printed)
    assert result["before"] == {"disabled": True, "warned": True, "repaints": 0}
    assert result["after"] == {"disabled": False, "warned": False, "repaints": 1}
    # And the row it then adds names a line this rig has, rather than "".
    assert result["added"] == [{"line": "reward_valve", "kind": "high", "pulse_ms": None}]
    assert result["offered"] == ["reward_valve"]
    assert not result["repaintedByAnUnchangedPoll"], (
        "a poll that changed nothing rebuilt the editor under somebody's cursor"
    )


#: A small board: three inputs, two outputs, one of each already named. Small so
#: that "every pin has a name" is reachable in a test, and consistent -- D6 is
#: input line 0 because it is first in the board's own list, which is the only
#: thing a line number ever means.
LINES_WITH_TWO_FREE_INPUT_PINS = {
    "input_lines": [
        {"name": "lever", "line_index": 0, "pin_label": "D6", "is_high_now": False},
    ],
    "output_lines": [
        {"name": "reward_valve", "line_index": 1, "pin_label": "A0", "is_high_now": False},
    ],
    "board_input_pins": ["D6", "D7", "D8"],
    "board_output_pins": ["D10", "A0"],
    "pin_labels_came_from": "device",
}


def drive_the_line_panel(script: str, lines=None) -> dict:
    """Build the Lines panel over `lines`, run `script`, and read what it printed."""
    module = (web_directory() / "elements" / "line_map_panel_element.js").as_uri()
    return json.loads(
        run_in_node(
            f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
            "installMinimalDom();\n"
            f"const {{ LineMapPanelElement }} = await import({module!r});\n"
            f"const lines = {json_dumps(LINES_WITH_TWO_FREE_INPUT_PINS if lines is None else lines)};\n"
            "const panel = new LineMapPanelElement();\n"
            "panel.renderShell();\n"
            "panel.adoptDraft(lines);\n"
            "panel.paint(lines);\n" + script
        )
    )


def test_a_new_line_arrives_on_a_free_pin_rather_than_blank() -> None:
    """Adding a line does not create one -- the board's lines exist whether they
    are named or not -- so the row arrives on the free pin with the lowest line
    number. A blank row would be one the daemon refuses, and a person cannot
    invent a ninth input by clicking.
    """
    result = drive_the_line_panel(
        "panel.addLine('in');\n"
        "console.log(JSON.stringify({ added: panel.draft.input_lines.at(-1),\n"
        "  free: panel.unclaimedPins('in'), unsaved: panel.hasUnsavedEdits,\n"
        "  saveOffered: !panel.saveButton.disabled }));\n"
    )
    assert result["added"]["pin_label"] == "D7"
    assert result["added"]["line_index"] == 1, "resolved from the board's own table, not guessed"
    assert result["added"]["name"] == "input_1", "named after the line it is on, and unique"
    assert result["free"] == ["D8"]
    assert result["unsaved"] is True
    assert result["saveOffered"] is True


def test_every_remaining_pin_can_be_named_at_once() -> None:
    """The panel's answer to "the board has eight inputs and I want all of them"."""
    result = drive_the_line_panel(
        "panel.addEveryUnclaimedPin('in');\n"
        "console.log(JSON.stringify({ pins: panel.draft.input_lines.map((l) => l.pin_label),\n"
        "  free: panel.unclaimedPins('in') }));\n"
    )
    assert result["pins"] == ["D6", "D7", "D8"]
    assert result["free"] == []


def test_a_board_with_every_pin_named_offers_no_more() -> None:
    """A board has a fixed number of lines. The button says so rather than
    adding a row the daemon would refuse."""
    result = drive_the_line_panel(
        "panel.addEveryUnclaimedPin('in');\n"
        "const buttons = panel.root.descendants().filter((n) => n.tagName === 'button');\n"
        "const add = buttons.find((b) => b.textContent.startsWith('add an input'));\n"
        "console.log(JSON.stringify({ disabled: add.disabled }));\n"
    )
    assert result["disabled"] is True


def test_removing_a_line_a_graph_names_holds_the_save() -> None:
    """The failure this exists to move earlier.

    Removing `reward_valve` while go-nogo pulses it does not fail in the panel
    and does not fail at the PATCH -- it fails at the next upload, which is the
    start of a session with an animal in the booth. So the panel reads the
    loaded config's graphs and refuses to offer the save, naming the graph.
    """
    result = drive_the_line_panel(
        # What readWhichLinesTheGraphsName() would have loaded from the config.
        "panel.namesUsedByGraphs = { in: new Map(), out: new Map([['reward_valve', ['go-nogo']]]) };\n"
        "panel.removeLine('out', panel.draft.output_lines[0]);\n"
        "const banners = panel.root.descendants()\n"
        "  .filter((n) => n.className === 'failure').map((n) => n.textContent);\n"
        "console.log(JSON.stringify({ banners, saveOffered: !panel.saveButton.disabled,\n"
        "  outputs: panel.draft.output_lines.length }));\n"
    )
    assert result["outputs"] == 0, "the row goes -- loud and reversible beats forbidden"
    assert result["saveOffered"] is False
    assert any("reward_valve" in banner and "go-nogo" in banner for banner in result["banners"])


def test_removing_a_line_no_graph_names_is_just_allowed() -> None:
    result = drive_the_line_panel(
        "panel.namesUsedByGraphs = { in: new Map(), out: new Map() };\n"
        "panel.removeLine('in', panel.draft.input_lines[0]);\n"
        "const banners = panel.root.descendants().filter((n) => n.className === 'failure');\n"
        "console.log(JSON.stringify({ banners: banners.length,\n"
        "  saveOffered: !panel.saveButton.disabled, free: panel.unclaimedPins('in') }));\n"
    )
    assert result["banners"] == 0
    assert result["saveOffered"] is True
    # And the pin it was on is offered again, because a name is all that went.
    assert result["free"] == ["D6", "D7", "D8"]


def test_the_two_directions_do_not_protect_each_others_names() -> None:
    """An input called `lever` is not the output called `lever`: two numberings
    over two disjoint sets of pins, which is the same mistake the line map
    itself refuses to let a config make."""
    module = (web_directory() / "elements" / "line_map_panel_element.js").as_uri()
    graph = {
        "name": "go-nogo",
        "states": [
            {
                "name": "Wait",
                "on_entry": [{"line": "ready_lamp", "kind": "high"}],
                "on_exit": [{"line": "cue_lamp", "kind": "low"}],
                "transitions": [{"when": {"all": ["lever"], "none": ["abort"]}, "goto": "Hit"}],
            }
        ],
    }
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ lineNamesUsedBy }} = await import({module!r});\n"
        f"console.log(JSON.stringify(lineNamesUsedBy({json_dumps(graph)})));\n"
    )
    assert sorted(map(tuple, json.loads(printed))) == [
        ("in", "abort"),
        ("in", "lever"),
        ("out", "cue_lamp"),
        ("out", "ready_lamp"),
    ]


def test_the_serial_monitor_is_closed_and_silent_until_somebody_opens_it() -> None:
    """It is a debugging view, and a debugging view has two costs when it is on
    by default.

    The visible one: it is the loudest thing on the page for the people who need
    it least -- every line in and out of the port, scrolling, under a session
    somebody is trying to watch. The one that matters more: it holds a WebSocket
    per open tab against a daemon whose whole reason for existing is one serial
    port, for a question nobody has asked yet.

    So it is collapsed, **and not connected while collapsed**. The second half
    is the one worth a test: a panel that merely hid its table would still be
    streaming, and nothing on screen would say so.

    The fold is the panel's own -- the one every panel has -- and not a second
    `<details>` inside it. Two nested disclosures said the same thing twice, and
    the outer one shut a panel that was still holding a socket open behind it.
    """
    module = (web_directory() / "elements" / "serial_monitor_panel_element.js").as_uri()
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ SerialMonitorPanelElement }} = await import({module!r});\n"
        "const panel = new SerialMonitorPanelElement();\n"
        "let backfills = 0;\n"
        "const socket = { addEventListener() {}, close() {} };\n"
        "Object.defineProperty(panel, 'api', { value: {\n"
        "  readDeviceMonitor: async () => { backfills += 1; return { lines: [], ring_capacity: 9 }; },\n"
        "  openDeviceMonitorStream: () => socket,\n"
        "} });\n"
        "panel.renderShell();\n"
        "panel.installDisclosure();\n"
        "const closedByDefault = panel.collapsed === true;\n"
        "// And exactly one disclosure: the panel\'s own.\n"
        "const disclosures = panel.root.descendants()\n"
        "  .filter((n) => n.tagName === \'details\' || n.className === \'disclosure\').length;\n"
        "await panel.start();\n"
        "const socketsWhileClosed = panel.openSockets.length;\n"
        "const backfillsWhileClosed = backfills;\n"
        "await panel.setCollapsed(false);\n"
        "const socketsWhenOpened = panel.openSockets.length;\n"
        "await panel.setCollapsed(true);\n"
        "console.log(JSON.stringify({ closedByDefault, disclosures, socketsWhileClosed,\n"
        "  backfillsWhileClosed, socketsWhenOpened, socketsAfterClosing: panel.openSockets.length,\n"
        "  backfilledOnOpening: backfills }));\n"
    )
    result = json.loads(printed)
    assert result["closedByDefault"]
    assert result["disclosures"] == 1, "the monitor has two disclosures again"
    assert result["socketsWhileClosed"] == 0, "a collapsed monitor was still streaming"
    assert result["backfillsWhileClosed"] == 0, "a collapsed monitor still read the ring"
    # And it works when opened -- including the backfill, which is what makes
    # opening it *after* the fault still useful: the greeting and whatever
    # prompted the click both happened before it.
    assert result["socketsWhenOpened"] == 1
    assert result["backfilledOnOpening"] == 1
    # Closing it again gives the socket back rather than leaving one behind a
    # panel nobody is looking at.
    assert result["socketsAfterClosing"] == 0


def test_a_panel_folds_away_under_its_heading_and_its_own_controls_still_work() -> None:
    """A view holds four panels now, and on a laptop in a booth the one being
    watched is below the fold. So every panel folds: the heading is the handle,
    and the body goes under it.

    Two things are worth pinning. The fold has to be *general* -- it is
    installed by the base class by walking the section every panel builds, so a
    panel that changes its markup would silently lose it. And the heading is not
    only a handle: the Device panel keeps its `connect` button up there, and a
    click on that must connect rather than fold the panel away under the
    person's hand.
    """
    module = (web_directory() / "elements" / "device_panel_element.js").as_uri()
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ DevicePanelElement }} = await import({module!r});\n"
        "const panel = new DevicePanelElement();\n"
        "panel.renderShell();\n"
        "panel.installDisclosure();\n"
        "const section = panel.root.children.find((n) => n.tagName === 'section');\n"
        "const heading = section.children[0];\n"
        "const contents = section.children[1];\n"
        "const openByDefault = panel.collapsed === false && contents.hidden === false;\n"
        "// The body went under the heading rather than being dropped.\n"
        "const bodyIsInside = contents.children.includes(panel.body);\n"
        "heading.dispatch('click', { target: heading });\n"
        "const foldedByTheHeading = panel.collapsed && contents.hidden;\n"
        "heading.dispatch('click', { target: panel.disclosureToggle });\n"
        "const unfoldedByTheToggle = panel.collapsed === false;\n"
        "// The connect button is in the heading, and it is not a fold handle.\n"
        "heading.dispatch('click', { target: panel.connectButton });\n"
        "console.log(JSON.stringify({ openByDefault, bodyIsInside, foldedByTheHeading,\n"
        "  unfoldedByTheToggle, stillOpen: panel.collapsed === false,\n"
        "  headingKeptItsTitle: heading.children.some((n) => n.textContent === 'Device') }));\n"
    )
    result = json.loads(printed)
    assert result["openByDefault"], "a panel nobody asked to fold must be readable"
    assert result["bodyIsInside"], "the fold moved the panel's body out of the panel"
    assert result["foldedByTheHeading"]
    assert result["unfoldedByTheToggle"]
    assert result["stillOpen"], "clicking connect folded the panel instead of connecting"
    assert result["headingKeptItsTitle"]


def test_restoring_the_focus_after_a_repaint_does_not_move_the_page() -> None:
    """The Session panel rebuilds its manual controls every two seconds, and the
    button a person just pressed -- "arm and start" -- is what has the focus.

    A plain `focus()` scrolls that element back into view. So the page jumped
    upward every two seconds under somebody watching the trace or the serial
    monitor further down, for as long as a trial was running: the one moment
    they are least able to look away and fix it.

    What the repaint is restoring is the *caret*. Where the page is scrolled to
    belongs to the reader, and a repaint nobody asked for must not take it.
    """
    module = (web_directory() / "elements" / "device_panel_element.js").as_uri()
    printed = run_in_node(
        f"import {{ installMinimalDom }} from {MINIMAL_DOM!r};\n"
        "installMinimalDom();\n"
        f"const {{ DevicePanelElement }} = await import({module!r});\n"
        "const panel = new DevicePanelElement();\n"
        "panel.renderShell();\n"
        "const pressed = panel.connectButton;\n"
        "const focusedWith = [];\n"
        "pressed.focus = (options) => focusedWith.push(options ?? null);\n"
        "panel.root.activeElement = pressed;\n"
        "panel.repaintPreservingFocus(() => {});\n"
        "console.log(JSON.stringify({ focusedWith }));\n"
    )
    result = json.loads(printed)
    assert result["focusedWith"] == [{"preventScroll": True}], (
        "the focus was restored in a way that scrolls the page to it"
    )
