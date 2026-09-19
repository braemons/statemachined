// SPDX-License-Identifier: LGPL-3.0-or-later
//
// What a predicate means, in a sentence -- and what it means when it means
// something nobody intended.
//
// Three masks, ANDed (firmware/core/graph/transition.h):
//
//     (w & all)  == all                    every one of these is high
//  && (any == 0 || (w & any) != 0)         at least one; empty means don't care
//  && (w & none) == 0                      none of these is high
//
// The editor shows them as three columns of checkboxes over named lines, which
// is a faithful rendering of the wire and an easy way to write something you
// did not mean: the columns are independent, so a line can be ticked in two of
// them, and two of the three ways of doing that are mistakes.
//
//   * a line in **all and none** must be high and not high. The predicate is
//     never true, and nothing downstream can tell: the graph uploads, the state
//     runs, and the only way out never fires. The daemon refuses this one
//     (model/graph_definition.py), so the editor only has to say why.
//   * a line in **all and any** makes the whole `any` clause moot -- `all`
//     already requires it high, so `any` is satisfied whenever the predicate
//     could fire at all, and every other line in `any` is ignored. "L, and
//     either M or N" written as all:[L], any:[L,M,N] is just "L". Legal, and
//     narrower than it looks.
//   * a line in **any and none** can never satisfy `any` -- if it is high,
//     `none` has already failed -- so it is dead weight, and if it is the only
//     line in `any`, the predicate is never true either.
//
// Pure, so it is tested by running it rather than by reading it. No DOM here.

/// The predicate as a person would say it. `null` where nothing is named.
export function describePredicate(predicate = {}) {
  const all = predicate.all || [];
  const any = predicate.any || [];
  const none = predicate.none || [];
  const clauses = [];
  if (all.length === 1) clauses.push(`${all[0]} is high`);
  if (all.length > 1) clauses.push(`${all.join(" and ")} are high`);
  if (any.length === 1) clauses.push(`${any[0]} is high`);
  if (any.length > 1) clauses.push(`at least one of ${any.join(", ")} is high`);
  if (none.length === 1) clauses.push(`${none[0]} is low`);
  if (none.length > 1) clauses.push(`${none.join(" and ")} are low`);
  if (clauses.length === 0) return null;
  return `fires when ${clauses.join(", and ")}`;
}

/// Every way this predicate says something other than what it looks like.
///
/// `severity` is the difference between "this cannot work" and "this works and
/// is narrower than you think", and the editor renders them differently: one is
/// a refusal waiting to happen at save, the other is a note.
export function predicateProblems(predicate = {}) {
  const all = new Set(predicate.all || []);
  const any = new Set(predicate.any || []);
  const none = new Set(predicate.none || []);
  const shared = (left, right) => [...left].filter((line) => right.has(line)).sort();
  const problems = [];

  const contradictory = shared(all, none);
  if (contradictory.length > 0) {
    problems.push({
      severity: "error",
      lines: contradictory,
      detail:
        `${list(contradictory)} must be high and low at the same time, so this transition ` +
        `can never fire. The daemon refuses a graph with this in it.`,
    });
  }

  const moot = shared(all, any);
  if (moot.length > 0) {
    const ignored = [...any].filter((line) => !all.has(line)).sort();
    problems.push({
      severity: "warning",
      lines: moot,
      detail:
        `${list(moot)} is in both "all" and "any", so the "any" column has no effect` +
        (ignored.length > 0 ? ` and ${list(ignored)} ${ignored.length > 1 ? "are" : "is"} ignored` : "") +
        `. "all" already requires it high.`,
    });
  }

  const wasted = shared(any, none);
  if (wasted.length > 0) {
    const survives = [...any].filter((line) => !none.has(line));
    problems.push({
      severity: survives.length > 0 ? "warning" : "error",
      lines: wasted,
      detail:
        `${list(wasted)} is in both "any" and "none", so it can never satisfy "any": if it is ` +
        `high, "none" has already failed.` +
        (survives.length > 0
          ? ` Only ${list(survives)} can satisfy the "any" column.`
          : ` Nothing else is in "any", so this transition can never fire.`),
    });
  }
  return problems;
}

function list(names) {
  return names.map((name) => `"${name}"`).join(", ");
}
