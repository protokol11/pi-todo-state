/**
 * todo-state pi tools + advisory PLAN.md drift caution.
 *
 * Thin TS layer over the verified Python core (`python -m todo_state`): one fold, one
 * source of truth (DRY). Every core call uses an argv array with `shell: false`; no
 * string is ever interpolated into a shell.
 *
 * Drift caution (RTL-04 applied as advisory only): a `tool_call` handler evaluates the
 * target against `.docs/PLAN.md` and stores the verdict read-only. The matching
 * `tool_result` handler appends one visible caution to the result. Nothing cancels a
 * call; the hook has no block/abort/terminate path.
 *
 * Missing `.docs/PLAN.md`: the hook stays silent and one loud extension-log line is
 * written per session.
 *
 * Spec: `.docs/SPECS/harness-core/todo-state-tools.md`.
 */

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { delimiter, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import {
  PlanScanner,
  isInPlan,
  type CallContext,
  type PlanVerdict,
} from "./plan-scan.ts";

/** Where `todo_state` lives. `python -m todo_state` is run with this on PYTHONPATH. */
const EXTENSION_DIR = resolve(fileURLToPath(new URL(".", import.meta.url)));

/** Python from PATH, per spec. The core is stdlib-only, so no launcher is needed. */
const PYTHON = "python";

const DRIFT_TOOLS = new Set(["edit", "write", "bash"]);
const CAUTION_MARKER = "[todo-state] drift caution";

interface CliResult {
  code: number;
  stdout: string;
  stderr: string;
}

interface BoardTask {
  task_id: string;
  title: string;
  status: string;
  note?: string | null;
}

interface Board {
  seq: number;
  in_progress: string | null;
  tasks: BoardTask[];
}

interface ActiveTask {
  id: string;
  title: string;
}

function pythonPath(): string {
  const existing = process.env.PYTHONPATH;
  return existing && existing.length > 0
    ? `${EXTENSION_DIR}${delimiter}${existing}`
    : EXTENSION_DIR;
}

/** Spawn the core CLI. `shell: false` and an argv array are the whole safety story. */
function runPython(args: string[], cwd: string, signal: AbortSignal | undefined): Promise<CliResult> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(PYTHON, args, {
      cwd,
      shell: false,
      windowsHide: true,
      env: { ...process.env, PYTHONPATH: pythonPath() },
    });

    let stdout = "";
    let stderr = "";
    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr?.on("data", (chunk: string) => {
      stderr += chunk;
    });

    child.on("error", reject);
    child.on("close", (code) => {
      resolvePromise({ code: code ?? 1, stdout, stderr });
    });

    if (signal !== undefined) {
      const onAbort = (): void => {
        child.kill();
      };
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
      child.on("close", () => signal.removeEventListener("abort", onAbort));
    }
  });
}

async function runTodo(
  cwd: string,
  sessionId: string,
  command: string[],
  signal: AbortSignal | undefined,
): Promise<CliResult> {
  // Global flags precede the subcommand: argparse does not accept them afterwards.
  return runPython(["-m", "todo_state", "--session-id", sessionId, ...command], cwd, signal);
}

function requireOk(result: CliResult, label: string): void {
  if (result.code === 0) return;
  const detail = result.stderr.trim() || result.stdout.trim() || `exit ${result.code}`;
  throw new Error(`${label} failed: ${detail}`);
}

function parseBoard(text: string): Board | null {
  try {
    const value = JSON.parse(text) as Partial<Board> | null;
    if (value === null || typeof value !== "object") return null;
    if (!Array.isArray(value.tasks)) return null;
    if (typeof value.seq !== "number") return null;
    const inProgress = typeof value.in_progress === "string" ? value.in_progress : null;
    return { seq: value.seq, in_progress: inProgress, tasks: value.tasks as BoardTask[] };
  } catch {
    return null;
  }
}

async function fetchBoard(
  cwd: string,
  sessionId: string,
  signal: AbortSignal | undefined,
): Promise<Board> {
  const result = await runTodo(cwd, sessionId, ["board", "--json"], signal);
  requireOk(result, "todo board");
  const board = parseBoard(result.stdout);
  if (board === null) throw new Error("todo board returned unparseable JSON");
  return board;
}

function boardSummary(board: Board): string {
  const active = board.in_progress ?? "none";
  const lines = [`seq=${board.seq} tasks=${board.tasks.length} in_progress=${active}`];
  for (const task of board.tasks) {
    const note = task.note ? `  note: ${task.note}` : "";
    lines.push(`${task.status.padEnd(12)} ${task.task_id}  ${task.title}${note}`);
  }
  return lines.join("\n");
}

function sessionIdOf(ctx: ExtensionContext): string {
  try {
    return ctx.sessionManager.getSessionId();
  } catch {
    return "todo-state-tool";
  }
}

function statePath(root: string): string {
  const directory = process.env.TODO_STATE_DIR;
  return directory === undefined || directory.length === 0
    ? join(root, ".pi", "todo", "state.json")
    : resolve(root, directory, "state.json");
}

/** Active task from the core's cache. Absent or stale cache means "no active task". */
function readActiveTask(root: string): ActiveTask | null {
  try {
    const board = parseBoard(readFileSync(statePath(root), "utf8"));
    if (board === null || board.in_progress === null) return null;
    const task = board.tasks.find((candidate) => candidate.task_id === board.in_progress);
    return { id: board.in_progress, title: task?.title ?? board.in_progress };
  } catch {
    return null;
  }
}

function callContextFor(event: { toolName: string; input?: unknown }): CallContext {
  const input = (event.input ?? {}) as Record<string, unknown>;
  return {
    toolName: event.toolName,
    path: typeof input.path === "string" ? input.path : undefined,
    command: typeof input.command === "string" ? input.command : undefined,
  };
}

function buildCaution(active: ActiveTask, verdict: PlanVerdict): string {
  const targets = verdict.targets.length > 0 ? verdict.targets.join(", ") : "the call target";
  const nearest =
    verdict.nearestSection === null
      ? ""
      : ` Nearest plan section: "${verdict.nearestSection}".`;
  return (
    `${CAUTION_MARKER}: active task "${active.title}" (${active.id}) is outside ` +
    `.docs/PLAN.md. Target: ${targets}.${nearest} Advisory only; the call was not blocked.`
  );
}

const WriteParams = Type.Object({
  title: Type.String({ description: "Task title: what the model is about to do." }),
  status: Type.Optional(
    Type.Union([Type.Literal("pending"), Type.Literal("in_progress")]),
  ),
  task_id: Type.Optional(Type.String({ description: "Explicit task id; generated when omitted." })),
  note: Type.Optional(Type.String({ description: "Optional note stored on the task." })),
});

const UpdateParams = Type.Object({
  task_id: Type.String({ description: "Id of the task to update." }),
  status: Type.Union([Type.Literal("done"), Type.Literal("blocked"), Type.Literal("wontfix")]),
  note: Type.Optional(Type.String({ description: "Optional closing note." })),
});

const ReadParams = Type.Object({
  at_seq: Type.Optional(
    Type.Number({ description: "Replay the log up to this seq instead of reading the head." }),
  ),
});

const UPDATE_COMMAND: Record<string, string> = {
  done: "done",
  blocked: "block",
  wontfix: "wontfix",
};

export default function todoState(pi: ExtensionAPI): void {
  let root = process.cwd();
  let scanner: PlanScanner | null = null;
  const pendingCautions = new Map<string, string>();
  let hookErrorLogged = false;

  function logLine(message: string): void {
    // Extension log: stderr is the runtime's diagnostic channel in every mode.
    console.error(`todo-state: ${message}`);
  }

  function logHookError(error: unknown): void {
    if (hookErrorLogged) return;
    hookErrorLogged = true;
    const message = error instanceof Error ? error.message : String(error);
    logLine(`drift hook disabled after error: ${message}`);
  }

  function ensureScanner(cwd: string): PlanScanner {
    if (scanner !== null && root === cwd) return scanner;
    root = cwd;
    scanner = new PlanScanner(join(cwd, ".docs", "PLAN.md"), {
      onMissing: (load) => {
        logLine(`PLAN.md not found at ${load.planPath}; drift caution disabled (hook silent).`);
      },
      onError: (load) => {
        if (load.status === "error") {
          logLine(`PLAN.md unreadable at ${load.planPath}: ${load.error}`);
        }
      },
    });
    return scanner;
  }

  pi.on("session_start", (_event, ctx) => {
    root = ctx.cwd;
    scanner = null;
    pendingCautions.clear();
    hookErrorLogged = false;
    try {
      // Resolve once at start so a missing plan leaves one loud log line.
      ensureScanner(ctx.cwd).current();
    } catch (error) {
      logHookError(error);
    }
  });

  pi.on("tool_call", (event, ctx) => {
    try {
      if (!DRIFT_TOOLS.has(event.toolName)) return undefined;
      const active = readActiveTask(ctx.cwd);
      if (active === null) return undefined; // no active task -> silent

      const plan = ensureScanner(ctx.cwd).current();
      const verdict = isInPlan(callContextFor(event), plan);
      if (verdict.inPlan) return undefined; // match, no-plan, no-signal -> silent

      pendingCautions.set(event.toolCallId, buildCaution(active, verdict));
    } catch (error) {
      // A thrown tool_call handler blocks the call as a fail-safe. The caution is
      // advisory, so a hook failure must never reach that path.
      logHookError(error);
    }
    return undefined;
  });

  pi.on("tool_result", (event) => {
    try {
      const caution = pendingCautions.get(event.toolCallId);
      if (caution === undefined) return undefined;
      pendingCautions.delete(event.toolCallId);

      const content = [...event.content, { type: "text" as const, text: `\n\n${caution}` }];
      return event.structuredContent === undefined
        ? { content }
        : { content, structuredContent: event.structuredContent };
    } catch (error) {
      logHookError(error);
      return undefined;
    }
  });

  pi.registerTool({
    name: "todo_write",
    label: "Todo write",
    description:
      "Add one task to the append-only todo board. Use status=in_progress to start it " +
      "immediately; the core demotes any previous in-progress task to pending.",
    parameters: WriteParams,
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false },
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const args = ["add", params.title];
      if (params.task_id !== undefined) args.push("--task-id", params.task_id);
      if (params.note !== undefined) args.push("--note", params.note);
      args.push("--status", params.status ?? "pending");

      const result = await runTodo(ctx.cwd, sessionIdOf(ctx), args, signal);
      requireOk(result, "todo_write");

      const taskId = result.stdout.trim().split(/\r?\n/)[0] ?? "";
      const board = await fetchBoard(ctx.cwd, sessionIdOf(ctx), signal);
      return {
        content: [
          {
            type: "text" as const,
            text: `todo_write: added ${taskId}\n${boardSummary(board)}`,
          },
        ],
        details: { board, taskId },
      };
    },
  });

  pi.registerTool({
    name: "todo_update",
    label: "Todo update",
    description:
      "Close one task on the todo board with status done, blocked, or wontfix. " +
      "The core rejects an unknown task id with a reason.",
    parameters: UpdateParams,
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false },
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const args = [UPDATE_COMMAND[params.status], params.task_id];
      if (params.note !== undefined) args.push("--note", params.note);

      const result = await runTodo(ctx.cwd, sessionIdOf(ctx), args, signal);
      requireOk(result, "todo_update");

      const board = await fetchBoard(ctx.cwd, sessionIdOf(ctx), signal);
      return {
        content: [
          {
            type: "text" as const,
            text: `todo_update: ${params.status} ${params.task_id}\n${boardSummary(board)}`,
          },
        ],
        details: { board, taskId: params.task_id },
      };
    },
  });

  pi.registerTool({
    name: "todo_read",
    label: "Todo read",
    description: "Read the todo board as JSON. Pass at_seq to replay the log up to that seq.",
    parameters: ReadParams,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const args = ["board", "--json"];
      if (params.at_seq !== undefined) args.push("--at-seq", String(params.at_seq));

      const result = await runTodo(ctx.cwd, sessionIdOf(ctx), args, signal);
      requireOk(result, "todo_read");
      const board = parseBoard(result.stdout);
      if (board === null) throw new Error("todo_read: board returned unparseable JSON");

      return {
        content: [{ type: "text" as const, text: JSON.stringify(board, null, 2) }],
        details: { board },
      };
    },
  });
}
