/**
 * Table tests for the deterministic PLAN.md scanner.
 *
 * Covers path matches, directory matches, absolute targets, bash command tokens,
 * heading-only task matches, out-of-plan mismatches, the silent no-plan path for a
 * missing PLAN.md (with exactly one loud callback), and mtime cache invalidation.
 *
 * Run: node --experimental-strip-types tests/plan-scan.test.ts
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  PlanScanner,
  isInPlan,
  parsePlan,
  readPlan,
  type CallContext,
  type PlanIndex,
  type PlanVerdictReason,
} from "../plan-scan.ts";

const FIXTURE = `---
planmd:
  version: 1
  items:
    - id: harness-core-wave-1
---
# Plan

## Now

1. [x] Mental tree drill (see \`.docs/MEGADRILL_PLANS.md\`).

## Next

1. [ ] **harness-core wave 1**: implement specs (\`.docs/SPECS/harness-core/\`). <!-- id: hc -->
2. [ ] Drill on routing — \`MODELS SPEC.md\` + \`.docs/handoff/\`.

## Backlog

1. [ ] RTL loop (see \`.docs/SPECS/rtl-to-gds/README.md\`).
`;

interface Case {
  name: string;
  context: CallContext;
  inPlan: boolean;
  reason: PlanVerdictReason;
  section: string | null;
  nearestSection: string | null;
}

const CASES: Case[] = [
  {
    name: "path listed exactly in a section matches that section",
    context: { path: ".docs/MEGADRILL_PLANS.md" },
    inPlan: true,
    reason: "match",
    section: "Now",
    nearestSection: "Now",
  },
  {
    name: "file inside a planned directory matches",
    context: { path: ".docs/SPECS/harness-core/todo-state-tools.md" },
    inPlan: true,
    reason: "match",
    section: "Next",
    nearestSection: "Next",
  },
  {
    name: "absolute target ending in a planned path matches",
    context: { path: "C:/repo/.docs/SPECS/rtl-to-gds/README.md" },
    inPlan: true,
    reason: "match",
    section: "Backlog",
    nearestSection: "Backlog",
  },
  {
    name: "absolute target nested in a planned directory matches",
    context: { path: "C:/repo/.docs/SPECS/harness-core/index.ts" },
    inPlan: true,
    reason: "match",
    section: "Next",
    nearestSection: "Next",
  },
  {
    name: "bash command path token matches",
    context: { command: "git diff -- .docs/SPECS/harness-core/index.ts" },
    inPlan: true,
    reason: "match",
    section: "Next",
    nearestSection: "Next",
  },
  {
    name: "heading-only task text matches a section heading",
    context: { task: "work the backlog items" },
    inPlan: true,
    reason: "match",
    section: "Backlog",
    nearestSection: "Backlog",
  },
  {
    name: "unrelated path is out of plan without a neighbour",
    context: { path: "src/unrelated.ts" },
    inPlan: false,
    reason: "out-of-plan",
    section: null,
    nearestSection: null,
  },
  {
    name: "nearby path is out of plan and reports the nearest section",
    context: { path: ".docs/SPECS/other/notes.md" },
    inPlan: false,
    reason: "out-of-plan",
    section: null,
    nearestSection: "Next",
  },
  {
    name: "backslash path is normalized before comparison",
    context: { path: "src\\main.ts" },
    inPlan: false,
    reason: "out-of-plan",
    section: null,
    nearestSection: null,
  },
  {
    name: "generic section word does not rescue an unrelated task",
    context: { task: "do it now" },
    inPlan: true,
    reason: "no-signal",
    section: null,
    nearestSection: null,
  },
  {
    name: "empty context abstains silently",
    context: {},
    inPlan: true,
    reason: "no-signal",
    section: null,
    nearestSection: null,
  },
  {
    name: "missing plan abstains silently",
    context: { path: "src/unrelated.ts" },
    inPlan: true,
    reason: "no-plan",
    section: null,
    nearestSection: null,
  },
];

function loadFixture(): PlanIndex {
  return parsePlan(FIXTURE, ".docs/PLAN.md");
}

for (const item of CASES) {
  test(item.name, () => {
    const plan = item.reason === "no-plan" ? null : loadFixture();
    const verdict = isInPlan(item.context, plan);
    assert.equal(verdict.inPlan, item.inPlan);
    assert.equal(verdict.reason, item.reason);
    assert.equal(verdict.section, item.section);
    assert.equal(verdict.nearestSection, item.nearestSection);
  });
}

test("parser indexes only level-2+ headings and their inline paths", () => {
  const plan = loadFixture();
  assert.deepEqual(
    plan.sections.map((section) => section.heading),
    ["Now", "Next", "Backlog"],
  );
  assert.deepEqual(plan.paths, [
    ".docs/MEGADRILL_PLANS.md",
    ".docs/SPECS/harness-core",
    "MODELS SPEC.md",
    ".docs/handoff",
    ".docs/SPECS/rtl-to-gds/README.md",
  ]);
  // Frontmatter ids are not headings and not paths.
  assert.ok(!plan.headings.includes("planmd"));
  assert.ok(!plan.paths.some((path) => path.includes("harness-core-wave-1")));
});

test("empty plan abstains as no-plan", () => {
  const verdict = isInPlan({ path: "src/a.ts" }, parsePlan(""));
  assert.equal(verdict.inPlan, true);
  assert.equal(verdict.reason, "no-plan");
});

test("missing PLAN.md is reported once, and the hook stays silent", () => {
  const dir = mkdtempSync(join(tmpdir(), "todo-state-plan-"));
  try {
    const planPath = join(dir, ".docs", "PLAN.md");
    const missing: string[] = [];
    const scanner = new PlanScanner(planPath, {
      onMissing: (load) => missing.push(load.planPath),
    });

    assert.equal(scanner.current(), null);
    assert.equal(scanner.current(), null);
    assert.equal(missing.length, 1, "one loud callback per scanner, not per call");

    const load = readPlan(planPath);
    assert.equal(load.status, "missing");
    assert.equal(isInPlan({ path: "src/a.ts" }, null).reason, "no-plan");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("scanner reloads the plan when its mtime changes", () => {
  const dir = mkdtempSync(join(tmpdir(), "todo-state-plan-"));
  try {
    const planPath = join(dir, "PLAN.md");
    writeFileSync(planPath, "# Plan\n\n## Now\n\n- see `.docs/a.md`\n", "utf8");

    const scanner = new PlanScanner(planPath);
    const first = scanner.current();
    assert.ok(first);
    assert.deepEqual(first.paths, [".docs/a.md"]);

    writeFileSync(planPath, "# Plan\n\n## Now\n\n- see `.docs/b.md`\n", "utf8");
    const future = new Date(Date.now() + 5000);
    utimesSync(planPath, future, future);

    const second = scanner.current();
    assert.ok(second);
    assert.deepEqual(second.paths, [".docs/b.md"]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
