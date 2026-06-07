"""Dispatch a single task to a short-lived `claude -p` coding agent.

We deliberately spawn a *fresh* headless agent per task instead of keeping one
long-lived session: short processes can't accumulate context rot, and any crash
is contained to one task. The supervisor stays dumb and unkillable; the agent
does the thinking.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config as C
from .log import log

# Structured result the agent must return as its final answer.
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["spec_ready", "implemented", "failed", "blocked"],
        },
        "spec_id": {"type": "string", "description": "OpenSpec change id (folder name)"},
        "committed": {"type": "boolean"},
        "pushed": {"type": "boolean"},
        "deployed": {"type": "boolean"},
        "tests_passed": {"type": "boolean"},
        "summary": {
            "type": "string",
            "description": (
                "Итог по-русски, в стиле пещерного человека, без воды: 1-2 коротких "
                "фразы, ломаная грамматика, настоящее время. Пример: «Я делать спеку. "
                "Тесты зелёные. Я пушить и деплоить». Технические токены (id спеки, "
                "имена веток и файлов) оставляй как есть."
            ),
        },
        "error": {
            "type": "string",
            "description": (
                "Пусто, если outcome не failed/blocked. Иначе — причина по-русски, "
                "в стиле пещерного человека, без воды. Пример: «Нет openspec. Я не "
                "трогать». Технические токены оставляй как есть."
            ),
        },
    },
    "required": ["outcome", "summary"],
}

SYSTEM_PROMPT = """\
You are an autonomous software delivery agent invoked head-less (no human is \
watching). NEVER ask questions or wait for confirmation — make the best \
reasonable decision and proceed. Your working directory is a git repository \
that uses OpenSpec for spec-driven development.

Hard rules:
- This project is OpenSpec-only. Every change MUST be expressed as an OpenSpec \
change before implementation. If the repo has no `openspec/` directory and you \
were not explicitly told to initialise it, return outcome="blocked".
- Keep the human task as the source of truth for WHAT to build.
- Follow the repository's own conventions: read CLAUDE.md and README.md first.
- Do not invent secrets, do not push to remotes other than `origin`, never run \
destructive history rewrites.
- Your FINAL message must be the required structured object and nothing else.
- The `summary` and `error` fields go straight into the human's sheet, so write \
them in Russian, in caveman register (short broken phrases, present tense, \
primitive grammar) and без воды — terse, no filler. Keep technical tokens (spec \
ids, branch/file names) verbatim. This rule is ONLY about those two fields; \
everything else you do (code, commit messages, reasoning) stays normal.
"""


@dataclass
class AgentResult:
    outcome: str = "failed"
    spec_id: str = ""
    committed: bool = False
    pushed: bool = False
    deployed: bool = False
    tests_passed: bool = False
    summary: str = ""
    error: str = ""
    rate_limited: bool = False
    interrupted: bool = False   # killed by a signal (daemon restart) — not a real failure
    duration_s: float = 0.0
    raw_tail: str = field(default="", repr=False)


# Signals that we hit a usage limit rather than a real task failure.
_RATE_RE = re.compile(
    r"rate.?limit|overloaded|\b429\b|billing|insufficient.+credit|credit.+exhaust|quota.+exceed",
    re.I,
)


# --------------------------------------------------------------------------
# Live progress: a deterministic, side-effect-free tracker
# --------------------------------------------------------------------------
# The pipeline a task moves through, with rough effort weights. The percent ladder
# is the cumulative weight up to the agent's *terminal* stage, so it auto-scales:
# a spec-only run treats `spec` as the whole job; a `ship` run spreads across all
# six. The numbers are deliberately approximate — this is an estimate, not a
# measured fraction of work.
_PIPELINE: list[tuple[str, int]] = [
    ("spec", 3),
    ("implement", 8),
    ("tests", 3),
    ("commit", 1),
    ("push", 1),
    ("deploy", 2),
]
_STAGE_ORDER: list[str] = [name for name, _ in _PIPELINE]
_STAGE_WEIGHT: dict[str, int] = dict(_PIPELINE)
_STAGE_INDEX: dict[str, int] = {name: i for i, name in enumerate(_STAGE_ORDER)}

# Tool-call → stage classification patterns (matched case-insensitively).
_TEST_RE = re.compile(
    r"\b(pytest|py\.test|npm (run )?test|yarn test|go test|cargo test|tox|jest|vitest|unittest)\b",
    re.I,
)
# Anchored to a real deploy INVOCATION (`bash deploy/deploy.sh`, `./deploy.sh`, a
# `docker compose up/build/...`) — NOT a bare mention like `cat deploy.sh` or
# `git add deploy.sh`, which used to false-positive the deploy stage.
_DEPLOY_RE = re.compile(
    # A `/` boundary before `deploy.sh` so `predeploy.sh`/`redeploy.sh` don't match.
    r"(?:\bbash\s+|\bsh\s+|(?:^|[\s;&|(])\./)(?:\S*/)?deploy\.sh\b"
    r"|docker[ -]compose\s+(?:up|build|run|down|stop|restart)\b",
    re.I,
)
_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
_BASH_TOOLS = {"bash", "shell"}


@dataclass
class Progress:
    """A snapshot of where a running agent is. `pct` is an approximate estimate."""
    stage: str
    pct: int
    autonomy: str
    phase: str


def _classify_command(cmd: str) -> str | None:
    """Map a shell command to a pipeline stage, or None if it carries no signal.
    Ordered most-specific-first so `git push` beats `git commit` etc."""
    c = cmd.lower()
    if "git push" in c:
        return "push"
    if "git commit" in c:
        return "commit"
    if _DEPLOY_RE.search(c):
        return "deploy"
    if _TEST_RE.search(c):
        return "tests"
    if "openspec" in c:
        return "spec"
    return None


def _classify_path(path: str) -> str:
    """A file edit is `spec` work if it touches `openspec/`, else `implement`."""
    p = path.replace("\\", "/").lower()
    return "spec" if "openspec/" in p else "implement"


def _terminal_stage(autonomy: str, phase: str) -> str:
    """The last stage this run will reach, so the percent ladder scales to it."""
    if phase == "spec" or autonomy == "spec":
        return "spec"
    if autonomy == "code":
        return "commit"
    return "deploy"  # ship, or an approved `implement` phase


class ProgressTracker:
    """Derives a (stage, pct) estimate from the agent's tool-use events.

    Pure and I/O-free: it only digests events handed to it. Progress is monotonic
    — neither the stage index nor the percent ever decreases — so an out-of-order
    signal (e.g. a late spec edit during the test stage) can never roll it back.
    """

    def __init__(self, autonomy: str, phase: str):
        self.autonomy = autonomy
        self.phase = phase
        self._terminal_idx = _STAGE_INDEX[_terminal_stage(autonomy, phase)]
        # An approved `implement` run has already finished the spec → start there.
        self._floor_idx = _STAGE_INDEX["implement"] if phase == "implement" else 0
        self._idx = self._floor_idx
        self._calls_in_stage = 0
        self._pct = 0                       # set before _compute_pct reads it
        self._pct = self._compute_pct()

    def _compute_pct(self) -> int:
        total = sum(w for s, w in _PIPELINE if _STAGE_INDEX[s] <= self._terminal_idx)
        before = sum(w for s, w in _PIPELINE if _STAGE_INDEX[s] < self._idx)
        cur = _STAGE_WEIGHT[_STAGE_ORDER[self._idx]]
        # Intra-stage creep so a long stage still visibly advances; saturates so it
        # never spills into the next stage's band.
        frac = min(0.85, self._calls_in_stage / 6.0)
        pct = int(100.0 * (before + cur * frac) / total)
        pct = max(pct, self._pct)                    # monotonic
        return min(pct, 99)                          # never 100 until the run ends

    def observe(self, event: dict) -> bool:
        """Digest one stream event. Returns True if the snapshot changed."""
        if not isinstance(event, dict) or event.get("type") != "assistant":
            return False
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return False
        changed = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            stage = self._classify(block.get("name", ""), block.get("input") or {})
            if self._apply(stage):
                changed = True
        return changed

    def _classify(self, name: str, inp: dict) -> str | None:
        n = (name or "").lower()
        if n in _BASH_TOOLS:
            return _classify_command(str(inp.get("command", "")))
        if n in _EDIT_TOOLS:
            return _classify_path(str(inp.get("file_path") or inp.get("path") or ""))
        return None

    def _apply(self, stage: str | None) -> bool:
        prev_idx, prev_pct = self._idx, self._pct
        if stage is not None:
            target = max(self._floor_idx, min(_STAGE_INDEX[stage], self._terminal_idx))
            if target > self._idx:
                self._idx = target
                self._calls_in_stage = 0
        self._calls_in_stage += 1     # every tool call shows life within the stage
        self._pct = self._compute_pct()
        return self._idx != prev_idx or self._pct != prev_pct

    def snapshot(self) -> Progress:
        return Progress(stage=_STAGE_ORDER[self._idx], pct=self._pct,
                        autonomy=self.autonomy, phase=self.phase)


def format_progress(p: Progress) -> str:
    """A compact one-liner for the row's Log cell, e.g. `⏳ implement ~58% (ship)`.
    The mode token answers "is this agent autonomous?": `ship`/`code` finish on
    their own; `gated`/`spec` park for a human; an approved implement run ships."""
    mode = "approved→ship" if p.phase == "implement" else p.autonomy
    return f"⏳ {p.stage} ~{p.pct}% ({mode})"


ProgressCallback = Callable[[Progress], None]


# How to ship, reused by `ship`/`gated`-implement. Kept verbatim so the two paths
# give the agent identical deploy instructions.
_SHIP_STEP = (
    "implement it and run the test suite (must pass). Then ship it the way THIS "
    "repo expects — read CLAUDE.md/README for its branching + deploy workflow and "
    "follow it exactly so the code you just wrote is the code that gets deployed "
    "(don't deploy a stale branch). Commit, push to origin, and run the documented "
    "deploy (e.g. `bash deploy/deploy.sh`). Set committed/pushed/deployed honestly. "
    "Return outcome=\"implemented\"."
)


def _spec_phase_prompt(task: str, detail_block: str, init_block: str) -> str:
    """Phase 1 only: author + validate the OpenSpec change, then stop."""
    return f"""\
TASK (human-authored, the WHAT):
{task}{detail_block}

Author an OpenSpec change for this task — do NOT write implementation code yet.
{init_block}
1. SPEC. Create an OpenSpec change capturing this task.
   - Prefer the CLI: `openspec new change <slug>` (use `--json` if supported).
   - Author proposal.md, the spec deltas under specs/, and tasks.md following the
     conventions already present in `openspec/`.
   - Validate: `openspec validate <slug>` (or `openspec validate --all`).
   - Record the change id (the folder name under openspec/changes/) as `spec_id`.

Stopping rule: Stop after the spec is created and validated. Do NOT write
implementation code. Return outcome="spec_ready".

If anything blocks you irrecoverably (no openspec, no repo), return
outcome="blocked" or "failed" with a clear `error`. Always set `spec_id` once the
change folder exists, even on later failure.
"""


def _implement_phase_prompt(task: str, detail_block: str, spec_id: str) -> str:
    """Phase 2: a human approved an existing spec — implement it and ship."""
    return f"""\
TASK (human-authored, the WHAT):
{task}{detail_block}

An OpenSpec change `{spec_id}` ALREADY EXISTS for this task and has been approved
by a human. Do NOT create a new change. Read `openspec/changes/{spec_id}/`
(proposal.md, specs/, tasks.md) and:

1. IMPLEMENT it exactly as the approved spec describes, working through tasks.md.
2. SHIP: {_SHIP_STEP}
   Set `spec_id` to "{spec_id}".

If the change folder is missing or the spec is unimplementable, return
outcome="blocked" or "failed" with a clear `error`.
"""


def _full_prompt(task: str, detail_block: str, init_block: str, autonomy: str) -> str:
    stop = {
        "spec": "Stop after the spec is created and validated. Do NOT write "
                "implementation code. Return outcome=\"spec_ready\".",
        "code": "After the spec, implement it and run the test suite (must pass). "
                "Commit following the repo's branching convention (default: a new "
                "branch `agent/<change-id>`). Do NOT push or deploy. Return "
                "outcome=\"implemented\".",
        "ship": f"After the spec, {_SHIP_STEP}",
    }[autonomy]
    return f"""\
TASK (human-authored, the WHAT):
{task}{detail_block}

Do this, end to end, autonomously:
{init_block}
1. SPEC. Create an OpenSpec change capturing this task.
   - Prefer the CLI: `openspec new change <slug>` (use `--json` if supported).
   - Author proposal.md, the spec deltas under specs/, and tasks.md following the
     conventions already present in `openspec/`.
   - Validate: `openspec validate <slug>` (or `openspec validate --all`).
   - Record the change id (the folder name under openspec/changes/) as `spec_id`.

2. IMPLEMENT / SHIP (per the stopping rule below).

Stopping rule: {stop}

If anything blocks you irrecoverably (no openspec, no repo, missing deploy
script when shipping), return outcome="blocked" or "failed" with a clear `error`.
Always set `spec_id` once the change folder exists, even on later failure.
"""


def _task_prompt(task: str, detail: str, autonomy: str, allow_init: bool,
                 phase: str = "full", spec_id: str = "") -> str:
    detail_block = f"\n\nExtra context / acceptance criteria:\n{detail}" if detail.strip() else ""
    init_block = (
        "\n0. INIT. If `openspec/` is missing, run `openspec init` first "
        "(you are explicitly authorised to initialise it).\n"
        if allow_init else ""
    )
    if phase == "spec":
        return _spec_phase_prompt(task, detail_block, init_block)
    if phase == "implement":
        return _implement_phase_prompt(task, detail_block, spec_id)
    return _full_prompt(task, detail_block, init_block, autonomy)


def _build_cmd(cfg: C.Config, prompt: str) -> list[str]:
    cmd = [
        cfg.claude_bin, "-p", prompt,
        "--model", cfg.model,
        # stream-json (requires --verbose in -p mode) lets us read tool-use events
        # as they happen and derive live progress, instead of buffering one blob.
        "--output-format", "stream-json",
        "--verbose",
        "--json-schema", json.dumps(RESULT_SCHEMA),
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    if cfg.permission_mode == "acceptEdits":
        cmd += ["--permission-mode", "acceptEdits"]
    else:
        cmd += ["--dangerously-skip-permissions"]
    return cmd


def _structured_from_dict(env: dict) -> dict | None:
    """Pull our result schema out of one claude envelope dict (a `result` event or
    a plain `--output-format json` blob). Shared by the streamed and string paths."""
    if not isinstance(env, dict):
        return None
    if isinstance(env.get("structured_output"), dict):
        return env["structured_output"]
    # plain dict already matching our schema
    if "outcome" in env:
        return env
    # text result that itself is JSON
    res = env.get("result")
    if isinstance(res, str):
        try:
            inner = json.loads(res)
            if isinstance(inner, dict):
                return inner
        except json.JSONDecodeError:
            return None
    return None


def _last_json_object(s: str) -> dict | None:
    """Right-to-left scan for the last well-formed JSON object that carries our
    structured result. A greedy `{.*}` regex can never isolate a single object out of
    noisy multi-object output (it spans from the first `{` to the last `}`); instead
    we try `raw_decode` at each `{` from the end and return the last one that parses to
    a dict from which `_structured_from_dict` can extract a result."""
    # The structured result is always the terminal object, so only the tail can
    # carry it — bound the right-to-left scan so a multi-MB transcript stays O(tail).
    s = s[-65536:]
    dec = json.JSONDecoder()
    for m in reversed(list(re.finditer(r"\{", s))):
        try:
            obj, _ = dec.raw_decode(s, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            structured = _structured_from_dict(obj)
            if structured is not None:
                return structured
    return None


def _list_change_ids(repo_path: Path) -> set[str]:
    """Names of OpenSpec change folders currently on disk — active under
    `openspec/changes/<id>/` and archived under `openspec/changes/archive/<id>/`.

    The agent's self-reported `spec_id` is optional (not `required` in
    RESULT_SCHEMA) and routinely omitted, so the durable truth is the change
    folder the run created. We scan both locations because a `ship` run may archive
    the change it just authored. OSError-safe: a missing tree yields an empty set."""
    out: set[str] = set()
    base = repo_path / "openspec" / "changes"
    for d in (base, base / "archive"):
        try:
            for c in d.iterdir():
                if c.is_dir() and c.name != "archive":
                    out.add(c.name)
        except OSError:
            pass
    return out


def _spec_from_disk(repo_path: Path, before: set[str]) -> str:
    """The single OpenSpec change id that appeared during the run, or `""` when
    none (or ambiguously several) appeared — the caller then keeps its own value.
    Used on the partial/failed return paths where there is no structured result."""
    new = sorted(_list_change_ids(repo_path) - before)
    return new[0] if len(new) == 1 else ""


def _resolve_spec_id(repo_path: Path, before: set[str], reported: str,
                     phase: str, given: str) -> str:
    """Filesystem-first resolution of the run's OpenSpec change id.

    The agent's `reported` id is advisory; the change folder created during the run
    is authoritative. Order:
      1. `implement` phase — the approved id was handed in and the agent must not
         create a new change; keep it verbatim.
      2. exactly one new change folder appeared -> that's the id.
      3. the reported id, when it names a folder that actually exists on disk.
      4. several new folders, reported among them -> the reported one.
      5. several new folders, none reported -> the lexically-first (deterministic).
      6. nothing new on disk -> fall back to the reported id (may be empty)."""
    reported = (reported or "").strip()
    if phase == "implement" and given.strip():
        return given.strip()
    after = _list_change_ids(repo_path)
    new = sorted(after - before)
    if len(new) == 1:
        return new[0]
    if reported and reported in after:
        return reported
    if reported in new:
        return reported
    if new:
        return new[0]
    return reported


def _parse(stdout: str) -> dict | None:
    """Extract the structured object from a single-blob JSON envelope, falling back to
    the last well-formed JSON object in noisier output."""
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return _last_json_object(stdout)
    return _structured_from_dict(env)


def _result_from_obj(obj: dict, repo_path: Path, before: set[str], phase: str,
                     given_spec: str, raw: str, dur: float) -> "AgentResult":
    """Build the AgentResult from a parsed structured object. Shared by the normal
    completion path and the timeout-salvage path so they can't drift."""
    return AgentResult(
        outcome=obj.get("outcome", "failed"),
        spec_id=_resolve_spec_id(repo_path, before, obj.get("spec_id", ""), phase, given_spec),
        committed=bool(obj.get("committed", False)),
        pushed=bool(obj.get("pushed", False)),
        deployed=bool(obj.get("deployed", False)),
        tests_passed=bool(obj.get("tests_passed", False)),
        summary=obj.get("summary", ""),
        error=obj.get("error", ""),
        rate_limited=bool(_RATE_RE.search(raw)) and obj.get("outcome") in {"failed", "blocked"},
        duration_s=dur,
        raw_tail=raw[-4000:],
    )


def _parse_event(line: str) -> dict | None:
    """Parse one stream-json line into an event dict; None for blank/non-JSON noise."""
    line = line.strip()
    if not line or line[0] not in "{[":
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Best-effort hard kill of the agent and any children it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _bash_bound_env(cfg) -> dict:
    """The child env for a dispatched agent: the inherited env plus the Layer-1
    bash tool-call bounds, so a single bash call cannot run unbounded (the common
    hang shape — an `until … grep` verify loop the agent expected to finish fast)."""
    return {
        **os.environ,
        "BASH_DEFAULT_TIMEOUT_MS": str(cfg.bash_default_timeout_ms),
        "BASH_MAX_TIMEOUT_MS": str(cfg.bash_max_timeout_ms),
    }


def _start_reader(cmd: list[str], cwd: Path, env: dict | None = None) -> tuple[subprocess.Popen, "queue.Queue[str | None]", threading.Thread]:
    """Spawn `cmd` in its OWN process group (so a timeout can hard-kill it and every
    child it spawned — git, pytest, deploy) and drain its stdout into a queue via a
    daemon reader thread. The queue lets the caller enforce a hard timeout even while
    the child is silent; a `None` sentinel is queued when the stream closes. `env`
    overrides the child environment (None inherits ours). Raises FileNotFoundError if
    the binary is missing — the caller maps that to a result. Shared by `run()` and
    `chat()` so the subprocess scaffold lives in one place."""
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True, env=env,
    )
    q: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line)
        except Exception:  # noqa: BLE001 — reader errors surface as EOF
            pass
        finally:
            q.put(None)  # sentinel: stream closed

    th = threading.Thread(target=_reader, name="agent-reader", daemon=True)
    th.start()
    return proc, q, th


_RAW_SEQ = itertools.count()  # process-wide tiebreaker so same-second dumps never collide


def _prune_runs(runs_dir: Path, keep: int) -> None:
    """Bound the forensic dump directory to the newest `keep` files; delete the rest,
    oldest-first by mtime. These dumps are forensics, never authority (the durable
    state is the SHEET), so it is always safe to drop the oldest — unbounded they fill
    the host disk. `keep <= 0` disables pruning. Best-effort and never raises: a racing
    concurrent agent may unlink the same file first (FileNotFoundError) and the whole
    pass is OSError-guarded, because forensics must never crash a real run."""
    if keep <= 0:
        return
    try:
        # Stamp each file with its mtime, skipping any that a racing agent unlinked
        # between the glob and the stat — one vanished file must not abort the whole
        # prune (the directory would then drift over cap until the next dump).
        stamped: list[tuple[float, Path]] = []
        for p in runs_dir.glob("*.json"):
            try:
                stamped.append((p.stat().st_mtime, p))
            except OSError:
                continue
        if len(stamped) <= keep:
            return
        stamped.sort(key=lambda t: t[0])               # oldest first
        for _, p in stamped[:len(stamped) - keep]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _persist_raw(runs_dir: Path, name: str, content: str, keep: int = 0) -> None:
    """Dump an agent's raw output for forensics. The filename is wall-clock time +
    pid + a monotonic sequence number: human-orderable and collision-free even when
    two concurrent agents (e.g. parallel chats on one repo) finish in the same second
    — `time.strftime` is only second-granular and the pid is identical across the
    daemon's agents, so the sequence is what guarantees uniqueness. (`time.monotonic()`,
    used for durations, is an arbitrary process-relative float — wrong for filenames.)

    After writing, prune the directory to the newest `keep` dumps (0 = keep all)."""
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (runs_dir / f"{name}-{stamp}-{os.getpid()}-{next(_RAW_SEQ)}.json").write_text(content)
    except OSError:
        pass
    _prune_runs(runs_dir, keep)


def run(cfg: C.Config, repo_path: Path, task: str, detail: str,
        allow_init: bool = False, phase: str = "full", spec_id: str = "",
        on_progress: ProgressCallback | None = None) -> AgentResult:
    prompt = _task_prompt(task, detail, cfg.autonomy, allow_init, phase, spec_id)
    cmd = _build_cmd(cfg, prompt)
    log.info("dispatching agent in %s (model=%s, autonomy=%s, phase=%s, timeout=%ss)",
             repo_path, cfg.model, cfg.autonomy, phase, cfg.agent_timeout)
    runs_dir = Path(cfg.state_dir) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot the existing OpenSpec change folders so we can deterministically
    # detect the one this run creates — the agent's self-reported spec_id is
    # optional and routinely omitted, leaving the sheet's Spec column blank.
    changes_before = _list_change_ids(repo_path)

    tracker = ProgressTracker(cfg.autonomy, phase)

    def _emit() -> None:
        if on_progress is None:
            return
        try:
            on_progress(tracker.snapshot())
        except Exception:  # noqa: BLE001 — a progress sink can never break the run
            log.debug("on_progress callback raised", exc_info=True)

    _emit()  # initial snapshot so the row shows a stage immediately

    start = time.monotonic()
    try:
        proc, q, th = _start_reader(cmd, repo_path, env=_bash_bound_env(cfg))
    except FileNotFoundError:
        return AgentResult(outcome="failed",
                           error=f"claude binary not found: {cfg.claude_bin}",
                           summary="claude not found")

    lines: list[str] = []
    result_env: dict | None = None
    timed_out = False
    stalled = False
    last_line_at = time.monotonic()  # stall watchdog: when did the stream last speak?
    while True:
        remaining = cfg.agent_timeout - (time.monotonic() - start)
        if remaining <= 0:
            timed_out = True
            break
        # Stall watchdog (Layer 2): a silent stream is the real shape of a hang
        # (an unbounded bash, or a wedged child holding the pipe so claude blocks).
        # MUST be ≥ the max single bash, which is silent until it returns.
        if time.monotonic() - last_line_at > cfg.agent_stall_timeout:
            stalled = True
            break
        try:
            line = q.get(timeout=min(remaining, 2.0))
        except queue.Empty:
            continue  # re-check the deadline / stall window
        if line is None:
            break  # stream closed
        last_line_at = time.monotonic()
        lines.append(line)
        evt = _parse_event(line)
        if evt is None:
            continue
        if evt.get("type") == "result":
            result_env = evt
        elif tracker.observe(evt):
            _emit()

    raw = "".join(lines)
    if timed_out or stalled:
        _kill_process_group(proc)
        th.join(timeout=2)   # symmetry with the normal path: don't leak the reader
        dur = time.monotonic() - start
        _persist_raw(runs_dir, repo_path.name, raw, cfg.runs_keep)
        # The agent may have already emitted its terminal `result` event and only
        # the process lingered (a slow child holding stdout open past the deadline).
        # Salvage that structured result rather than recording a shipped run as a
        # timeout/stall failure (which would burn a retry and re-dispatch done work).
        obj = _structured_from_dict(result_env) if result_env else None
        if obj is not None:
            reason = "stalled" if stalled else f"the {cfg.agent_timeout}s deadline"
            log.warning("agent hit %s but had already emitted a result in %s — "
                        "salvaging it", reason, repo_path)
            return _result_from_obj(obj, repo_path, changes_before, phase, spec_id, raw, dur)
        if stalled:
            log.error("agent stalled (no stream output for %ss) in %s — killed",
                      cfg.agent_stall_timeout, repo_path)
            return AgentResult(
                outcome="failed",
                error=f"agent stalled — no output for {cfg.agent_stall_timeout}s (watchdog killed it)",
                summary="завис, нет вывода — watchdog убил",
                duration_s=dur, raw_tail=raw[-2000:],
                spec_id=_spec_from_disk(repo_path, changes_before) or spec_id)
        log.error("agent timed out after %.0fs in %s", dur, repo_path)
        return AgentResult(outcome="failed",
                           error=f"agent timed out after {cfg.agent_timeout}s",
                           summary="timed out", duration_s=dur, raw_tail=raw[-2000:],
                           spec_id=_spec_from_disk(repo_path, changes_before) or spec_id)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
    th.join(timeout=2)

    dur = time.monotonic() - start
    _persist_raw(runs_dir, repo_path.name, raw, cfg.runs_keep)
    tail = raw[-4000:]
    rate_limited = bool(_RATE_RE.search(raw))

    obj = _structured_from_dict(result_env) if result_env else None
    if obj is None:
        obj = _parse(raw)
    if obj is None:
        # A signal-killed agent (returncode -15/-9/-2, or the 128+sig form 143/137/130)
        # produced no JSON because it was INTERRUPTED — almost always the daemon being
        # restarted for a deploy, which SIGTERMs the whole cgroup incl. the `claude`
        # child. That is NOT a task failure: flag it so the orchestrator requeues the
        # row (and doesn't burn a retry) instead of marking it permanently `failed`.
        rc = proc.returncode
        interrupted = rc is not None and (rc < 0 or rc in (143, 137, 130))
        if interrupted:
            log.warning("agent interrupted by signal (exit=%s) in %s — requeue, not fail",
                        rc, repo_path)
            return AgentResult(outcome="failed", interrupted=True,
                               error=f"interrupted (exit={rc})",
                               summary="interrupted by daemon restart — requeued",
                               duration_s=dur, raw_tail=tail)
        err = raw.strip()[-400:] or "no structured output from agent"
        log.error("agent produced no parseable result (exit=%s, rate_limited=%s): %s",
                  rc, rate_limited, err)
        return AgentResult(outcome="failed", error=err,
                           summary="agent returned no structured result",
                           rate_limited=rate_limited, duration_s=dur, raw_tail=tail,
                           spec_id=_spec_from_disk(repo_path, changes_before) or spec_id)

    res = _result_from_obj(obj, repo_path, changes_before, phase, spec_id, raw, dur)
    log.info("agent finished in %.0fs -> outcome=%s spec=%s",
             dur, res.outcome, res.spec_id or "-")
    return res


# --------------------------------------------------------------------------
# Read-only chat agent — discuss the repo, never change it
# --------------------------------------------------------------------------
# A separate, deliberately weaker agent for the sheet's chat column. It answers
# questions ABOUT the repo and must never mutate it: read-only is enforced at the
# TOOL level (allow only Read/Grep/Glob, explicitly deny the mutators) — NOT by
# trusting the prompt, and NOT with --dangerously-skip-permissions (which would
# defeat the whole restriction). This is why it cannot reuse `run()`'s command.
CHAT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string",
                   "description": "Your reply to the human, in their language"},
    },
    "required": ["answer"],
}

CHAT_SYSTEM_PROMPT = """\
You are a READ-ONLY engineering assistant embedded in a Google-Sheet control plane. \
A human is discussing ONE git repository with you — asking questions, thinking out \
loud, exploring design — WITHOUT changing any code.

Hard rules:
- You can ONLY read and search the repository (Read/Grep/Glob). You have NO ability \
to edit, write, run shell commands, commit, push, or deploy — and you must not ask \
for it or pretend you did. Discuss, explain, propose — never act.
- Ground your answers in the actual code: cite concrete file paths (and line numbers \
where useful) rather than guessing.
- Reply in the SAME language the human used.
- Be concise and concrete — your answer lands in a single spreadsheet cell, so use a \
few short paragraphs at most and avoid long verbatim code dumps.
- Your FINAL message must be the required structured object (just `answer`) and \
nothing else.
"""


@dataclass
class ChatResult:
    answer: str = ""
    error: str = ""
    rate_limited: bool = False
    interrupted: bool = False
    duration_s: float = 0.0
    raw_tail: str = field(default="", repr=False)


def _chat_prompt(question: str, history: list[tuple[str, str]]) -> str:
    """Build the chat prompt. The full transcript is replayed each turn because the
    `claude -p` process is stateless — the sheet IS the conversation memory."""
    turns = ""
    for q, a in history:
        turns += f"\n[Human] {q}\n[Assistant] {a}\n"
    history_block = (
        f"\n\nConversation so far (oldest first — continue it naturally):{turns}"
        if history else ""
    )
    return f"""\
You are discussing this repository with a human via a chat column in a Google Sheet.\
{history_block}

The human's new message:
{question}

Read and search the repository as needed, then answer. Return the structured object \
{{"answer": "..."}}."""


def _build_chat_cmd(cfg: C.Config, prompt: str) -> list[str]:
    return [
        cfg.claude_bin, "-p", prompt,
        "--model", cfg.chat_model,   # cheaper tier — read-only Q&A, not code delivery
        "--output-format", "json",
        "--json-schema", json.dumps(CHAT_RESULT_SCHEMA),
        "--append-system-prompt", CHAT_SYSTEM_PROMPT,
        # READ-ONLY enforcement (tool level, no permission bypass):
        "--allowedTools", "Read,Grep,Glob",
        "--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit,Bash",
        "--permission-mode", "default",
    ]


def chat(cfg: C.Config, repo_path: Path, question: str,
         history: list[tuple[str, str]] | None = None) -> ChatResult:
    """Run one read-only chat turn against `repo_path` and return the answer.

    Mirrors `run()`'s subprocess plumbing (own process group, hard timeout via a
    reader thread) but with a single-blob JSON output and the read-only tool set.
    Never raises for an agent-level problem — failures come back on the result."""
    prompt = _chat_prompt(question, history or [])
    cmd = _build_chat_cmd(cfg, prompt)
    log.info("dispatching CHAT agent in %s (model=%s, timeout=%ss)",
             repo_path, cfg.chat_model, cfg.chat_timeout)
    runs_dir = Path(cfg.state_dir) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    try:
        proc, q, th = _start_reader(cmd, repo_path, env=_bash_bound_env(cfg))
    except FileNotFoundError:
        return ChatResult(error=f"claude binary not found: {cfg.claude_bin}")

    lines: list[str] = []
    timed_out = False
    stalled = False
    last_line_at = time.monotonic()
    while True:
        remaining = cfg.chat_timeout - (time.monotonic() - start)
        if remaining <= 0:
            timed_out = True
            break
        if time.monotonic() - last_line_at > cfg.chat_stall_timeout:
            stalled = True
            break
        try:
            line = q.get(timeout=min(remaining, 2.0))
        except queue.Empty:
            continue
        if line is None:
            break
        last_line_at = time.monotonic()
        lines.append(line)

    raw = "".join(lines)
    dur = time.monotonic() - start
    if timed_out or stalled:
        _kill_process_group(proc)
        th.join(timeout=2)
        if stalled:
            log.error("chat agent stalled (no output for %ss) in %s — killed",
                      cfg.chat_stall_timeout, repo_path)
        else:
            log.error("chat agent timed out after %.0fs in %s", dur, repo_path)
        _persist_raw(runs_dir, f"{repo_path.name}-chat", raw, cfg.runs_keep)
        msg = (f"stalled — no output for {cfg.chat_stall_timeout}s" if stalled
               else f"timed out after {cfg.chat_timeout}s")
        return ChatResult(error=msg, duration_s=dur, raw_tail=raw[-2000:])

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
    th.join(timeout=2)
    _persist_raw(runs_dir, f"{repo_path.name}-chat", raw, cfg.runs_keep)

    rate_limited = bool(_RATE_RE.search(raw))
    obj = _parse(raw)
    if obj is None or not isinstance(obj.get("answer"), str):
        rc = proc.returncode
        interrupted = rc is not None and (rc < 0 or rc in (143, 137, 130))
        if interrupted:
            return ChatResult(interrupted=True, error=f"interrupted (exit={rc})",
                              duration_s=dur, raw_tail=raw[-2000:])
        err = raw.strip()[-400:] or "no structured answer from chat agent"
        log.error("chat agent produced no parseable answer (exit=%s): %s", rc, err)
        return ChatResult(error=err, rate_limited=rate_limited,
                          duration_s=dur, raw_tail=raw[-2000:])

    log.info("chat agent finished in %.0fs (%d chars)", dur, len(obj["answer"]))
    return ChatResult(answer=obj["answer"], duration_s=dur, raw_tail=raw[-2000:])
