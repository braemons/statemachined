// SPDX-License-Identifier: LGPL-3.0-or-later
//
// One stylesheet, adopted by every panel's shadow root.
//
// It is a JS module rather than a `.css` file because a shadow root cannot see
// the page's stylesheet -- which is the whole point of using one (dev/DAEMON.md
// §5: triald's ~600 lines of global selectors cannot reach in here, natively,
// with no tooling) -- and because `@import` inside a shadow root is a second
// network round trip before anything renders.
//
// A `CSSStyleSheet` constructed once and adopted by every root, so a page with
// six panels parses this once. `adoptedStyleSheets` is in every browser that
// has custom elements; the fallback exists for the one that does not rather
// than out of caution.

const STYLE_TEXT = `
  :host {
    display: block;
    /* Deliberately relative units and inheritable properties: a panel dropped
       into a console should take that page's size, not fight it. */
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    font-size: 0.9rem;
    line-height: 1.45;
    color: #16202a;

    /* Overridable from outside, which is the one hook a host page gets. A
       shadow root blocks selectors, not custom properties, so this is how a
       console themes a panel without reaching into its markup. */
    --panel-background: #ffffff;
    --panel-border: #d3dae1;
    --panel-heading: #4a5b6a;
    --accent: #1f6feb;
    --good: #1a7f37;
    --bad: #b42318;
    --warn: #9a6700;
    --muted: #667a8a;
    --code-font: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, monospace;
  }

  section {
    background: var(--panel-background);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    padding: 0.85rem 1rem 1rem;
  }

  h2 {
    margin: 0 0 0.6rem;
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--panel-heading);
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    /* The whole strip folds the panel, so say so -- except over the controls
       some panels keep up here, which carry their own cursor and meaning. */
    cursor: pointer;
    user-select: none;
  }

  /* A panel folded away is one line: heading, and nothing under it. The margin
     under the heading goes with the body it was separating. */
  section.collapsed { padding-bottom: 0.85rem; }
  section.collapsed h2 { margin-bottom: 0; }

  h2 button.disclosure {
    font: inherit;
    line-height: 1;
    width: 1rem;
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
  }
  h2 button.disclosure:hover:not(:disabled) { border-color: transparent; }

  [hidden] { display: none !important; }

  h3 {
    /* Enough that a reader sees where one part of a panel ends and the next
       begins, without a rule drawn across it. */
    margin: 1.4rem 0 0.5rem;
    font-size: 0.85rem;
    color: var(--panel-heading);
  }
  h3:first-child { margin-top: 0; }

  dl.fields {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.15rem 0.9rem;
    margin: 0;
  }
  dl.fields dt { color: var(--muted); }
  dl.fields dd { margin: 0; font-variant-numeric: tabular-nums; }

  table {
    width: 100%;
    border-collapse: collapse;
    font-variant-numeric: tabular-nums;
  }
  th, td {
    text-align: left;
    padding: 0.25rem 0.5rem 0.25rem 0;
    border-bottom: 1px solid var(--panel-border);
    vertical-align: middle;
  }
  th { color: var(--muted); font-weight: 500; font-size: 0.8rem; }
  tr:last-child td { border-bottom: none; }

  code, .mono { font-family: var(--code-font); font-size: 0.85em; }

  button {
    font: inherit;
    padding: 0.25rem 0.7rem;
    border: 1px solid var(--panel-border);
    border-radius: 4px;
    background: #f6f8fa;
    cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover:not(:disabled) { color: #fff; filter: brightness(1.08); }
  button.danger:hover:not(:disabled) { border-color: var(--bad); color: var(--bad); }

  input[type="text"], input[type="number"], select, textarea {
    font: inherit;
    padding: 0.15rem 0.35rem;
    border: 1px solid var(--panel-border);
    border-radius: 4px;
    background: #fff;
    color: inherit;
  }
  input[type="number"] { width: 6rem; }
  textarea { width: 100%; font-family: var(--code-font); font-size: 0.85em; }

  .row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .spacer { flex: 1; }
  .muted { color: var(--muted); }
  .good { color: var(--good); }
  .bad { color: var(--bad); }
  .warn { color: var(--warn); }

  /* A level, not a verdict: a lit dot means the line is high, and whether high
     is "on" is what the line map's safe level says. */
  .level {
    display: inline-block;
    width: 0.62rem; height: 0.62rem;
    border-radius: 50%;
    border: 1px solid #b6c0c9;
    background: #e8edf1;
    vertical-align: -1px;
  }
  .level.high { background: #ffbf3d; border-color: #d69a1f; }

  .pill {
    display: inline-block;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.78rem;
    border: 1px solid var(--panel-border);
    background: #f6f8fa;
    color: var(--muted);
  }
  .pill.good { border-color: #b4dcbf; background: #eaf6ee; color: var(--good); }
  .pill.bad  { border-color: #eabcb6; background: #fdeeec; color: var(--bad); }
  .pill.warn { border-color: #e6d5a8; background: #fbf4e2; color: var(--warn); }

  .failure {
    border: 1px solid #eabcb6;
    background: #fdeeec;
    color: var(--bad);
    border-radius: 4px;
    padding: 0.4rem 0.6rem;
    margin-bottom: 0.6rem;
  }
  .failure .context { color: var(--muted); }

  .bits { font-family: var(--code-font); letter-spacing: 0.12em; }

  .scroller { max-height: 22rem; overflow-y: auto; }

  @media (prefers-color-scheme: dark) {
    :host {
      color: #dfe6ec;
      --panel-background: #171d24;
      --panel-border: #2c353e;
      --panel-heading: #93a3b3;
      --accent: #58a6ff;
      --good: #4ac26b;
      --bad: #ff7b72;
      --warn: #d8a43a;
      --muted: #8a9aa8;
    }
    button { background: #1f262e; }
    input[type="text"], input[type="number"], select, textarea {
      background: #1f262e;
    }
    .level { background: #262f38; border-color: #3a444e; }
    .pill { background: #1f262e; }
    .pill.good { background: #14301d; border-color: #245c33; }
    .pill.bad  { background: #34191a; border-color: #6d2b28; }
    .pill.warn { background: #322a13; border-color: #5f4d1c; }
    .failure { background: #34191a; border-color: #6d2b28; }
  }
`;

let sharedStyleSheet = null;

/// Give one shadow root the shared styles, however this browser allows it.
export function adoptSharedStyles(shadowRoot) {
  if ("adoptedStyleSheets" in Document.prototype && typeof CSSStyleSheet === "function") {
    if (sharedStyleSheet === null) {
      sharedStyleSheet = new CSSStyleSheet();
      sharedStyleSheet.replaceSync(STYLE_TEXT);
    }
    shadowRoot.adoptedStyleSheets = [...shadowRoot.adoptedStyleSheets, sharedStyleSheet];
    return;
  }
  const style = document.createElement("style");
  style.textContent = STYLE_TEXT;
  shadowRoot.append(style);
}

export { STYLE_TEXT as SHARED_PANEL_STYLE_TEXT };
