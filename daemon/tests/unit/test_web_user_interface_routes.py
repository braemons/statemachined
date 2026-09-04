# SPDX-License-Identifier: GPL-3.0-or-later
"""The UI is served, and it is an honest test of the API.

There is no build step and no framework here, so there is no compiler to catch
an element importing a module that does not exist or calling a route that does
not. These tests are that compiler, and they are cheap because the UI is files.

Four of them do work a build step would otherwise do:

  * **every module it imports exists**, and **every module parses** -- there is
    no bundler to notice a renamed file and no compiler to catch a stray brace;
  * **every `/api/` path the UI mentions is a route this daemon serves** -- the
    claim in dev/DAEMON.md §5 that the web UI uses only this API, checked rather
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

from statemachined.api.application import create_application
from statemachined.api.web_user_interface_routes import read_asset, web_directory
from statemachined.daemon_configuration import DaemonConfiguration
from statemachined.model.trial_outcome import DECLARABLE_TERMINAL_OUTCOMES

ELEMENT_TAG_NAMES = [
    "statemachined-device",
    "statemachined-lines",
    "statemachined-graph",
    "statemachined-session",
    "statemachined-trace",
    "statemachined-firmware",
]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """The app, with nothing pointing at a real rig's directories.

    `connect_on_startup` off because there is no board and a test that waited
    for a serial timeout would be slow for no reading.
    """
    configuration = DaemonConfiguration(
        device_target="loop://",
        connect_on_startup=False,
        graph_store_directory=tmp_path / "graphs",
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
    """The one URL the console repo depends on. dev/DAEMON.md §5."""
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
    """The board owns the labels (dev/PROTOCOL.md §3.6): which pin line 4 is was
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


def test_the_editors_outcome_names_are_the_ones_the_store_accepts() -> None:
    source = (web_directory() / "elements" / "graph_store_panel_element.js").read_text()
    block = re.search(r"const OUTCOME_NAMES = \[(.*?)\];", source, re.S).group(1)
    offered = {name for name in re.findall(r'"([A-Z_]*)"', block) if name}
    assert offered == set(DECLARABLE_TERMINAL_OUTCOMES)
