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


def test_the_editors_outcome_names_are_the_ones_the_store_accepts() -> None:
    source = (web_directory() / "elements" / "graph_store_panel_element.js").read_text()
    block = re.search(r"const OUTCOME_NAMES = \[(.*?)\];", source, re.S).group(1)
    offered = {name for name in re.findall(r'"([A-Z_]*)"', block) if name}
    assert offered == set(DECLARABLE_TERMINAL_OUTCOMES)
