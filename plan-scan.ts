/**
 * Deterministic `.docs/PLAN.md` scanner for the todo-state drift caution.
 *
 * The scan is a surrogate, not a semantic judge (K4): it extracts section headings and
 * inline paths from the markdown and answers one question — does this call target appear
 * among the plan's sections? No model, no clock, no network.
 *
 * The reference to the plan is advisory. `isInPlan` never throws for a missing plan:
 * `readPlan` reports `missing`, and `isInPlan(null)` returns a silent "no-plan" verdict.
 *
 * Spec: `.docs/SPECS/harness-core/todo-state-tools.md`, Implementation Decision 3.
 */

import { readFileSync, statSync } from "node:fs";

/** One section of the plan: a heading plus the paths mentioned in its body. */
export interface PlanSection {
  /** Heading text without the leading `#` characters. */
  heading: string;
  /** Heading level, 2..6. The document title (level 1) is not a section. */
  level: number;
  /** Repo-relative paths found inside the section body, normalized and deduped. */
  paths: string[];
}

/** Parsed plan. Paths and headings are unions over all sections. */
export interface PlanIndex {
  /** Absolute path of the scanned file, for the trace. */
  planPath: string;
  sections: PlanSection[];
  paths: string[];
  headings: string[];
}

/** What a call is about. The drift hook fills path/command; a future hard gate may add task. */
export interface CallContext {
  toolName?: string;
  /** File path the call targets, repo-relative or absolute. */
  path?: string;
  /** Shell command string, tokenized for path-like arguments. */
  command?: string;
  /** Stated task text, matched against section headings. */
  task?: string;
}

export type PlanVerdictReason = "match" | "out-of-plan" | "no-plan" | "no-signal";

/**
 * Three outcomes required by RTL-04 (plus two silent abstentions):
 * match, out-of-plan, and the silent no-plan / no-signal paths.
 */
export interface PlanVerdict {
  inPlan: boolean;
  reason: PlanVerdictReason;
  /** Section that matched, null for abstentions and mismatches. */
  section: string | null;
  /** The plan path or heading that produced the match, null otherwise. */
  matched: string | null;
  /** Targets derived from the call context, for the trace and the caution text. */
  targets: string[];
  /** Best path-prefix neighbour when out of plan, for the caution wording. */
  nearestSection: string | null;
}

/** Result of reading a plan file. `missing` is silent by contract. */
export type PlanLoad =
  | { status: "loaded"; planPath: string; index: PlanIndex; mtimeMs: number }
  | { status: "missing"; planPath: string }
  | { status: "error"; planPath: string; error: string };

const HEADING = /^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$/;
const FENCE = /^[ \t]*(```|~~~)/;
const INLINE_CODE = /`([^`\n]+)`/g;
const FILE_EXTENSION =
  /\.(md|markdown|ts|tsx|js|jsx|mjs|cjs|py|json|jsonl|toml|ya?ml|txt|sh|css|html|sql|rs|go|java|c|h|cpp|ini|cfg)$/i;
const TRAILING_PUNCTUATION = /[.,;:!?)\]}>"']+$/;
const LEADING_PUNCTUATION = /^[("'[{]+/;
const LINE_SUFFIX = /:\d+(?::\d+)?$/;

/** A heading must be at least this long to match task text; `Now` is not an identity. */
const MIN_HEADING_MATCH_LENGTH = 4;

/** Normalize a path-like token: slashes, quotes, `./`, anchors, trailing punctuation. */
export function normalizePath(raw: string): string {
  let value = raw.trim();
  value = value.replace(/\\/g, "/");
  value = value.replace(LEADING_PUNCTUATION, "").replace(TRAILING_PUNCTUATION, "");
  value = value.replace(LINE_SUFFIX, "");
  value = value.replace(/#.*$/, "");
  while (value.startsWith("./")) value = value.slice(2);
  if (value.length > 1 && value.endsWith("/")) value = value.replace(/\/+$/, "");
  return value;
}

function looksLikePath(raw: string): boolean {
  const value = normalizePath(raw);
  if (value.length === 0) return false;
  if (value.includes("://")) return false;
  if (value.includes("/")) return true;
  return FILE_EXTENSION.test(value);
}

/** Split a shell command or prose into candidate tokens without trying to parse shell. */
function tokenize(text: string): string[] {
  const flattened = text.replace(/[;&|<>]+/g, " ");
  return flattened.split(/\s+/).filter((token) => token.length > 0);
}

function pathCandidates(line: string): string[] {
  const candidates: string[] = [];
  INLINE_CODE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = INLINE_CODE.exec(line)) !== null) candidates.push(match[1]);
  // Tokenize the prose only: an inline code span is one candidate, not several.
  const prose = line.replace(/`[^`\n]+`/g, " ");
  for (const token of tokenize(prose)) candidates.push(token);
  return candidates;
}

function addUnique(list: string[], value: string): void {
  if (!list.includes(value)) list.push(value);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Parse markdown into a plan index. Pure: no I/O, same bytes in, same index out. */
export function parsePlan(markdown: string, planPath = ""): PlanIndex {
  const lines = markdown.split(/\r?\n/);
  const sections: PlanSection[] = [];
  const headings: string[] = [];
  let inFrontmatter = false;
  let inFence = false;
  let current: PlanSection | null = null;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    if (index === 0 && line.trim() === "---") {
      inFrontmatter = true;
      continue;
    }
    if (inFrontmatter) {
      if (line.trim() === "---") inFrontmatter = false;
      continue;
    }

    if (FENCE.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;

    const heading = HEADING.exec(line);
    if (heading !== null) {
      const level = heading[1].length;
      const text = heading[2].trim();
      if (level >= 2 && text.length > 0) {
        current = { heading: text, level, paths: [] };
        sections.push(current);
        addUnique(headings, text);
      } else {
        // A level-1 document title is not a plan section.
        current = null;
      }
      continue;
    }

    if (current === null) continue;
    for (const candidate of pathCandidates(line)) {
      if (!looksLikePath(candidate)) continue;
      const normalized = normalizePath(candidate);
      if (normalized.length > 0) addUnique(current.paths, normalized);
    }
  }

  const paths: string[] = [];
  for (const section of sections) {
    for (const path of section.paths) addUnique(paths, path);
  }

  return { planPath, sections, paths, headings };
}

/** Read and parse a plan file. Missing is a normal state, not an exception. */
export function readPlan(planPath: string): PlanLoad {
  let mtimeMs: number;
  try {
    mtimeMs = statSync(planPath).mtimeMs;
  } catch (error) {
    const code = (error as { code?: string }).code;
    if (code === "ENOENT") return { status: "missing", planPath };
    return { status: "error", planPath, error: error instanceof Error ? error.message : String(error) };
  }

  try {
    const markdown = readFileSync(planPath, "utf8");
    return { status: "loaded", planPath, index: parsePlan(markdown, planPath), mtimeMs };
  } catch (error) {
    return { status: "error", planPath, error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * Mtime-cached plan reader.
 *
 * The cache invalidates when the file's mtime changes, so an edited plan is visible on the
 * next tool call without a watcher. A missing file is reported once through `onMissing`:
 * the hook stays silent, while the operator still gets one loud log line.
 */
export class PlanScanner {
  private readonly planPath: string;
  private readonly onMissing: ((load: PlanLoad) => void) | undefined;
  private readonly onError: ((load: PlanLoad) => void) | undefined;
  private loaded: PlanIndex | null = null;
  private loadedMtimeMs: number | null = null;
  private missingReported = false;

  constructor(
    planPath: string,
    options: {
      onMissing?: (load: PlanLoad) => void;
      onError?: (load: PlanLoad) => void;
    } = {},
  ) {
    this.planPath = planPath;
    this.onMissing = options.onMissing;
    this.onError = options.onError;
  }

  /** Current plan index, or null when the file is missing or unreadable. */
  current(): PlanIndex | null {
    const load = readPlan(this.planPath);

    if (load.status === "missing") {
      this.loaded = null;
      this.loadedMtimeMs = null;
      if (!this.missingReported) {
        this.missingReported = true;
        this.onMissing?.(load);
      }
      return null;
    }

    if (load.status === "error") {
      this.loaded = null;
      this.loadedMtimeMs = null;
      this.onError?.(load);
      return null;
    }

    this.missingReported = false;
    if (this.loaded !== null && this.loadedMtimeMs === load.mtimeMs) return this.loaded;
    this.loaded = load.index;
    this.loadedMtimeMs = load.mtimeMs;
    return this.loaded;
  }
}

function pathMatches(target: string, planPath: string): boolean {
  if (target === planPath) return true;
  // Absolute target ending in the repo-relative plan path, or nested below it.
  if (target.endsWith(`/${planPath}`)) return true;
  if (target.includes(`/${planPath}/`)) return true;
  // Target inside a planned directory (`.docs/SPECS/harness-core/`).
  if (target.startsWith(`${planPath}/`)) return true;
  return false;
}

function headingMatches(text: string, heading: string): boolean {
  const needle = heading.trim().toLowerCase();
  if (needle.length < MIN_HEADING_MATCH_LENGTH) return false;
  const pattern = new RegExp(
    `(^|[^\\p{L}\\p{N}_])${escapeRegExp(needle)}($|[^\\p{L}\\p{N}_])`,
    "iu",
  );
  return pattern.test(text.toLowerCase());
}

function collectTargets(context: CallContext): string[] {
  const targets: string[] = [];

  if (context.path !== undefined && context.path.trim().length > 0) {
    const normalized = normalizePath(context.path);
    if (normalized.length > 0) addUnique(targets, normalized);
  }

  const texts = [context.command, context.task].filter(
    (value): value is string => value !== undefined && value.trim().length > 0,
  );
  for (const text of texts) {
    for (const token of tokenize(text)) {
      if (token.startsWith("-") && !token.includes("=")) continue;
      const value = token.includes("=") ? token.slice(token.indexOf("=") + 1) : token;
      if (!looksLikePath(value)) continue;
      const normalized = normalizePath(value);
      if (normalized.length > 0) addUnique(targets, normalized);
    }
  }

  return targets;
}

function sharedSegments(target: string, planPath: string): number {
  const left = target.split("/");
  const right = planPath.split("/");
  const limit = Math.min(left.length, right.length);
  let shared = 0;
  while (shared < limit && left[shared] === right[shared]) shared += 1;
  return shared;
}

function nearestSection(targets: readonly string[], plan: PlanIndex): string | null {
  let best: string | null = null;
  let bestScore = 0;
  for (const section of plan.sections) {
    for (const planPath of section.paths) {
      for (const target of targets) {
        const score = sharedSegments(target, planPath);
        if (score > bestScore) {
          bestScore = score;
          best = section.heading;
        }
      }
    }
  }
  return best;
}

/**
 * Pure predicate over a parsed plan.
 *
 * - `null` plan -> silent `no-plan`.
 * - no derivable target -> silent `no-signal`.
 * - a path listed in the plan, a target inside a planned directory, or a stated task
 *   containing a section heading -> `match`.
 * - otherwise -> `out-of-plan` with the nearest section for the caution.
 */
export function isInPlan(context: CallContext, plan: PlanIndex | null): PlanVerdict {
  const targets = collectTargets(context);

  if (plan === null || plan.sections.length === 0) {
    return {
      inPlan: true,
      reason: "no-plan",
      section: null,
      matched: null,
      targets,
      nearestSection: null,
    };
  }

  const text = context.task ?? "";

  for (const section of plan.sections) {
    for (const planPath of section.paths) {
      for (const target of targets) {
        if (pathMatches(target, planPath)) {
          return {
            inPlan: true,
            reason: "match",
            section: section.heading,
            matched: planPath,
            targets,
            nearestSection: section.heading,
          };
        }
      }
    }
    if (text.length > 0 && headingMatches(text, section.heading)) {
      return {
        inPlan: true,
        reason: "match",
        section: section.heading,
        matched: `heading:${section.heading}`,
        targets,
        nearestSection: section.heading,
      };
    }
  }

  if (targets.length === 0) {
    return {
      inPlan: true,
      reason: "no-signal",
      section: null,
      matched: null,
      targets,
      nearestSection: null,
    };
  }

  return {
    inPlan: false,
    reason: "out-of-plan",
    section: null,
    matched: null,
    targets,
    nearestSection: nearestSection(targets, plan),
  };
}
