"""The supervisor loop.

Design goals (in priority order): never die, never lose a task, never double-ship.
- Every cycle and every task is wrapped so one failure can't kill the daemon.
- The sheet itself is the durable state — restart-safe by construction.
- A single-instance file lock prevents two daemons fighting over the sheet.
- Rows stuck in `working` (a crash mid-run) are reclaimed after a grace period.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Callable

from . import agent
from . import config as C
from . import repo as repolib
from .log import log
from . import sheets
from .sheets import ControlRow, Friend, Tab, TaskRow, make_backend

_STOP = False

# Hard cap (seconds) on the inline create_repo subprocess so a rare admin op can't
# stall the poll loop for the full agent timeout. See `_h_create_repo`.
_CREATE_REPO_TIMEOUT = 300

# --------------------------------------------------------------------------
# Control intent dispatcher — action -> handler registry
# --------------------------------------------------------------------------
# A handler receives (orchestrator, ControlRow, parsed_args_dict) and returns a
# short result string written to column F. Raising is caught by the dispatcher,
# so a buggy handler can never kill the supervisor. The real handlers
# (add_repo / create_repo, plus the async run_skill path) are registered by later
# stages; Stage 1 ships only the framework with an empty registry.
ControlHandler = Callable[["Orchestrator", ControlRow, dict], str]
CONTROL_HANDLERS: dict[str, ControlHandler] = {}


def register_control_handler(action: str, fn: ControlHandler) -> None:
    """Register (or replace) the handler for a control action."""
    CONTROL_HANDLERS[action] = fn


def _handle_term(signum, _frame):
    global _STOP
    log.info("received signal %s — will stop after current task", signum)
    _STOP = True


try:
    # Human-facing display timezone for the Updated (E) cell + heartbeat. Default
    # Europe/Moscow (the operator's locale); override with DISPLAY_TZ. A bad name /
    # missing tzdata falls back to UTC so the daemon can never fail to start on this.
    _DISPLAY_TZ: object = ZoneInfo(os.getenv("DISPLAY_TZ", "Europe/Moscow"))
except Exception:  # noqa: BLE001
    _DISPLAY_TZ = timezone.utc


def _now() -> str:
    """Timestamp for the Updated cell / heartbeat, in the display timezone (Moscow by
    default). The UTC offset is ENCODED (e.g. `+03:00`) — never a bare `Z` on a
    non-UTC clock — so `_age_seconds` stays timezone-correct (no 3 h skew that would
    break stale-reclaim)."""
    return datetime.now(_DISPLAY_TZ).isoformat(sep=" ", timespec="seconds")


def _age_seconds(ts: str) -> float:
    """Seconds since `ts`. Accepts the daemon's own offset-bearing stamp written by
    `_now()` (``"YYYY-MM-DD HH:MM:SS+03:00"`` — space separator, explicit UTC offset,
    NOT a bare ``Z``) and the Apps-Script ISO-8601 timestamps in ``_control``
    (``...T...`` with fractional seconds / offsets). A naive (offset-less) stamp is
    assumed UTC. Unparseable / empty → inf, so a malformed row is treated as stale and
    reclaimed rather than stuck forever."""
    try:
        dt = datetime.fromisoformat(ts.strip())
    except (ValueError, AttributeError, TypeError):
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


# --------------------------------------------------------------------------
# Friend-sheet policy model (pure helpers — shared by the share flow now and the
# Stage-2 multi-sheet poll loop). Kept side-effect-free so they are trivially
# unit-tested and can be reused without an Orchestrator.
# Autonomy levels live in config (the single source of truth): `C.AUTONOMY_LEVELS`.
# --------------------------------------------------------------------------


def friend_repo_allowed(friend: Friend, binding: str) -> bool:
    """True iff `binding` is in `friend`'s repo allowlist. Matched on the exact
    binding string AND on the bare last path segment (so an allowlist entry of a
    repo NAME admits its full-path binding and vice-versa). An empty binding or an
    empty allowlist is never allowed — a friend sheet defaults to deny."""
    binding = (binding or "").strip()
    if not binding:
        return False
    keys: set[str] = set()
    for r in friend.repos:
        r = r.strip()
        if r:
            keys.add(r)
            keys.add(sheets._last_segment(r))
    return binding in keys or sheets._last_segment(binding) in keys


def friend_autonomy(friend: Friend, cfg: C.Config) -> str:
    """A friend file's effective autonomy: its own level when valid, else
    `FRIEND_DEFAULT_AUTONOMY` (which itself defaults to `gated`)."""
    a = (friend.autonomy or "").strip().lower()
    if a in C.AUTONOMY_LEVELS:
        return a
    return cfg.friend_default_autonomy


# Control actions a friend (partner) file may NEVER drive: anything that creates a
# repo or mints/shares a new file. A friend file ships with NO bound Apps Script and
# NO `_control` tab, so this guard is purely defensive — if a partner hand-adds a
# `_control` tab, these intents are rejected (`error`) and every other action is
# ignored (left pending, never dispatched), so partners can never create repos.
FRIEND_FORBIDDEN_CONTROL = frozenset({"add_repo", "create_repo", "share_repos"})


class SingleInstance:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("another sheet-agent instance is already running")
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        except OSError:
            pass


@dataclass
class SheetCtx:
    """Everything the cycle needs to operate ONE sheet (the master or a friend file).
    Stage 2 polls many sheets per cycle; the per-sheet backend + policy travels in
    this context so a friend-sheet task's write-back lands on the friend sheet (never
    the master) and is gated by the friend file's own autonomy + repo allowlist.

    `friend` is None for the master sheet and the `Friend` registry record for a
    friend file. `label` keys the per-(sheet,tab) heartbeat throttle and prefixes
    logs; it is stable across cycles (the master's "master", a friend's sheet id) so
    the throttle survives the friend backend being rebuilt each cycle."""
    backend: object
    autonomy: str
    label: str
    friend: object | None = None      # sheets.Friend | None

    @property
    def is_friend(self) -> bool:
        return self.friend is not None


@dataclass
class WorkItem:
    """One ready-to-dispatch unit, resolved during the (sequential) collect phase
    so the (parallel) dispatch phase only shells out to the agent."""
    title: str
    repo_path: Path
    allow_init: bool
    task: TaskRow
    phase: str          # "full" | "spec" | "implement"
    spec_id: str        # existing change id when phase == "implement"
    next_tries: int     # attempt number to stamp on dispatch
    # Effective agent prompt + extra context. For an ordinary row these are the task
    # text and "". For a `/<скилл>` row they are the catalog skill's prompt and the
    # trailing free text — so the daemon runs the skill without ever writing column A.
    prompt: str = ""
    detail: str = ""
    # The originating sheet's context: status/log write-backs go to `ctx.backend` and
    # the gated/ship decision uses `ctx.autonomy`. None means the master sheet.
    ctx: SheetCtx | None = None


@dataclass
class ChatWork:
    """One ready-to-run read-only chat turn. The compose box has already been
    consumed and the question echoed into the transcript at `answer_row` (under a
    'thinking' placeholder) during the single-threaded collect phase, so running it
    only shells out to the read-only agent and writes the reply back."""
    title: str
    repo_path: Path
    question: str
    history: list[tuple[str, str]]
    answer_row: int       # transcript row to write the reply (K) into
    # Originating sheet: the reply + pin are written back to this backend, never the
    # master. None means the master sheet. `label` keys the heartbeat throttle.
    ctx: SheetCtx | None = None


@dataclass
class SkillRun:
    """One ready-to-run catalog-skill dispatch. Materialised from a `run_skill`
    `_control` intent: the skill's prompt becomes the agent task, and the run reports
    status/result back into the control row (E/F) — NOT a repo task row, so the
    'daemon never writes a human A/G cell' invariant holds."""
    control_row: int      # `_control` row to report status/result into
    skill: str            # skill name (for logging / the result note)
    tab_title: str        # the bound repo tab (for the heartbeat cell E1)
    repo_path: Path
    task: str             # the skill's prompt (the agent's task)
    detail: str = ""      # optional free-text extra context from the human


class Orchestrator:
    def __init__(self, cfg: C.Config):
        self.cfg = cfg
        self.backend = make_backend(cfg)
        self._last_hb: dict[tuple[str, str], tuple[str, float]] = {}
        self._backoff = False  # set when an agent reports rate-limit/billing
        # Serialises ALL backend access: gspread/MockBackend are not thread-safe
        # and agents now run in parallel across repos.
        self._lock = threading.Lock()
        # Agents run in a PERSISTENT background pool so the poll loop keeps running
        # (and keeps dispatching fast `_control` button intents) while long repo-task
        # agents are mid-flight. `_inflight` tracks repos with a running/queued group
        # so the same repo is never dispatched twice (preserves "serial within one
        # repo" across cycles); the sheet `working` status guards individual rows.
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, self.cfg.max_concurrent_agents),
            thread_name_prefix="agent")
        self._inflight: set[Path] = set()
        # Read-only chat turns run in their OWN pool so a quick question is never
        # blocked behind a long implement-and-ship task agent (chat must stay snappy).
        # No per-repo in-flight guard is needed: chat is read-only (no git races) and
        # the compose box is cleared synchronously at collect, so a turn is never
        # double-dispatched.
        self._chat_pool = ThreadPoolExecutor(
            max_workers=max(1, self.cfg.max_concurrent_chats),
            thread_name_prefix="chat")
        # Per-cycle map (rebuilt at the top of run_once). The chat now lives on a
        # separate paired chat tab; this links a repo binding to its chat tab within one
        # cycle.
        self._chat_pairs: dict[str, str] = {}     # repo binding -> chat tab title
        # Signature of the last discovered repo set published to `_repos`. Lets the
        # per-cycle refresh skip the Sheets write when discovery is unchanged.
        self._last_repos_sig: tuple | None = None
        # Per-cycle cache (rebuilt at the top of run_once) of the `_skills` catalog by
        # name, lazily filled the first time a `/<скилл>` task is seen in the cycle so an
        # ordinary cycle never pays an extra Sheets read.
        self._skill_map_cache: dict | None = None
        # Monotonic time each async `_control` row entered `working`, keyed by sheet
        # row. The reclaim ages a long-running async skill off THIS, not the human
        # click time in column B (which the daemon never advances). Lost on restart by
        # design: after a restart the pool is empty, so a `working` row really is stale
        # and must be reclaimed — the click-time fallback handles exactly that case.
        self._ctl_working_since: dict[int, float] = {}
        # Stage 2: one backend per registered friend sheet, cached across cycles
        # (rebuilding a GoogleBackend re-opens the spreadsheet — a wasted read). Keyed
        # by sheet id; a backend that errors (lost access) is dropped and rebuilt next
        # cycle. `make_friend_backend` is overridable in tests via this map.
        self._friend_backends: dict[str, object] = {}

    def _refresh_repos(self) -> None:
        """Keep the `_repos` reference tab (the B1 add-repo dropdown source) in sync
        with live discovery, so a repo deleted from disk or pruned via `REPO_IGNORE`
        disappears from the dropdown WITHOUT a manual `repos`/`bootstrap` CLI run.

        Quota-friendly: rewrite `_repos` only when the discovered set changed since the
        last cycle. Per-tab dropdowns reference the `_repos!$A$2:$A` range, so
        refreshing the tab updates every dropdown — no per-tab rewrite needed. Caller
        wraps this best-effort; a discovery/write hiccup must never block the cycle."""
        repos = repolib.discover(self.cfg)
        sig = tuple(sorted((r.name, str(r.path), r.has_openspec) for r in repos))
        if sig == self._last_repos_sig:
            return
        with self._lock:
            self.backend.ensure_repos_tab(repos)
        self._last_repos_sig = sig
        log.info("refreshed %s: %d repo(s)", C.REPOS_TAB, len(repos))

    # Throttled heartbeat: write only on state change or every ~2 min, so we
    # don't burn Sheets write-quota on an idle "still watching" message.
    def _hb(self, title: str, text: str, *, ctx: "SheetCtx | None" = None) -> None:
        backend = ctx.backend if ctx is not None else self.backend
        label = ctx.label if ctx is not None else "master"
        key = (label, title)  # per-sheet so friend tabs never collide with the master
        with self._lock:
            prev = self._last_hb.get(key)
            now = time.monotonic()
            if prev and prev[0] == text and (now - prev[1]) < 120:
                return
            self._last_hb[key] = (text, now)
            try:
                backend.heartbeat(title, f"{text} @ {_now()}")
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed (%s/%s): %s", label, title, e)

    # -- sheet write helpers -------------------------------------------------
    def _set(self, title: str, row: int, *, ctx: "SheetCtx | None" = None,
             **cols: str) -> None:
        backend = ctx.backend if ctx is not None else self.backend
        mapping = {
            "spec": C.COL_SPEC, "status": C.COL_STATUS,
            "updated": C.COL_UPDATED, "log": C.COL_LOG, "tries": C.COL_TRIES,
        }
        with self._lock:
            for key, val in cols.items():
                try:
                    backend.write_cell(title, row, mapping[key], val)
                except Exception as e:  # noqa: BLE001 — never let a write kill the loop
                    log.warning("write_cell failed (%s r%s %s): %s", title, row, key, e)

    # -- chat (read-only Q&A on the paired `_chat <repo>` tab, cols A/B) ------
    def _chat_write(self, title: str, cells: list[tuple[int, int, str]],
                    *, ctx: "SheetCtx | None" = None) -> None:
        """Write a batch of chat cells (A/B only) in ONE API call. The chat always
        touches a small fixed set together (echo+thinking+pin+clear, or answer+pin),
        so batching is both cheaper and atomic. A failed write can never kill the loop."""
        backend = ctx.backend if ctx is not None else self.backend
        with self._lock:
            try:
                backend.write_cells(title, cells)
            except Exception as e:  # noqa: BLE001
                log.warning("chat write_cells failed (%s, %d cells): %s", title, len(cells), e)

    def _maybe_chat(self, title: str, tab: Tab, repo_path: Path,
                    ctx: "SheetCtx | None" = None) -> None:
        """If the chat tab's compose box (A2) holds a question, consume it and dispatch a
        read-only chat turn. Runs in the single-threaded collect phase: it echoes the
        question into the transcript under a 'thinking' marker, pins that marker to
        B2, and resets A2 to the placeholder — all synchronously — so the next cycle
        can't re-dispatch the same question. The agent itself runs async in `_chat_pool`."""
        q = tab.chat_input.strip()
        # The visible placeholder is the "empty" state — never dispatch it as a question.
        if not q or q == C.CHAT_INPUT_PLACEHOLDER:
            return
        # Next transcript row sits just below the last existing turn (A4/B4 down).
        next_row = max((t.row for t in tab.chat_turns), default=C.CHAT_FIRST_ROW - 1) + 1
        history = [(t.question, t.answer) for t in tab.chat_turns if t.question or t.answer]
        # One atomic batch: echo the question + a thinking marker into the transcript,
        # pin the marker to B2, and CONSUME the compose box A2 by resetting it to the
        # visible placeholder (not blank, so the "ask here" cue is always present).
        # Atomicity means the box is never reset without the question being recorded.
        self._chat_write(title, [
            (next_row, C.COL_CHAT_Q, q),
            (next_row, C.COL_CHAT_A, C.CHAT_THINKING),
            (C.CHAT_PINNED_ROW, C.CHAT_PINNED_COL, C.CHAT_THINKING),
            (C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL, C.CHAT_INPUT_PLACEHOLDER),
        ], ctx=ctx)
        work = ChatWork(title=title, repo_path=repo_path, question=q,
                        history=history, answer_row=next_row, ctx=ctx)
        try:
            self._chat_pool.submit(self._run_chat, work)
        except RuntimeError:  # pool shutting down (SIGTERM) — leave the marker, retry next boot
            log.info("[%s chat] pool shutting down; deferring r%s", title, next_row)

    def _run_chat(self, w: ChatWork) -> None:
        """Run one read-only chat turn in a pool thread and write the reply into
        B{answer_row} + the pinned B2. Fully exception-wrapped — a chat can never
        crash the daemon. The question stays echoed in A{answer_row} regardless, so a
        rate-limit/restart marker still lets the human re-ask by copying it."""
        log.info("[%s chat r%s] %s", w.title, w.answer_row, w.question[:80])
        self._hb(w.title, f"chat r{w.answer_row}", ctx=w.ctx)
        try:
            res = agent.chat(self.cfg, w.repo_path, w.question, w.history)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s chat r%s] crashed", w.title, w.answer_row)
            self._write_chat_answer(w, f"⚠️ ошибка: {e}")
            return
        if res.rate_limited:
            with self._lock:
                self._backoff = True
            answer = "⏳ rate-limited; спроси ещё раз чуть позже"
        elif res.interrupted:
            answer = "⏳ прервано рестартом демона; спроси ещё раз"
        elif res.answer:
            answer = res.answer
        else:
            answer = f"⚠️ {res.error or 'пустой ответ'}"
        self._write_chat_answer(w, answer)
        log.info("[%s chat r%s] answered (%d chars)", w.title, w.answer_row, len(answer))

    def _write_chat_answer(self, w: ChatWork, answer: str) -> None:
        answer = answer[:C.CHAT_MAX_ANSWER]
        # Transcript reply + pinned B2 in one atomic batch write.
        self._chat_write(w.title, [
            (w.answer_row, C.COL_CHAT_A, answer),
            (C.CHAT_PINNED_ROW, C.CHAT_PINNED_COL, answer),
        ], ctx=w.ctx)

    # -- paired chat tab (one per repo, matched by B1 binding) ----------------
    def _find_chat_tab(self, binding: str,
                       ctx: "SheetCtx | None" = None) -> str | None:
        """Title of the chat tab (`_chat …`) whose B1 binding equals `binding`, else
        None. Pairing is by binding, NEVER by parsing the title. A single unreadable
        chat tab can never break the scan — it is just skipped. Scoped to `ctx`'s
        backend so a friend sheet pairs against its OWN chat tabs."""
        backend = ctx.backend if ctx is not None else self.backend
        for title in backend.list_tab_titles():
            if not title.startswith(C.CHAT_TAB_PREFIX):
                continue
            try:
                ct = backend.read_chat_tab(title)
            except Exception:  # noqa: BLE001 — one bad tab must not break pairing
                continue
            if ct.repo_binding.strip() == binding:
                return title
        return None

    def _ensure_chat_pair(self, repo_title: str, binding: str,
                          ctx: "SheetCtx | None" = None) -> str | None:
        """Ensure the repo bound to `binding` has a paired chat tab, returning its title.
        Idempotent and matched by binding: a fast in-cycle cache (`_chat_pairs`) is
        checked first, then an authoritative scan, before a new `_chat <repo>` tab is
        created (B1 = binding, chat schema stamped). Creating the chat tab acts on the
        binding only — it never writes a human-owned cell of any repo tab."""
        binding = (binding or "").strip()
        if not binding:
            return None
        backend = ctx.backend if ctx is not None else self.backend
        cached = self._chat_pairs.get(binding)
        if cached is not None:
            return cached
        existing = self._find_chat_tab(binding, ctx)
        if existing is not None:
            self._chat_pairs[binding] = existing
            return existing
        title = self._unique_tab_title(f"{C.CHAT_TAB_PREFIX}{repo_title}", ctx)
        with self._lock:
            try:
                backend.create_tab(title)
                backend.write_cell(title, C.CONFIG_ROW, C.COL_REPO_BINDING, binding)  # B1
                backend.ensure_chat_schema(title)
            except Exception as e:  # noqa: BLE001 — a chat-tab failure can't kill the loop
                log.warning("could not create chat tab %r for %r: %s", title, repo_title, e)
                return None
        self._chat_pairs[binding] = title
        log.info("created paired chat tab %r for repo %r", title, repo_title)
        return title

    def _run_chat_tab(self, title: str, tab: Tab,
                      ctx: "SheetCtx | None" = None) -> None:
        """Resolve a chat tab's repo by its B1 binding and run any pending question.
        Best-effort: a resolution/agent hiccup writes a heartbeat/marker, never raises.
        On a friend sheet, a chat tab whose binding is outside the file's allowlist is
        refused (read-only chat must honour the same per-sheet scope as tasks)."""
        binding = tab.repo_binding.strip()
        if not binding:
            self._hb(title, "no REPO_PATH (B1)", ctx=ctx)
            return
        if ctx is not None and ctx.is_friend and not friend_repo_allowed(ctx.friend, binding):
            self._hb(title, "repo not in this file's allowlist", ctx=ctx)
            return
        rr = repolib.resolve(binding, self.cfg)
        if not rr.ok:
            self._hb(title, f"repo error: {rr.reason}", ctx=ctx)
            return
        self._hb(title, f"chat ready, watching {rr.path.name}", ctx=ctx)
        self._maybe_chat(title, tab, rr.path, ctx)

    # -- spec review surface -------------------------------------------------
    def _set_note(self, title: str, row: int, col: int, text: str,
                  ctx: "SheetCtx | None" = None) -> None:
        """Write a cell NOTE on the originating sheet (best-effort; never kills a task)."""
        backend = ctx.backend if ctx is not None else self.backend
        with self._lock:
            try:
                backend.write_note(title, row, col, text)
            except Exception as e:  # noqa: BLE001 — a note is cosmetic-ish, never fatal
                log.warning("write_note failed (%s r%s c%s): %s", title, row, col, e)

    def _spec_digest(self, repo_path: Path, spec_id: str) -> str:
        """The human-readable body of a just-written OpenSpec change — its `proposal.md`
        plus every spec delta — so a gated spec can be REVIEWED from the sheet before
        approval, not approved blind on the change id alone. Best-effort: returns '' if
        the files aren't there; capped so a huge change can't bloat the cell note."""
        spec_id = (spec_id or "").strip()
        if not spec_id:
            return ""
        base = repo_path / "openspec" / "changes" / spec_id
        parts: list[str] = []
        try:
            prop = base / "proposal.md"
            if prop.is_file():
                parts.append(prop.read_text(encoding="utf-8", errors="replace").strip())
        except Exception:  # noqa: BLE001 — best-effort read
            pass
        try:
            for sp in sorted((base / "specs").rglob("spec.md")):
                body = sp.read_text(encoding="utf-8", errors="replace").strip()
                parts.append(f"=== {sp.relative_to(base)} ===\n{body}")
        except Exception:  # noqa: BLE001 — best-effort read
            pass
        text = "\n\n".join(p for p in parts if p).strip()
        return text[:C.COL_SPEC_NOTE_MAX]

    # -- per-task ------------------------------------------------------------
    def _process_task(self, w: WorkItem) -> None:
        title, t = w.title, w.task
        ctx = w.ctx
        # Per-sheet autonomy (Stage 2): a friend file's tasks obey ITS autonomy
        # (gated by default), independent of the master AUTONOMY.
        autonomy = ctx.autonomy if ctx is not None else self.cfg.autonomy
        log.info("[%s r%s] starting (phase=%s try=%s): %s",
                 title, t.row, w.phase, w.next_tries, t.task[:80])
        self._set(title, t.row, status=C.ST_WORKING, updated=_now(),
                  tries=str(w.next_tries), log="агент работать…", ctx=ctx)
        self._hb(title, f"working r{t.row}", ctx=ctx)

        # Live progress: write the agent's stage + approximate % into the row's Log
        # as it runs. Throttled (stage change / ≥5-point jump / ≥20 s) so it can't
        # outspend the Sheets write quota; Status stays `working` the whole time.
        prog = {"stage": "", "pct": -100, "t": 0.0}

        def _on_progress(p: agent.Progress) -> None:
            try:
                now = time.monotonic()
                if not (p.stage != prog["stage"] or p.pct - prog["pct"] >= 5
                        or now - prog["t"] >= 20):
                    return
                prog.update(stage=p.stage, pct=p.pct, t=now)
                # Log (E) + Updated (D) only — never Status, never a human cell.
                self._set(title, t.row, updated=_now(), log=agent.format_progress(p), ctx=ctx)
            except Exception:  # noqa: BLE001 — a progress write can never kill a task
                log.debug("progress write failed", exc_info=True)

        try:
            # The effective prompt/detail were resolved at collect time: for an ordinary
            # row prompt == the task text and detail == ""; for a `/<скилл>` row prompt is
            # the catalog skill's prompt and detail is the trailing free text. Either way
            # the human's column A is never written.
            res = agent.run(self.cfg, w.repo_path, w.prompt or t.task, w.detail,
                            allow_init=w.allow_init, phase=w.phase,
                            spec_id=w.spec_id, on_progress=_on_progress)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s r%s] agent crashed", title, t.row)
            self._set(title, t.row, status=C.ST_FAILED, updated=_now(),
                      log=f"агент упал: {e}"[:480], ctx=ctx)
            return

        # Rate-limit / billing: don't burn retries marking it failed — requeue and
        # let the loop back off. The attempt counter still ticks (stamped above),
        # so a prolonged quota outage eventually dead-letters instead of looping
        # forever on the metered Agent SDK pool.
        if res.rate_limited:
            log.warning("[%s r%s] rate-limited/billing; requeue + back off", title, t.row)
            with self._lock:
                self._backoff = True
            self._set(title, t.row, status=C.ST_QUEUED, updated=_now(),
                      log="лимит запросов. потом повтор.", ctx=ctx)
            return

        # Interrupted by a daemon restart (SIGTERM killed the agent mid-run) — NOT a
        # task failure. Requeue and give back the attempt we stamped at dispatch, so a
        # deploy/restart never permanently fails an in-flight task with the bogus
        # "no structured output" error.
        if res.interrupted:
            log.info("[%s r%s] interrupted by restart; requeue (attempt refunded)", title, t.row)
            self._set(title, t.row, status=C.ST_QUEUED, updated=_now(),
                      tries=str(max(0, w.next_tries - 1)),
                      log="демон рестарт. вернул в очередь.", ctx=ctx)
            return

        # Persist spec id as soon as we know it
        if res.spec_id:
            self._set(title, t.row, spec=res.spec_id, ctx=ctx)

        # Success + terminal status depend on which phase ran.
        shipped = autonomy == "ship" or w.phase == "implement"
        if autonomy == "gated" and w.phase != "implement":
            # spec phase → park for human approval, not done
            success = res.outcome == "spec_ready"
            done_status = C.ST_SPEC_READY
        else:
            success = res.outcome == "implemented" or (
                autonomy == "spec" and res.outcome == "spec_ready"
            )
            done_status = C.ST_DONE

        if success:
            status = done_status
            note = res.summary
            if status == C.ST_SPEC_READY:
                # Surface the actual spec for review: attach proposal + deltas as a NOTE
                # on the Спека cell so the human can READ it before approving (not just
                # see the change id). Best-effort — a missing/unreadable spec just falls
                # back to the id-only message.
                digest = self._spec_digest(w.repo_path, res.spec_id or t.spec)
                if digest:
                    self._set_note(title, t.row, C.COL_SPEC, digest, ctx=ctx)
                    note = ("спека готова — открой примечание ячейки «Спека» (B), "
                            f"прочитай, потом статус approved. {note}")
                else:
                    note = f"спека готова. ставь статус `approved` — тогда деплой. {note}"
            elif shipped:
                note += f" [пуш={res.pushed} деплой={res.deployed}]"
        elif res.outcome == "blocked":
            status = C.ST_BLOCKED
            note = res.error or res.summary or "стоп. я не делать."
        else:
            status = C.ST_FAILED
            note = res.error or res.summary or "сломалось."

        self._set(title, t.row, status=status, updated=_now(), log=note[:480], ctx=ctx)
        log.info("[%s r%s] -> %s", title, t.row, status)

    # -- per-tab -------------------------------------------------------------
    def _reclaim_stale(self, tab: Tab, ctx: "SheetCtx | None" = None) -> None:
        grace = max(self.cfg.agent_timeout * 2, 600)
        for t in tab.rows:
            if t.status.lower() == C.ST_WORKING and _age_seconds(t.updated) > grace:
                log.warning("[%s r%s] reclaiming stale 'working' -> queued", tab.title, t.row)
                self._set(tab.title, t.row, status=C.ST_QUEUED,
                          log="reclaimed after crash", ctx=ctx)
                t.status = C.ST_QUEUED

    # -- control intent queue (`_control` meta-tab) --------------------------
    def _set_control(self, row: int, *, status: str | None = None,
                     result: str | None = None, backend: object | None = None) -> None:
        """Write ONLY the control-owned columns: status (E) and result (F).
        Never touches A-D (id/ts/action/args — Apps-Script-owned). A failed write
        can never kill the loop. `backend` defaults to the master sheet; the friend
        control guard passes the friend backend."""
        backend = backend or self.backend
        with self._lock:
            try:
                if status is not None:
                    backend.write_cell(C.CONTROL_TAB, row,
                                       C.COL_CTL_STATUS, status)
                if result is not None:
                    backend.write_cell(C.CONTROL_TAB, row,
                                       C.COL_CTL_RESULT, result[:480])
            except Exception as e:  # noqa: BLE001 — a write can't kill the loop
                log.warning("control write_cell failed (r%s): %s", row, e)

    def _reclaim_stale_control(self, rows: list[ControlRow]) -> None:
        """Reclaim `_control` rows stuck in `working` (a crash mid-dispatch) back
        to `pending`, reusing the SAME age-based grace as `_reclaim_stale`. A
        fresh `working` row (within the grace window) is left untouched."""
        grace = max(self.cfg.agent_timeout * 2, 600)  # IDENTICAL to _reclaim_stale
        for cr in rows:
            if cr.status.lower() != C.CTL_WORKING:
                continue
            # Age an async skill off the daemon-owned 'entered working' stamp; fall back
            # to the human click time (column B) only when we have no stamp — i.e. after
            # a restart, where a leftover `working` row genuinely IS stale. Read the
            # daemon-owned map under the lock: pool threads write/pop it concurrently,
            # so the read must share their lock discipline (see the pop below).
            with self._lock:
                started = self._ctl_working_since.get(cr.row)
            age = (time.monotonic() - started) if started is not None else _age_seconds(cr.ts)
            if age > grace:
                log.warning("[_control r%s] reclaiming stale working -> pending",
                            cr.row)
                self._set_control(cr.row, status=C.CTL_PENDING,
                                  result="reclaimed after crash")
                cr.status = C.CTL_PENDING
                with self._lock:
                    self._ctl_working_since.pop(cr.row, None)

    def _process_control(self) -> None:
        """One dispatch cycle over `_control`: reclaim stale rows, then process
        pending rows oldest-first. Fully exception-wrapped — it can never raise
        out into `run_forever`."""
        try:
            rows = self.backend.read_control()  # bootstraps + sorts oldest-first
        except Exception as e:  # noqa: BLE001 — a read failure must not kill us
            log.warning("could not read %s: %s", C.CONTROL_TAB, e)
            return
        try:
            self._reclaim_stale_control(rows)
        except Exception:  # noqa: BLE001
            log.exception("control stale-reclaim failed; continuing")
        for cr in rows:
            if _STOP:
                break
            if cr.status.lower() != C.CTL_PENDING:
                # done/error/working -> skip. Idempotency by id: a terminal row's
                # handler is never re-invoked and its result is never overwritten.
                continue
            if cr.action in C.ASYNC_CONTROL_ACTIONS:
                # Long delivery work (run_skill) — dispatch through the agent pool so
                # the poll loop is never blocked for the agent's duration.
                self._dispatch_skill(cr)
            else:
                self._dispatch_control(cr)

    def _dispatch_control(self, cr: ControlRow) -> None:
        """Run one pending control row: pending -> working -> done|error.
        Idempotent by id (caller skips non-pending rows). Never raises."""
        self._set_control(cr.row, status=C.CTL_WORKING, result="dispatching…")
        try:
            try:
                args = json.loads(cr.args) if cr.args.strip() else {}
                if not isinstance(args, dict):
                    raise ValueError("args must be a JSON object")
            except (ValueError, TypeError) as e:
                self._set_control(cr.row, status=C.CTL_ERROR,
                                  result=f"bad args JSON: {e}")
                return
            handler = CONTROL_HANDLERS.get(cr.action)
            if handler is None:
                self._set_control(cr.row, status=C.CTL_ERROR,
                                  result=f"unknown action {cr.action!r}")
                return
            result = handler(self, cr, args) or "done"
            self._set_control(cr.row, status=C.CTL_DONE, result=result)
        except Exception as e:  # noqa: BLE001 — a handler crash can't kill us
            log.exception("[_control r%s] handler crashed", cr.row)
            self._set_control(cr.row, status=C.CTL_ERROR, result=f"crashed: {e}")

    # -- run_skill: async control intent -> agent pool ------------------------
    def _dispatch_skill(self, cr: ControlRow) -> None:
        """Dispatch a pending `run_skill` intent ASYNCHRONOUSLY. Resolves tab→repo and
        the skill's prompt from `_skills`, then (respecting the same per-repo in-flight
        guard as task agents) claims the repo, marks the control row `working`, and
        submits the run to the background agent pool. A repo already in-flight leaves
        the row `pending` for a later cycle; any resolution error marks it `error`.
        Never raises out of the control cycle."""
        try:
            try:
                args = json.loads(cr.args) if cr.args.strip() else {}
                if not isinstance(args, dict):
                    raise ValueError("args must be a JSON object")
            except (ValueError, TypeError) as e:
                self._set_control(cr.row, status=C.CTL_ERROR, result=f"bad args JSON: {e}")
                return
            skill_name = (args.get("skill") or "").strip()
            tab_title = (args.get("tab") or "").strip()
            detail = (args.get("detail") or "").strip()
            if not skill_name or not tab_title:
                self._set_control(cr.row, status=C.CTL_ERROR,
                                  result="run_skill needs args.skill and args.tab")
                return
            if tab_title.startswith(C.META_PREFIX):
                self._set_control(cr.row, status=C.CTL_ERROR,
                                  result=f"{tab_title!r} is a meta tab, not a repo tab")
                return
            # The sheet is the source of truth for what a skill does: look up its prompt
            # via the per-cycle cache so a cycle never pays a second `_skills` read.
            skill = self._skill_map().get(skill_name)
            if skill is None:
                self._set_control(cr.row, status=C.CTL_ERROR,
                                  result=f"unknown skill {skill_name!r}")
                return
            tab = self.backend.read_tab(tab_title)
            if not tab.repo_binding:
                self._set_control(cr.row, status=C.CTL_ERROR,
                                  result=f"tab {tab_title!r} has no repo binding (B1)")
                return
            rr = repolib.resolve(tab.repo_binding, self.cfg)
            if not rr.ok:
                self._set_control(cr.row, status=C.CTL_ERROR,
                                  result=f"cannot resolve repo: {rr.reason}"[:480])
                return
            repo_path = rr.path
            # Per-repo serialization: never two agents in one working dir. Busy → defer.
            with self._lock:
                if repo_path in self._inflight:
                    log.info("[_control r%s] repo %s busy; run_skill stays pending",
                             cr.row, repo_path.name)
                    return  # stays pending, retried next cycle
                self._inflight.add(repo_path)
            self._set_control(cr.row, status=C.CTL_WORKING,
                              result=f"running skill {skill_name}…")
            with self._lock:
                self._ctl_working_since[cr.row] = time.monotonic()
            sr = SkillRun(control_row=cr.row, skill=skill_name, tab_title=tab_title,
                          repo_path=repo_path, task=skill.prompt, detail=detail)
            try:
                self._pool.submit(self._run_skill_tracked, sr)
            except RuntimeError:  # pool shutting down (SIGTERM) — release + leave pending
                with self._lock:
                    self._inflight.discard(repo_path)
                self._set_control(cr.row, status=C.CTL_PENDING,
                                  result="deferred (daemon shutting down)")
        except Exception as e:  # noqa: BLE001 — a dispatch can never kill the loop
            log.exception("[_control r%s] run_skill dispatch crashed", cr.row)
            self._set_control(cr.row, status=C.CTL_ERROR, result=f"crashed: {e}")

    def _run_skill_tracked(self, sr: SkillRun) -> None:
        """Run one skill in a pool thread, then release the in-flight claim. Mirrors
        `_run_group_tracked`: the `finally` guarantees the claim is freed on any crash."""
        try:
            self._run_skill(sr)
        except Exception as e:  # noqa: BLE001 — one skill can't kill the pool
            log.exception("[skill %s] crashed", sr.skill)
            self._set_control(sr.control_row, status=C.CTL_ERROR,
                              result=f"skill {sr.skill} crashed: {e}"[:480])
        finally:
            with self._lock:
                self._inflight.discard(sr.repo_path)
                self._ctl_working_since.pop(sr.control_row, None)

    def _run_skill(self, sr: SkillRun) -> None:
        """Run the skill's prompt as a normal OpenSpec-gated agent task and report the
        outcome back into the control row. Rate-limit / restart requeue to `pending`."""
        log.info("[skill %s] running in %s (control r%s)",
                 sr.skill, sr.repo_path, sr.control_row)
        self._hb(sr.tab_title, f"skill {sr.skill} running")
        res = agent.run(self.cfg, sr.repo_path, sr.task, sr.detail,
                        allow_init=self.cfg.auto_openspec_init, phase="full")
        if res.rate_limited:
            with self._lock:
                self._backoff = True
            self._set_control(sr.control_row, status=C.CTL_PENDING,
                              result=f"skill {sr.skill}: rate-limited; will retry")
            return
        if res.interrupted:
            self._set_control(sr.control_row, status=C.CTL_PENDING,
                              result=f"skill {sr.skill}: interrupted by restart; will retry")
            return
        spec = f" spec={res.spec_id}" if res.spec_id else ""
        if res.outcome in ("implemented", "spec_ready"):
            note = f"скилл {sr.skill} → {res.outcome}{spec}: {res.summary}"
            self._set_control(sr.control_row, status=C.CTL_DONE, result=note[:480])
        else:
            note = (f"скилл {sr.skill} → {res.outcome}{spec}: "
                    f"{res.error or res.summary or 'сломалось.'}")
            self._set_control(sr.control_row, status=C.CTL_ERROR, result=note[:480])
        log.info("[skill %s] done -> %s", sr.skill, res.outcome)

    # -- repo-tab provisioning helpers (used by the add_repo handler) ---------
    def _tab_bound_to(self, path: str) -> str | None:
        """Title of an existing tab whose B1 binding equals `path` exactly, else
        None. Meta tabs (`_`-prefixed) carry no binding and are skipped. A single
        unreadable tab can never break the scan — it is just skipped. The binding
        is the only source of truth for "is this repo already bound" (no local
        bookkeeping that could drift from the sheet)."""
        for title in self.backend.list_tab_titles():
            if title.startswith(C.META_PREFIX):
                continue
            try:
                tab = self.backend.read_tab(title)
            except Exception:  # noqa: BLE001 — one bad tab must not break add_repo
                continue
            if tab.repo_binding.strip() == path:
                return title
        return None

    def _unique_tab_title(self, base: str,
                          ctx: "SheetCtx | None" = None) -> str:
        """`base` if free, else `base-2`, `base-3`, … (first free). The result is
        always <=100 chars: `base` is truncated to leave room for the `-N` suffix
        before it is appended, so a long name can never overflow the Sheets cap."""
        backend = ctx.backend if ctx is not None else self.backend
        existing = set(backend.list_tab_titles())
        if base not in existing:
            return base
        n = 2
        while True:
            suffix = f"-{n}"
            trimmed = base[: sheets._TAB_TITLE_MAX - len(suffix)]
            candidate = f"{trimmed}{suffix}"
            if candidate not in existing:
                return candidate
            n += 1

    # -- friend sheets: mint + seed a shared file (Drive seam) ----------------
    def _mint_friend_sheet(self, recipient: str, repos: list[str],
                           autonomy: str) -> tuple[str, str]:
        """Mint a brand-new spreadsheet, share it with the owner + recipient, and seed
        it with one bound repo tab per allowlisted repo. Returns `(sheet_id, url)`.

        This is the single Drive seam for the share flow: ALL of the irreversible,
        Google-touching work lives here so the offline test suite can stub this one
        method and never hit the network (mirroring how `create_repo` isolates
        `create_beelink_repo.sh`). Owned by the service account; shared back to the
        owner so they keep full access. When `recipient` is blank the file is
        "owner-distributed": only the owner-share runs (no recipient-share) and the
        title is derived from the repo allowlist instead of an empty recipient."""
        title = (f"beelink • {recipient} ({len(repos)} repo)" if recipient
                 else f"beelink • {repos[0]} ({len(repos)} repo)")
        sheet_id, url = self.backend.create_spreadsheet(title)
        # Share back to the owner (full access) FIRST so a later failure still leaves
        # the owner able to reach the file; then the recipient (if one was named).
        if self.cfg.owner_email:
            try:
                self.backend.share_spreadsheet(sheet_id, self.cfg.owner_email, "writer")
            except Exception as e:  # noqa: BLE001 — owner-share best-effort
                log.warning("could not share friend sheet with owner: %s", e)
        if recipient:
            self.backend.share_spreadsheet(sheet_id, recipient, "writer")
        # Seed the friend file with a bound repo tab per allowlisted repo, using a
        # backend pinned to the NEW sheet id.
        fb = sheets.GoogleBackend(sheet_id, self.cfg.sa_json)
        for binding in repos:
            ttl = sheets.sanitize_tab_title(binding)
            fb.create_tab(ttl)
            fb.write_cell(ttl, C.CONFIG_ROW, C.COL_REPO_BINDING, binding)  # B1
            fb.ensure_schema(ttl)
        return sheet_id, url

    def share_repos(self, recipient: str, repos, autonomy: str | None = None, *,
                    on_minted=None) -> tuple[str, str, list[str], str]:
        """Mint + share a friend file for a chosen subset of repos — the single,
        shared implementation behind both the `share_repos` control handler and the
        `share` CLI subcommand, so the two entry points can never drift.

        The recipient is OPTIONAL: a blank recipient mints an "owner-distributed"
        file (shared back to `OWNER_EMAIL` only, for the owner to hand out) and is
        rejected only when no `OWNER_EMAIL` is configured. Normalises `repos` (a
        separator-delimited string or a list), rejects any repo not bound on the
        master sheet (you can only share
        what you operate — this also blocks sharing a meta tab), resolves a blank
        autonomy to `FRIEND_DEFAULT_AUTONOMY` and rejects an out-of-range value, mints
        via the Drive seam, then appends the `_friends` registry row. `on_minted`, if
        given, fires AFTER the mint but BEFORE the registry append so a caller (the
        control handler) can durably record the minted URL even if the registry write
        later hiccups. Returns `(sheet_id, url, repos, autonomy)`. Any validation error
        raises `ValueError` (the handler turns that into an `error` row; the CLI into a
        non-zero exit) — nothing is minted in that case."""
        recipient = (recipient or "").strip()
        # A blank recipient is "owner-distributed": the owner mints the file and hands
        # it out themselves. Only refuse when there is ALSO no owner to share it back
        # to — that would mint a file nobody but the service account could reach.
        if not recipient and not self.cfg.owner_email:
            raise ValueError(
                "share_repos needs a recipient (an e-mail) or OWNER_EMAIL configured")
        if isinstance(repos, str):
            repos = sheets._split_allowlist(repos)
        elif isinstance(repos, list):
            repos = [str(x).strip() for x in repos if str(x).strip()]
        else:
            repos = []
        if not repos:
            raise ValueError("share_repos needs repos (≥1 repo to share)")
        unknown = [r for r in repos if self._tab_bound_to(r) is None]
        if unknown:
            raise ValueError(f"not bound on the master sheet: {', '.join(unknown)}")
        autonomy = (autonomy or "").strip().lower() or self.cfg.friend_default_autonomy
        if autonomy not in C.AUTONOMY_LEVELS:
            raise ValueError(f"autonomy must be {C.AUTONOMY_CHOICES}, got {autonomy!r}")
        sheet_id, url = self._mint_friend_sheet(recipient, repos, autonomy)
        if on_minted is not None:
            on_minted(sheet_id, url)
        self.backend.append_friend(sheet_id, repos, recipient, autonomy, url)
        return sheet_id, url, repos, autonomy

    # -- skill trigger (`/<скилл>` typed into a repo task cell) ---------------
    def _skill_map(self) -> dict:
        """The `_skills` catalog by name, cached for the cycle. Lazily read the first
        time a `/<скилл>` task is seen so an ordinary cycle pays no extra Sheets read.
        A read failure caches an empty map (the row then fails with 'unknown skill')."""
        if self._skill_map_cache is None:
            try:
                self._skill_map_cache = {s.name: s for s in self.backend.read_skills()}
            except Exception as e:  # noqa: BLE001 — a skills read can't kill collection
                log.warning("could not read skills catalog: %s", e)
                self._skill_map_cache = {}
        return self._skill_map_cache

    def _resolve_skill_task(self, t: TaskRow) -> tuple[str, str, str | None]:
        """Resolve a task cell to the effective (prompt, detail, error). An ordinary task
        returns `(task_text, "", None)`. A `/<скилл> [контекст]` cell returns the catalog
        skill's prompt + the trailing text as detail, or `("", "", <error>)` for an
        unknown/blank skill — the caller fails the row instead of running literal text."""
        raw = t.task.strip()
        if not raw.startswith(C.SKILL_TRIGGER_PREFIX):
            return t.task, "", None
        body = raw[len(C.SKILL_TRIGGER_PREFIX):].strip()
        parts = body.split(None, 1)
        if not parts:
            return "", "", (f"пустой скилл. впиши /<скилл> — каталог во вкладке "
                            f"{C.SKILLS_TAB}")
        name = parts[0]
        detail = parts[1].strip() if len(parts) > 1 else ""
        skill = self._skill_map().get(name)
        if skill is None:
            return "", "", (f"неизвестный скилл /{name}. список скиллов — вкладка "
                            f"{C.SKILLS_TAB}")
        return skill.prompt, detail, None

    def _plan_row(self, title: str, repo_path: Path, allow_init: bool,
                  t: TaskRow, prompt: str, detail: str,
                  ctx: "SheetCtx | None" = None) -> WorkItem | None:
        """Turn one actionable row into a WorkItem, enforcing the attempt cap and
        resolving the gated phase. `prompt`/`detail` are the effective agent prompt and
        extra context (skill-resolved at collect time). The phase uses the SHEET's
        autonomy (a friend file's own level, gated by default). Returns None (and may
        dead-letter the row) when it should not be dispatched this cycle."""
        autonomy = ctx.autonomy if ctx is not None else self.cfg.autonomy
        status = t.status.strip().lower()
        # `retry`/`approved` are deliberate human restarts → fresh attempt budget.
        base = 0 if status in C.RESET_TRIES else t.tries
        if base >= self.cfg.max_attempts:
            self._set(title, t.row, status=C.ST_FAILED, updated=_now(),
                      log=f"max attempts ({self.cfg.max_attempts}) reached; "
                          "set status `retry` to re-run", ctx=ctx)
            return None

        if status == C.ST_APPROVED:
            phase, spec_id = "implement", t.spec.strip()
            if not spec_id:
                self._set(title, t.row, status=C.ST_BLOCKED, updated=_now(),
                          log="approved but no spec id in column B", ctx=ctx)
                return None
        elif autonomy == "gated":
            phase, spec_id = "spec", ""
        else:
            phase, spec_id = "full", ""

        return WorkItem(title=title, repo_path=repo_path, allow_init=allow_init,
                        task=t, phase=phase, spec_id=spec_id, next_tries=base + 1,
                        prompt=prompt, detail=detail, ctx=ctx)

    def _collect_tab(self, title: str, ctx: "SheetCtx | None" = None) -> list[WorkItem]:
        """Read a tab, reclaim stale rows, run the gates, and return ready work.
        All sheet reads/gate-writes happen here, single-threaded per cycle. `ctx`
        selects the sheet: None/master for the owner's sheet, a friend ctx for a
        shared file (whose backend, autonomy and repo allowlist all flow from it)."""
        backend = ctx.backend if ctx is not None else self.backend
        try:
            tab = backend.read_tab(title)  # bootstraps schema on first read
        except Exception as e:  # noqa: BLE001
            log.warning("could not read tab %r: %s", title, e)
            return []

        if not tab.repo_binding:
            self._hb(title, "no REPO_PATH (B1)", ctx=ctx)
            return []

        self._reclaim_stale(tab, ctx)

        # Per-sheet allowlist (Stage 2): a friend file may operate ONLY its shared
        # repos. A tab whose binding is outside the allowlist is blocked, never run —
        # the security gate, applied before any repo/openspec resolution.
        if ctx is not None and ctx.is_friend and not friend_repo_allowed(ctx.friend,
                                                                         tab.repo_binding):
            self._hb(title, "repo not in this file's allowlist", ctx=ctx)
            for t in tab.rows:
                if t.actionable:
                    self._set(title, t.row, status=C.ST_BLOCKED, updated=_now(),
                              log="repo not shared in this friend file (allowlist)",
                              ctx=ctx)
            return []

        rr = repolib.resolve(tab.repo_binding, self.cfg)
        if not rr.ok:
            self._hb(title, f"repo error: {rr.reason}", ctx=ctx)
            for t in tab.rows:
                if t.actionable:
                    self._set(title, t.row, status=C.ST_BLOCKED, updated=_now(),
                              log=rr.reason[:480], ctx=ctx)
            return []

        repo_path = rr.path

        # Chat now lives on a separate paired chat tab. Ensure the pair exists (migrates
        # repos that predate the split) on the SAME sheet. Best-effort: a chat-tab
        # hiccup must never block task collection.
        try:
            self._ensure_chat_pair(title, tab.repo_binding, ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("ensure chat pair failed for %r (tasks unaffected): %s", title, e)

        has_os = repolib.has_openspec(repo_path)
        allow_init = self.cfg.auto_openspec_init
        if not has_os and not allow_init:
            # OpenSpec-only policy: refuse repos without openspec.
            self._hb(title, f"no openspec/ in {repo_path.name}", ctx=ctx)
            for t in tab.rows:
                if t.actionable:
                    self._set(title, t.row, status=C.ST_BLOCKED, updated=_now(),
                              log="repo has no openspec/ (OpenSpec-only policy)", ctx=ctx)
            return []

        self._hb(title, f"idle, watching {repo_path.name}", ctx=ctx)
        items: list[WorkItem] = []
        for t in tab.rows:
            if not t.actionable:
                continue
            # Resolve a `/<скилл>` task cell to the catalog skill's prompt (+detail).
            # An unknown/blank skill fails the row here instead of running literal text.
            prompt, detail, skill_err = self._resolve_skill_task(t)
            if skill_err is not None:
                self._set(title, t.row, status=C.ST_FAILED, updated=_now(),
                          log=skill_err[:480], ctx=ctx)
                continue
            w = self._plan_row(title, repo_path, allow_init, t, prompt, detail, ctx)
            if w is not None:
                items.append(w)
            if self.cfg.max_tasks_per_cycle and len(items) >= self.cfg.max_tasks_per_cycle:
                break
        return items

    def _run_items(self, items: list[WorkItem]) -> None:
        """Dispatch agents into the background pool: parallel across distinct repos,
        serial within one repo (two agents in the same working dir would race on git).

        NON-BLOCKING: submits each repo's group and returns immediately so the poll
        loop keeps running `_process_control` every cycle while agents work. A repo
        already in `_inflight` is skipped (its rows aren't re-dispatched until its
        group finishes); global concurrency is capped by the pool's `max_workers`."""
        if not items or _STOP:
            return
        # No priority column any more: dispatch in stable sheet order (tab, then row).
        items.sort(key=lambda w: (w.title, w.task.row))
        groups: dict[Path, list[WorkItem]] = {}
        for w in items:
            groups.setdefault(w.repo_path, []).append(w)
        with self._lock:
            fresh = {p: g for p, g in groups.items() if p not in self._inflight}
            for p in fresh:
                self._inflight.add(p)
        for repo_path, group in fresh.items():
            try:
                self._pool.submit(self._run_group_tracked, repo_path, group)
            except RuntimeError:  # pool shutting down (SIGTERM) — drop the claim
                with self._lock:
                    self._inflight.discard(repo_path)

    def _run_group_tracked(self, repo_path: Path, group: list[WorkItem]) -> None:
        """Run one repo's tasks serially in a pool thread, then release the in-flight
        claim so the repo can be picked up again next cycle. Exception-wrapped per
        task; the `finally` guarantees the claim is freed even on crash."""
        try:
            for w in group:
                if _STOP:
                    break
                try:
                    self._process_task(w)
                except Exception:  # noqa: BLE001 — one task can't kill the pool
                    log.exception("[%s r%s] unexpected error", w.title, w.task.row)
        finally:
            with self._lock:
                self._inflight.discard(repo_path)

    def _process_tab(self, title: str) -> int:
        """Collect + dispatch a single tab. Kept for `once` and external callers;
        the main loop collects across all tabs first for cross-repo parallelism."""
        if title.startswith(C.META_PREFIX):
            return 0  # never treat a reference/meta tab as a repo tab
        items = self._collect_tab(title)
        self._run_items(items)
        return len(items)

    # -- per-cycle batched snapshot ------------------------------------------
    def _begin_cycle(self, ctx: "SheetCtx") -> bool:
        """Take the one-request per-cycle snapshot for `ctx`'s sheet so every read this
        cycle is served locally. Returns False on failure (e.g. a 429 surviving the
        retry) — the caller then skips that sheet's heavy poll for the cycle instead of
        degrading into a per-tab read fan-out. Never raises."""
        try:
            ctx.backend.begin_cycle()
            return True
        except Exception as e:  # noqa: BLE001 — a snapshot miss must never kill the cycle
            log.warning("snapshot read failed on %s (%s); skipping its poll this cycle",
                        ctx.label, e)
            return False

    # -- friend sheets: per-cycle backends + scoped polling (Stage 2) --------
    def _friend_backend(self, sheet_id: str):
        """A backend pinned to a friend sheet, cached across cycles. Rebuilt next
        cycle if it errors. Seam tests override by pre-seeding `_friend_backends`."""
        be = self._friend_backends.get(sheet_id)
        if be is None:
            be = sheets.make_friend_backend(self.cfg, sheet_id)
            self._friend_backends[sheet_id] = be
        return be

    def _friend_contexts(self) -> list["SheetCtx"]:
        """Build one `SheetCtx` per registered friend sheet from the master `_friends`
        registry. Each carries the friend's own backend, effective autonomy and
        allowlist. A registry read failure (or an unbuildable backend) skips friend
        sheets entirely — the master cycle is never affected."""
        try:
            friends = self.backend.read_friends()
        except Exception as e:  # noqa: BLE001 — registry hiccup must not break the cycle
            log.warning("could not read %s (friend sheets skipped this cycle): %s",
                        C.FRIENDS_TAB, e)
            return []
        ctxs: list[SheetCtx] = []
        seen: set[str] = set()
        for f in friends:
            sid = (f.sheet_id or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            try:
                be = self._friend_backend(sid)
            except Exception as e:  # noqa: BLE001 — a lost-access sheet is just skipped
                log.warning("friend sheet %s backend unavailable (skipped): %s",
                            sid[:8], e)
                self._friend_backends.pop(sid, None)
                continue
            ctxs.append(SheetCtx(backend=be, autonomy=friend_autonomy(f, self.cfg),
                                 label=sid[:8], friend=f))
        return ctxs

    def _guard_friend_control(self, ctx: "SheetCtx") -> None:
        """Reject repo-creating / file-minting intents on a friend sheet's `_control`
        (only called when that tab already exists — never bootstraps one)."""
        for cr in ctx.backend.read_control():
            if cr.status.lower() != C.CTL_PENDING:
                continue
            if cr.action in FRIEND_FORBIDDEN_CONTROL:
                log.warning("[%s _control r%s] rejecting %r from friend sheet",
                            ctx.label, cr.row, cr.action)
                self._set_control(cr.row, status=C.CTL_ERROR,
                                  result=f"{cr.action} not permitted on a friend sheet",
                                  backend=ctx.backend)

    def _poll_sheet(self, ctx: "SheetCtx") -> list[WorkItem]:
        """Collect ready work from ONE sheet (master or friend) and run its pending
        chat turns. Per-sheet: resets the binding→chat-tab cache, lists that sheet's
        tabs, defends a friend file's `_control`, collects repo tabs, then runs chat.
        Returns the WorkItems to dispatch. Each tab/chat is individually wrapped, and
        the caller wraps the whole call, so one bad sheet can never break the cycle."""
        items: list[WorkItem] = []
        # The binding→chat-tab cache is per-sheet: master and a friend file may bind
        # the SAME repo, but each has its own chat tab — never cross them.
        self._chat_pairs = {}
        try:
            titles = ctx.backend.list_tab_titles()
        except Exception as e:  # noqa: BLE001
            log.warning("could not list tabs on %s: %s", ctx.label, e)
            return items
        # A friend file must never be a control surface for partners.
        if ctx.is_friend and C.CONTROL_TAB in titles:
            try:
                self._guard_friend_control(ctx)
            except Exception:  # noqa: BLE001
                log.exception("friend control guard failed on %s; continuing", ctx.label)
        # Prebuild binding→chat-tab from this sheet's chat tabs so the migration in
        # `_collect_tab` never duplicates a pair; stash grids to run after the repo loop.
        chat_tabs: list[tuple[str, Tab]] = []
        for title in titles:
            if _STOP:
                break
            if not title.startswith(C.CHAT_TAB_PREFIX):
                continue
            try:
                ct = ctx.backend.read_chat_tab(title)
            except Exception as e:  # noqa: BLE001 — a bad chat tab can't kill us
                log.warning("could not read chat tab %r on %s: %s", title, ctx.label, e)
                continue
            b = ct.repo_binding.strip()
            if b:
                self._chat_pairs.setdefault(b, title)
            chat_tabs.append((title, ct))
        # Repo tabs: collect work (also ensures the paired chat tab on THIS sheet).
        for title in titles:
            if _STOP:
                break
            if title.startswith(C.META_PREFIX):
                continue  # _repos / _control / _skills / _chat … reference/meta tabs
            items += self._collect_tab(title, ctx)
        # Chat tabs: run any pending question on this sheet.
        for title, ct in chat_tabs:
            if _STOP:
                break
            try:
                self._run_chat_tab(title, ct, ctx)
            except Exception:  # noqa: BLE001 — chat can never kill the cycle
                log.exception("chat tab %r on %s failed; continuing", title, ctx.label)
        return items

    # -- one cycle -----------------------------------------------------------
    def run_once(self, drain: bool = False) -> int:
        """Run ONE supervisor cycle and return the number of work items dispatched.

        Dispatch queued `_control` intents FIRST — they're fast admin ops (add/create
        a repo tab) a human just clicked a button for, and must not wait behind
        a long repo-task batch (which can block `_run_items` up to AGENT_TIMEOUT).
        Then collect ready work from every repo tab (sequential reads) and submit it
        in one batch so different repos run in parallel. Read-only chat lives on a
        separate paired chat tab (`_chat <repo>`): each cycle reads those tabs, ensures
        every repo has one (migrating older repos), and runs any pending question.
        Fully exception-wrapped per the never-die contract: this can never raise.

        `drain=True` (the one-shot `once` CLI) waits for the in-flight agent + chat
        pools to finish before returning; the long-running loop passes drain=False and
        never blocks here. THE SINGLE definition of a cycle — `run_forever` and the
        `once` command both go through it, so they can never drift apart."""
        items: list[WorkItem] = []
        # The per-sheet binding->chat-tab cache is (re)built inside `_poll_sheet`.
        # Drop the per-cycle skills cache so a freshly edited `_skills` catalog is seen.
        self._skill_map_cache = None
        # If a pool agent has reported a rate-limit/billing signal, don't pile a fresh
        # batch of agents onto the throttled API this cycle: skip the claude-driven work
        # (repo tasks + chat). Peek only — `run_forever` consumes+resets the flag under
        # the lock and applies the cool-off sleep. Without this, the next cycle would
        # dispatch before the loop ever reached its backoff check. Read under the lock:
        # pool threads set the flag under the same lock, so the peek shares their
        # discipline rather than relying on read atomicity.
        with self._lock:
            backing_off = self._backoff
        master = SheetCtx(backend=self.backend, autonomy=self.cfg.autonomy,
                          label="master", friend=None)
        try:
            # ONE batched read snapshots the whole master sheet for this cycle; every
            # read below (ensure/control/poll) is served from it instead of a per-tab
            # API fan-out. A failed snapshot skips the master poll rather than degrade
            # into that fan-out; `end_cycle` always clears the snapshot.
            master_ok = self._begin_cycle(master)
            try:
                # Self-bootstrap the `_skills` catalog so a daemon-only operator (the
                # documented `sheet_agent run` deploy path) sees the catalog tab and the
                # `▶️ Запустить скилл` menu without a separate `bootstrap`/`skills` CLI
                # step. Seed-once + quota-friendly (cached in the backend after the first
                # confirmation), best-effort: a `_skills` hiccup must never block the cycle.
                try:
                    self.backend.ensure_skills_tab()
                except Exception as e:  # noqa: BLE001 — never let _skills kill the cycle
                    log.warning("ensure skills catalog failed (tasks unaffected): %s", e)
                # Ensure the `_friends` registry tab exists so the owner sees the shared-
                # file list (and the `📤 Поделиться репо` flow has somewhere to record).
                # Seed-once + quota-friendly, best-effort: never block the cycle.
                try:
                    self.backend.ensure_friends_schema()
                except Exception as e:  # noqa: BLE001 — never let _friends kill the cycle
                    log.warning("ensure friends registry failed (tasks unaffected): %s", e)
                # Keep the add-repo dropdown source (`_repos`) in sync with live discovery
                # so deleted/REPO_IGNORE'd repos drop out. Quota-friendly (writes only on
                # change) and best-effort: a refresh failure must never block the cycle.
                try:
                    self._refresh_repos()
                except Exception as e:  # noqa: BLE001 — never let _repos refresh kill the cycle
                    log.warning("refresh repos failed (tasks unaffected): %s", e)
                # `_control` is processed ONLY on the master sheet (friend files get a
                # defensive read-only guard in `_poll_sheet`, never full dispatch).
                self._process_control()
                if backing_off:
                    log.warning("rate-limit cool-off in effect — skipping repo task & "
                                "chat dispatch this cycle")
                elif not master_ok:
                    log.warning("master snapshot unavailable — skipping poll this cycle")
                else:
                    # Poll the master sheet first, then every registered friend sheet.
                    # Each friend sheet is snapshotted (one read) then polled, all
                    # exception-wrapped so one bad sheet can never break the cycle. All
                    # work is collected, then dispatched together so distinct repos run
                    # in parallel and the same repo (even shared across sheets) stays serial.
                    items += self._poll_sheet(master)
                    for fctx in self._friend_contexts():
                        if _STOP:
                            break
                        if not self._begin_cycle(fctx):
                            continue
                        try:
                            items += self._poll_sheet(fctx)
                        except Exception:  # noqa: BLE001 — one friend sheet can't kill us
                            log.exception("friend sheet %s failed; continuing", fctx.label)
                        finally:
                            fctx.backend.end_cycle()
                    self._run_items(items)
            finally:
                self.backend.end_cycle()
        except Exception:  # noqa: BLE001 — one cycle must never kill the daemon
            log.exception("cycle failed; backing off")
        if drain:
            self._pool.shutdown(wait=True)
            self._chat_pool.shutdown(wait=True)
        return len(items)

    # -- main loop -----------------------------------------------------------
    def run_forever(self) -> None:
        log.info("orchestrator up: backend=%s autonomy=%s model=%s poll=%ss concurrency=%s",
                 self.cfg.backend, self.cfg.autonomy, self.cfg.model,
                 self.cfg.poll_interval, self.cfg.max_concurrent_agents)
        while not _STOP:
            cycle_start = time.monotonic()
            self.run_once(drain=False)
            # Sleep the remainder of the interval, responsive to SIGTERM.
            remaining = max(1.0, self.cfg.poll_interval - (time.monotonic() - cycle_start))
            # Consume+reset the flag under the lock (pool threads set it under the same
            # lock) so a concurrent set isn't lost between read and reset.
            with self._lock:
                backed_off = self._backoff
                self._backoff = False
            if backed_off:
                remaining = max(remaining, 300.0)  # cool off after rate-limit
                log.warning("backing off %.0fs after rate-limit/billing signal", remaining)
            _sleep(remaining)
        # SIGTERM: let in-flight agents finish (honour "stop after current task")
        # before exiting, so no repo is left mid-dispatch.
        log.info("draining %d in-flight agent group(s)…", len(self._inflight))
        self._pool.shutdown(wait=True)
        self._chat_pool.shutdown(wait=True)
        log.info("orchestrator stopped cleanly")


def _sleep(seconds: float) -> None:
    """Interruptible sleep that returns early on SIGTERM/SIGINT."""
    slept = 0.0
    while slept < seconds and not _STOP:
        time.sleep(min(1.0, seconds - slept))
        slept += 1.0


def run(_cfg: C.Config | None = None) -> None:
    """Supervisor entrypoint with degraded-wait startup.

    A robust daemon must not crash-loop because a credential or .env isn't there
    yet. We hold the lock and keep (re)loading config until it's valid and the
    backend connects, then run forever. Drop the service-account JSON later and
    it goes live on its own — no restart needed.
    """
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    lock = Path((_cfg or C.load()).state_dir) / "sheet-agent.lock"
    with SingleInstance(lock):
        warned = False
        while not _STOP:
            cfg = C.load()  # reload each attempt so a freshly-created .env is seen
            problems = cfg.validate()
            if problems:
                if not warned:
                    for p in problems:
                        log.warning("waiting for config: %s", p)
                    log.warning("daemon is up and will start automatically once fixed")
                    warned = True
                _sleep(30)
                continue
            try:
                orch = Orchestrator(cfg)
            except Exception as e:  # noqa: BLE001 — sheet unreachable yet
                log.warning("backend not ready (%s); retrying in 30s", e)
                _sleep(30)
                continue
            orch.run_forever()
            return


# --------------------------------------------------------------------------
# Control handler: add_repo
# --------------------------------------------------------------------------
def _h_add_repo(orch: "Orchestrator", cr: ControlRow, args: dict) -> str:
    """Provision a NEW repo-bound tab from `args.path`.

    Title = the sanitized LAST path segment (Google-Sheets-illegal chars stripped,
    capped at 100), with `-2`/`-3`/… suffixing on collision. The new tab is
    bootstrapped to the current layout: B1 = path, A1 label + the row-2 task header
    via `ensure_schema`. There is no Product Vision cell.

    Invariants:
    - Idempotent: if a tab is already bound to this EXACT path, this is a no-op
      marked `done` (NOT error) — never a duplicate tab. The check runs BEFORE any
      tab is created.
    - Provisioning a fresh tab is the daemon acting on the human's explicit intent,
      so stamping the NEW tab's B1/labels is allowed. It NEVER writes a human-owned
      cell (A or the B1 binding) of any OTHER existing tab.
    - Order: create -> set B1 -> ensure_schema. B1 is written before ensure_schema
      (schema stamping never touches B1)."""
    path = (args.get("path") or "").strip()
    if not path:
        raise ValueError("add_repo needs args.path (the repo path to bind)")

    # Idempotency: an existing tab already bound to this exact path -> no-op done.
    # Still ensure the paired chat tab exists (migrates repos added before the split).
    existing = orch._tab_bound_to(path)
    if existing is not None:
        orch._ensure_chat_pair(existing, path)
        return f"repo already bound to tab {existing!r}; ensured chat pair; no-op"

    title = orch._unique_tab_title(sheets.sanitize_tab_title(path))

    orch.backend.create_tab(title)
    orch.backend.write_cell(title, C.CONFIG_ROW, C.COL_REPO_BINDING, path)  # B1
    orch.backend.ensure_schema(title)            # stamps A1 label + row-2 task header
    # Provision the paired read-only chat tab (matched by binding, idempotent).
    orch._ensure_chat_pair(title, path)
    return f"added repo tab {title!r} + chat pair -> {path}"


register_control_handler("add_repo", _h_add_repo)


# --------------------------------------------------------------------------
# Control handler: create_repo
# --------------------------------------------------------------------------
def _h_create_repo(orch: "Orchestrator", cr: ControlRow, args: dict) -> str:
    """Provision a BRAND-NEW beelink repo, then bind it to a fresh sheet tab.

    The irreversible work (copy template, register in deployer, write .env, build
    compose, openspec init, git init+commit, create the GitHub repo, SSH push) is
    done by the DETERMINISTIC engine `scripts/create_beelink_repo.sh` — never an
    LLM. This handler is the daemon entry point into that single engine; the
    `create-beelink-repo` skill is the human entry point into the SAME script.

    Steps:
    1. Run the script with `--name`/`--vision`/`--template`. The `--vision` text
       seeds the NEW repo's own `docs/strategy/vision.md` / `openspec/project.md` at
       creation (a repo artifact, not a sheet feature). Any non-zero exit (invalid
       name, dir/repo already exists, deployer/GitHub failure) raises so the
       dispatcher marks the row `error` — the daemon never dies.
    2. Parse the script's `{url, path}` stdout and record both into the control
       row `result`.
    3. Reuse the `add_repo` path to create + bootstrap the bound tab. The sheet has
       no Product Vision cell, so nothing vision-related is written back to the tab.

    Invariant: the script is the only thing that touches GitHub/deployer/git; the
    handler only orchestrates and writes back to the sheet."""
    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("create_repo needs args.name (the bare repo name)")
    vision = (args.get("vision") or "").strip()
    template = (args.get("template") or "init_project").strip()

    argv = [orch.cfg.create_repo_script, "--name", name,
            "--vision", vision, "--template", template]
    # This handler runs INLINE in the poll loop (it's a sync control action), so cap
    # the worst-case block well under the agent timeout: the engine is a deterministic
    # script (copy + deployer + git + a small push), seconds in practice — it must not
    # be able to stall task collection / chat for the full 30-minute agent_timeout.
    timeout = min(orch.cfg.agent_timeout, _CREATE_REPO_TIMEOUT)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise RuntimeError(f"create_beelink_repo.sh failed: {err}")

    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        url = info["url"]
        path = info["path"]
    except (ValueError, KeyError, IndexError) as e:
        raise RuntimeError(f"could not parse script output: {e}: {proc.stdout!r}")

    # Record {url, path} into the control row result FIRST so even a later
    # sheet-write hiccup leaves the irreversible outcome captured.
    orch._set_control(cr.row, result=f"url={url} path={path}")

    # Bind the new repo to a fresh tab (reuse the idempotent add_repo path). The sheet
    # carries no Product Vision cell, so nothing vision-related is written back here —
    # the supplied vision already seeded the new repo's own docs via the script.
    full_name = f"beelink-{name}"
    existing = orch._tab_bound_to(path)
    if existing is not None:
        title = existing
    else:
        title = orch._unique_tab_title(sheets.sanitize_tab_title(path))
        orch.backend.create_tab(title)
        orch.backend.write_cell(title, C.CONFIG_ROW, C.COL_REPO_BINDING, path)  # B1
        orch.backend.ensure_schema(title)
    # Provision the paired read-only chat tab (matched by binding, idempotent).
    orch._ensure_chat_pair(title, path)
    return f"created {full_name}: url={url} path={path} tab={title!r}"


register_control_handler("create_repo", _h_create_repo)


# --------------------------------------------------------------------------
# Control handler: share_repos
# --------------------------------------------------------------------------
def _h_share_repos(orch: "Orchestrator", cr: ControlRow, args: dict) -> str:
    """Mint a NEW Google file sharing a chosen subset of repos with a partner.

    Given `args.recipient` (e-mail), `args.repos` (≥1 repo binding, each already a
    tab on the master sheet) and optional `args.autonomy`, this: mints + shares a new
    spreadsheet (the Drive work is in `_mint_friend_sheet`), then records the friend
    in the `_friends` registry and reports the URL.

    Invariants:
    - The minted file is a fresh, script-less spreadsheet: it has NO bound Apps Script
      and NO `_control` tab, so it cannot enqueue `add_repo`/`create_repo` — the
      no-create guarantee holds by construction (Stage 2 enforces it on the loop).
    - A missing recipient, empty `repos`, an unknown repo, or a bad autonomy raises so
      the dispatcher marks the row `error` — the supervisor never dies.
    - The registry write (the durable record) goes to the MASTER sheet only; the
      handler never touches a human-owned cell of any repo tab."""
    recipient = (args.get("recipient") or "").strip()
    # All validation + the mint/register live in `Orchestrator.share_repos` (shared
    # with the `share` CLI). The `on_minted` callback captures the irreversible
    # outcome into the control result BEFORE the registry write, so even a later
    # sheet hiccup leaves the minted URL recorded.
    _, url, repos, autonomy = orch.share_repos(
        recipient, args.get("repos"), args.get("autonomy"),
        on_minted=lambda sid, u: orch._set_control(cr.row, result=f"minted {u}"))
    who = recipient or "owner (distribute yourself)"
    return f"shared {len(repos)} repo(s) with {who} [{autonomy}]: {url}"


register_control_handler("share_repos", _h_share_repos)
