// SPDX-License-Identifier: LGPL-3.0-or-later
//
// A graph, drawn. Read-only in v1 (dev/DAEMON.md §5).
//
// The layout is breadth-first from the entry state, one column per depth. It is
// the crudest thing that works and it is chosen deliberately: a paradigm graph
// is a sequence with branches -- foreperiod, cue, response, outcome -- so depth
// from the entry is very nearly the reading order somebody already has in their
// head. A force-directed layout would look more like a graph and less like the
// trial.
//
// What the drawing is *for* is the check no list of states can do: seeing that
// a state has no way out, or that two outcomes are unreachable, before an animal
// is in the booth. So unreachable states are drawn, in their own column, rather
// than dropped -- a state you cannot see is a state you will not fix.

const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

const NODE_WIDTH = 132;
const NODE_HEIGHT = 40;
const COLUMN_GAP = 74;
const ROW_GAP = 22;
const MARGIN = 16;

function svg(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NAMESPACE, tag);
  for (const [name, value] of Object.entries(attributes)) {
    node.setAttribute(name, `${value}`);
  }
  return node;
}

/// Every edge out of a state, both kinds, in one shape.
export function edgesOf(state) {
  const edges = [];
  if (state.timeout) {
    edges.push({ kind: "timeout", goto: state.timeout.goto, label: `after ${state.timeout.after}` });
  }
  for (const transition of state.transitions || []) {
    const predicate = transition.when || {};
    const parts = [];
    if ((predicate.all || []).length) parts.push(`all ${predicate.all.join("+")}`);
    if ((predicate.any || []).length) parts.push(`any ${predicate.any.join("/")}`);
    if ((predicate.none || []).length) parts.push(`not ${predicate.none.join("+")}`);
    edges.push({
      kind: "transition",
      goto: transition.goto,
      label: parts.join(", ") + (transition.hold ? ` held ${transition.hold}` : ""),
    });
  }
  return edges;
}

/// Column index per state: distance from the entry, and a column of its own for
/// anything the entry cannot reach.
export function layoutColumns(graph) {
  const byName = new Map((graph.states || []).map((state) => [state.name, state]));
  const depthByName = new Map();
  let frontier = byName.has(graph.entry) ? [graph.entry] : [];
  let depth = 0;
  while (frontier.length > 0) {
    const next = [];
    for (const name of frontier) {
      if (depthByName.has(name)) continue;
      depthByName.set(name, depth);
      for (const edge of edgesOf(byName.get(name) || {})) {
        if (byName.has(edge.goto) && !depthByName.has(edge.goto)) next.push(edge.goto);
      }
    }
    frontier = next;
    depth += 1;
  }
  const unreachable = (graph.states || [])
    .map((state) => state.name)
    .filter((name) => !depthByName.has(name));
  for (const name of unreachable) depthByName.set(name, depth);

  const columns = [];
  for (const [name, column] of depthByName) {
    (columns[column] ||= []).push(name);
  }
  return { columns, depthByName, unreachableNames: new Set(unreachable) };
}

export function renderGraphNodeDiagram(graph) {
  const { columns, unreachableNames } = layoutColumns(graph);
  const byName = new Map((graph.states || []).map((state) => [state.name, state]));

  const centreByName = new Map();
  const tallestColumn = Math.max(1, ...columns.map((column) => (column || []).length));
  const width = MARGIN * 2 + columns.length * NODE_WIDTH + (columns.length - 1) * COLUMN_GAP;
  const height = MARGIN * 2 + tallestColumn * NODE_HEIGHT + (tallestColumn - 1) * ROW_GAP;

  columns.forEach((names, columnIndex) => {
    const columnHeight = names.length * NODE_HEIGHT + (names.length - 1) * ROW_GAP;
    const top = (height - columnHeight) / 2;
    names.forEach((name, rowIndex) => {
      centreByName.set(name, {
        x: MARGIN + columnIndex * (NODE_WIDTH + COLUMN_GAP) + NODE_WIDTH / 2,
        y: top + rowIndex * (NODE_HEIGHT + ROW_GAP) + NODE_HEIGHT / 2,
      });
    });
  });

  const diagram = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    role: "img",
    "aria-label": `state diagram of ${graph.name}`,
  });

  const markers = svg("defs");
  const arrow = svg("marker", {
    id: "statemachined-arrowhead",
    viewBox: "0 0 8 8",
    refX: 7,
    refY: 4,
    markerWidth: 7,
    markerHeight: 7,
    orient: "auto-start-reverse",
  });
  arrow.append(svg("path", { d: "M 0 0 L 8 4 L 0 8 z", fill: "currentColor" }));
  markers.append(arrow);
  diagram.append(markers);

  // Edges first, so a node is never drawn under a line.
  for (const state of graph.states || []) {
    const from = centreByName.get(state.name);
    for (const edge of edgesOf(state)) {
      const to = centreByName.get(edge.goto);
      if (!from || !to) continue;
      diagram.append(...edgeShapes(from, to, edge));
    }
  }

  for (const state of graph.states || []) {
    const centre = centreByName.get(state.name);
    if (!centre) continue;
    diagram.append(nodeShape(state, centre, {
      isEntry: state.name === graph.entry,
      isUnreachable: unreachableNames.has(state.name),
    }));
  }

  if (!byName.has(graph.entry)) {
    // The store refuses this, so it can only be an unsaved edit -- which is
    // exactly when saying so is worth anything.
    const warning = svg("text", { x: MARGIN, y: MARGIN, class: "diagram-warning" });
    warning.textContent = `entry ${graph.entry} is not a state`;
    diagram.append(warning);
  }
  return diagram;
}

function edgeShapes(from, to, edge) {
  const startX = from.x + NODE_WIDTH / 2;
  const endX = to.x - NODE_WIDTH / 2;
  const goingBack = endX < startX;
  // A backward edge -- a retry, an abort that returns -- gets a curve above the
  // nodes rather than a straight line through them.
  const path = svg("path", {
    class: edge.kind === "timeout" ? "edge timeout" : "edge transition",
    "marker-end": "url(#statemachined-arrowhead)",
    fill: "none",
    d: goingBack
      ? `M ${from.x} ${from.y - NODE_HEIGHT / 2} ` +
        `C ${from.x} ${from.y - NODE_HEIGHT - 30}, ${to.x} ${to.y - NODE_HEIGHT - 30}, ` +
        `${to.x} ${to.y - NODE_HEIGHT / 2}`
      : `M ${startX} ${from.y} C ${startX + 30} ${from.y}, ${endX - 30} ${to.y}, ${endX} ${to.y}`,
  });
  const label = svg("text", {
    class: "edge-label",
    x: goingBack ? (from.x + to.x) / 2 : (startX + endX) / 2,
    y: goingBack ? Math.min(from.y, to.y) - NODE_HEIGHT / 2 - 24 : (from.y + to.y) / 2 - 5,
    "text-anchor": "middle",
  });
  label.textContent = edge.label;
  return [path, label];
}

function nodeShape(state, centre, { isEntry, isUnreachable }) {
  const group = svg("g", {
    class:
      "node" +
      (state.outcome ? " terminal" : "") +
      (isEntry ? " entry" : "") +
      (isUnreachable ? " unreachable" : ""),
  });
  group.append(
    svg("rect", {
      x: centre.x - NODE_WIDTH / 2,
      y: centre.y - NODE_HEIGHT / 2,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      rx: 6,
    }),
  );
  const name = svg("text", {
    x: centre.x,
    y: centre.y + (state.outcome ? -2 : 4),
    "text-anchor": "middle",
    class: "node-name",
  });
  name.textContent = state.name;
  group.append(name);
  if (state.outcome) {
    const outcome = svg("text", {
      x: centre.x,
      y: centre.y + 12,
      "text-anchor": "middle",
      class: "node-outcome",
    });
    outcome.textContent = state.outcome;
    group.append(outcome);
  }
  const title = document.createElementNS(SVG_NAMESPACE, "title");
  title.textContent = [
    isEntry ? "entry state" : null,
    isUnreachable ? "NOT REACHABLE from the entry state" : null,
    state.outcome ? `terminal: ${state.outcome}` : null,
    (state.on_entry || []).length ? `on entry: ${describeActions(state.on_entry)}` : null,
    (state.on_exit || []).length ? `on exit: ${describeActions(state.on_exit)}` : null,
  ]
    .filter(Boolean)
    .join("\n");
  group.append(title);
  return group;
}

export function describeActions(actions) {
  return (actions || [])
    .map((action) =>
      action.kind === "pulse"
        ? `pulse ${action.line} ${action.pulse_ms} ms`
        : `${action.kind} ${action.line}`,
    )
    .join(", ");
}

/// The diagram's own styles, kept beside the drawing rather than in the shared
/// sheet: nothing else in the UI has an edge or a terminal node.
export const GRAPH_DIAGRAM_STYLE_TEXT = `
  svg { max-width: 100%; height: auto; color: var(--muted); }
  .node rect { fill: #f6f8fa; stroke: var(--panel-border); }
  .node.entry rect { stroke: var(--accent); stroke-width: 2; }
  .node.terminal rect { fill: #eaf1f8; }
  .node.unreachable rect { stroke: var(--bad); stroke-dasharray: 4 3; }
  .node-name { font-size: 12px; fill: #16202a; }
  .node-outcome { font-size: 10px; fill: var(--muted); }
  .edge { stroke: #8fa2b3; stroke-width: 1.3; }
  .edge.timeout { stroke-dasharray: 5 3; }
  .edge-label { font-size: 10px; fill: var(--muted); }
  .diagram-warning { font-size: 11px; fill: var(--bad); }
  @media (prefers-color-scheme: dark) {
    .node rect { fill: #1f262e; }
    .node.terminal rect { fill: #232f3b; }
    .node-name { fill: #dfe6ec; }
    .edge { stroke: #55636f; }
  }
`;
