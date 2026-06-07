"""Sheet control-plane access.

Two interchangeable backends:
  * GoogleBackend — real Google Sheets via gspread + a service account.
  * MockBackend   — a local JSON file, for dry-run/offline development.

The sheet layout is fixed (see config.py). On a repo task tab the daemon owns columns
B/C/D/E/F (task state — F/Tries is a hidden column) plus the config/header rows; the
human owns only column A (Task). The read-only chat lives on a separate PAIRED chat tab
(`_chat <repo>`, matched by the B1 binding): there the daemon owns the whole
transcript and the pinned answer, and only consumes the human's compose box.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from tenacity import retry, stop_after_attempt, wait_exponential

from . import config as C
from .log import log


@dataclass
class TaskRow:
    row: int                 # 1-indexed sheet row
    task: str = ""
    spec: str = ""
    status: str = ""
    updated: str = ""
    logmsg: str = ""
    tries: int = 0           # F: dispatch attempts so far (daemon; hidden column)

    @property
    def actionable(self) -> bool:
        return bool(self.task.strip()) and self.status.strip().lower() in C.ACTIONABLE


@dataclass
class ChatTurn:
    """One exchange in a chat tab's read-only transcript (cols A/B). The daemon owns
    BOTH cells: it echoes the human's question into A{row} and writes the reply into
    B{row}. The human's only input surface is the A2 compose box."""
    row: int                 # 1-indexed sheet row
    question: str = ""       # A (COL_CHAT_Q)
    answer: str = ""         # B (COL_CHAT_A)


@dataclass
class Tab:
    title: str
    repo_binding: str = ""   # B1: absolute path or git url or bare name
    branch: str = ""         # D1
    rows: list[TaskRow] = field(default_factory=list)
    # Chat fields are populated only for a chat tab (parse_chat_grid); a repo tab leaves
    # them empty. chat_input is the A2 compose box; chat_turns is the A4:B.. transcript.
    chat_input: str = ""
    chat_turns: list[ChatTurn] = field(default_factory=list)


@dataclass
class Skill:
    """One row of the `_skills` catalog: a name (picker value), a human-facing
    description, and the agent-facing prompt the daemon runs as the task."""
    name: str = ""           # A
    description: str = ""     # B
    prompt: str = ""          # C


@dataclass
class Friend:
    """One row of the `_friends` registry: a shared friend file. `repos` is the
    per-sheet repo allowlist (the bindings the file may operate on); `autonomy`
    defaults to `gated` (resolved by `parse_friends_grid`)."""
    sheet_id: str = ""               # A
    repos: list[str] = field(default_factory=list)  # B (allowlist)
    recipient: str = ""              # C
    autonomy: str = ""               # D
    link: str = ""                   # E


@dataclass
class ControlRow:
    """One row of the `_control` intent queue. Apps Script owns id/ts/action/args
    (A-D); the daemon owns status/result (E/F). `args` stays a raw JSON string —
    it is parsed by the dispatcher, never here."""
    row: int                 # 1-indexed sheet row
    id: str = ""
    ts: str = ""
    action: str = ""
    args: str = ""           # raw JSON string
    status: str = ""
    result: str = ""


# --------------------------------------------------------------------------
# Tab-title sanitization (shared, pure — no backend)
# --------------------------------------------------------------------------
# Google Sheets rejects these characters in a worksheet title.
_ILLEGAL_TAB_CHARS = re.compile(r"[:\\/?*\[\]]")
_TAB_TITLE_MAX = 100      # Google Sheets caps a worksheet title at 100 chars.
_FALLBACK_TAB_TITLE = "repo"  # used when the sanitized last segment is empty


def _last_segment(path: str) -> str:
    """Last path segment of `path`, ignoring trailing slashes (so `/a/b/` -> `b`).
    Falls back to the full string when there is no segment (e.g. `path` is `///`)."""
    name = PurePosixPath(path.rstrip("/")).name
    return name or path


def sanitize_tab_title(path: str) -> str:
    """Derive a Google-Sheets-legal worksheet title from a repo path: take the LAST
    path segment, strip the illegal chars ``: \\ / ? * [ ]`` and cap the result at
    100 chars. An empty result (e.g. an all-illegal segment) falls back to a fixed,
    non-empty default so a tab title is never empty (Sheets rejects empty titles)."""
    title = _ILLEGAL_TAB_CHARS.sub("", _last_segment(path.strip())).strip()
    if not title:
        title = _FALLBACK_TAB_TITLE
    return title[:_TAB_TITLE_MAX]


class Backend(Protocol):
    def read_all(self) -> dict[str, list[list[str]]]: ...
    def begin_cycle(self) -> None: ...
    def end_cycle(self) -> None: ...
    def list_tab_titles(self) -> list[str]: ...
    def read_tab(self, title: str) -> Tab: ...
    def write_cell(self, title: str, row: int, col: int, value: str) -> None: ...
    def write_cells(self, title: str, cells: list[tuple[int, int, str]]) -> None: ...
    def write_note(self, title: str, row: int, col: int, text: str) -> None: ...
    def delete_column(self, title: str, col: int) -> None: ...
    def delete_row(self, title: str, row: int) -> None: ...
    def create_tab(self, title: str) -> None: ...
    def ensure_schema(self, title: str, grid: list[list[str]] | None = None) -> None: ...
    def read_chat_tab(self, title: str) -> Tab: ...
    def ensure_chat_schema(self, title: str, grid: list[list[str]] | None = None) -> None: ...
    def prettify(self, title: str) -> None: ...
    def set_repo_dropdown(self, title: str) -> None: ...
    def ensure_repos_tab(self, repos: list) -> None: ...
    def heartbeat(self, title: str, text: str) -> None: ...
    def read_control(self) -> list[ControlRow]: ...
    def ensure_control_schema(self) -> None: ...
    def read_skills(self) -> list[Skill]: ...
    def ensure_skills_tab(self, skills: list | None = None) -> None: ...
    def sync_skills(self) -> list[str]: ...
    def read_friends(self) -> list[Friend]: ...
    def ensure_friends_schema(self) -> None: ...
    def append_friend(self, sheet_id: str, repos: list[str], recipient: str,
                      autonomy: str, link: str) -> None: ...


# --------------------------------------------------------------------------
# Shared parsing
# --------------------------------------------------------------------------
def _grid_range(sid, r0: int, r1: int, c0: int, c1: int) -> dict:
    """A half-open GridRange for the batch_update API (0-based, end-exclusive).
    Shared by every formatting helper so the shape lives in one place."""
    return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c0, "endColumnIndex": c1}


def _cell(grid: list[list[str]], row: int, col: int) -> str:
    """1-indexed access into a ragged grid, blank if missing."""
    r = row - 1
    c = col - 1
    if 0 <= r < len(grid) and 0 <= c < len(grid[r]):
        return str(grid[r][c]).strip()
    return ""


def _grid_set(grid: list[list[str]], row: int, col: int, value: str) -> None:
    """1-indexed write into a ragged grid, growing rows/cols as needed. Keeps the
    in-memory grid a migration just edited consistent with the sheet, so the SAME
    poll cycle parses the new values."""
    while len(grid) < row:
        grid.append([])
    r = grid[row - 1]
    while len(r) < col:
        r.append("")
    r[col - 1] = value


def _int_cell(grid: list[list[str]], row: int, col: int, default: int) -> int:
    raw = _cell(grid, row, col)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def parse_grid(title: str, grid: list[list[str]]) -> Tab:
    repo = _cell(grid, C.CONFIG_ROW, 2)      # B1
    branch = _cell(grid, C.CONFIG_ROW, 4)    # D1
    tab = Tab(title=title, repo_binding=repo, branch=branch)
    last = len(grid)
    for r in range(C.FIRST_TASK_ROW, last + 1):
        task = _cell(grid, r, C.COL_TASK)
        spec = _cell(grid, r, C.COL_SPEC)
        status = _cell(grid, r, C.COL_STATUS)
        updated = _cell(grid, r, C.COL_UPDATED)
        logmsg = _cell(grid, r, C.COL_LOG)
        if not any([task, spec, status]):
            continue
        tab.rows.append(
            TaskRow(row=r, task=task, spec=spec, status=status,
                    updated=updated, logmsg=logmsg,
                    tries=_int_cell(grid, r, C.COL_TRIES, 0))
        )
    # Chat no longer lives on the repo tab — it has its own paired chat tab (see
    # parse_chat_grid). Leave Tab.chat_input/chat_turns at their empty defaults here.
    return tab


def parse_chat_grid(title: str, grid: list[list[str]]) -> Tab:
    """Parse a dedicated chat tab (`_chat <repo>`): the repo binding (`B1`), the compose
    box (`A2`) and the `A4:B..` transcript. The chat tab is paired to its repo tab by
    the shared `B1` binding, NOT by its title — so `Tab.repo_binding` is the pair key."""
    tab = Tab(title=title, repo_binding=_cell(grid, C.CONFIG_ROW, C.COL_REPO_BINDING))
    tab.chat_input = _cell(grid, C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL)   # A2 compose box
    for r in range(C.CHAT_FIRST_ROW, len(grid) + 1):
        q = _cell(grid, r, C.COL_CHAT_Q)
        a = _cell(grid, r, C.COL_CHAT_A)
        if not (q or a):
            continue
        tab.chat_turns.append(ChatTurn(row=r, question=q, answer=a))
    return tab


def _is_chat_initialized(grid: list[list[str]]) -> bool:
    """A chat tab is initialized once its A3 header is stamped."""
    return _cell(grid, C.CHAT_HEADER_ROW, C.COL_CHAT_Q) == C.CHAT_HEADERS[0]


# Old English config-row label + header (pre-Russianisation), kept as migration
# sentinels so an already-bootstrapped tab is detected and relabelled in place.
_OLD_LABEL_REPO = "REPO_PATH"
_OLD_LABEL_BRANCH = "BRANCH"
_OLD_HEADER_TASK = "Task"   # first header cell under the old English layout


def _is_initialized(grid: list[list[str]]) -> bool:
    """A repo tab counts as initialised under EITHER the old English labels
    (`REPO_PATH`/`Task`) or the new Russian ones (`Репозиторий`/`Задача`), so the
    Russianisation migration never triggers a spurious re-bootstrap."""
    a1 = _cell(grid, C.CONFIG_ROW, 1).strip().lower()
    h1 = _cell(grid, C.HEADER_ROW, 1).strip().lower()
    repo_ok = a1 in {_OLD_LABEL_REPO.lower(), C.CONFIG_LABEL_REPO.lower()}
    head_ok = h1 in {_OLD_HEADER_TASK.lower(), C.HEADERS[0].lower()}
    return repo_ok and head_ok


def _migrate_russianize(backend, title: str, grid: list[list[str]]) -> bool:
    """Mandatory, idempotent layout migration shared by both backends: bring a repo tab
    still on the old English labels to the Russian ones — `A1` (`REPO_PATH` →
    `Репозиторий`), `C1` (`BRANCH` → `Ветка`) and the header row (`Task,…` → the Russian
    `HEADERS`). Realigns the in-memory `grid` in place so the SAME poll cycle parses the
    new labels, and NEVER touches the `B1` binding or any task data. Returns True iff it
    changed anything (so a formatting backend can re-prettify to land colours/widths on
    an already-bootstrapped tab). A no-op once the labels are already Russian."""
    changed = False
    if _cell(grid, C.CONFIG_ROW, 1) == _OLD_LABEL_REPO:
        backend.write_cell(title, C.CONFIG_ROW, 1, C.CONFIG_LABEL_REPO)
        _grid_set(grid, C.CONFIG_ROW, 1, C.CONFIG_LABEL_REPO)
        changed = True
    if _cell(grid, C.CONFIG_ROW, 3) == _OLD_LABEL_BRANCH:
        backend.write_cell(title, C.CONFIG_ROW, 3, C.CONFIG_LABEL_BRANCH)
        _grid_set(grid, C.CONFIG_ROW, 3, C.CONFIG_LABEL_BRANCH)
        changed = True
    if _cell(grid, C.HEADER_ROW, 1) == _OLD_HEADER_TASK:
        for i, h in enumerate(C.HEADERS, start=1):
            backend.write_cell(title, C.HEADER_ROW, i, h)
            _grid_set(grid, C.HEADER_ROW, i, h)
        changed = True
    return changed


# The old layout carried a human "Product Vision" row at row 2 (`VISION` label in A2,
# merged B2:F2 vision text) with the task header pushed down to row 3. The Product
# Vision feature has been removed; the task header now sits at row 2 (HEADER_ROW).
_OLD_VISION_ROW = 2   # physical row 2 under the old layout (the VISION row)


def _migrate_drop_vision_row(backend, title: str, grid: list[list[str]]) -> None:
    """Mandatory layout migration, shared by both backends. If a tab still carries the
    old Product Vision row (`VISION` in A2, task header on row 3), delete physical row 2
    so the task header realigns to row 2 (HEADER_ROW) and tasks to row 3, and realign the
    in-memory `grid` in place so the SAME poll cycle parses the new layout. Idempotent:
    once A2 no longer reads `VISION` (it is the `Task` header) this is a no-op.

    Runs BEFORE the drop-Detail / drop-Priority column migrations so they inspect the
    realigned header row, not the stale VISION row."""
    if _cell(grid, _OLD_VISION_ROW, 1).upper() != "VISION":
        return
    backend.delete_row(title, _OLD_VISION_ROW)
    idx = _OLD_VISION_ROW - 1
    if len(grid) > idx:
        del grid[idx]


# The old task grid had a human "Detail" column at D (the 4th column). It has been
# removed. A tab bootstrapped under that old 8-column layout still has `Detail`
# sitting in its header cell D3 with the daemon columns shifted one to the right
# (Updated/Log/Tries/Priority in E/F/G/H). Dropping D realigns it to the 7-column
# layout (Priority in G), which `_migrate_drop_priority_column` then collapses to
# the current 6-column layout (A–F, Tries hidden).
_OLD_DETAIL_COL = 4   # physical column D under the old 8-column layout


def _migrate_drop_detail_column(backend, title: str, grid: list[list[str]]) -> None:
    """Mandatory layout migration, shared by both backends. If a tab still carries the
    old 8-column header (`Detail` in D3), delete physical column D so the remaining
    daemon-owned data realigns to the new 7-column layout, and realign the in-memory
    `grid` in place so the SAME poll cycle parses the new layout. Idempotent: once D3
    no longer reads `Detail` this is a no-op (so a real `Updated` value is never eaten)."""
    if _cell(grid, C.HEADER_ROW, _OLD_DETAIL_COL).lower() != "detail":
        return
    backend.delete_column(title, _OLD_DETAIL_COL)
    # Keep the grid we are about to parse consistent with the sheet we just edited.
    idx = _OLD_DETAIL_COL - 1
    for row in grid:
        if len(row) > idx:
            del row[idx]


# The old task grid had a human "Priority" column at G (the 7th column). It has been
# removed (tasks run in row order); the visible grid is now 6 wide (A–F). A tab
# bootstrapped under the old 7-column layout still has `Priority` sitting in its
# header cell G3, with Tries in F.
_OLD_PRIORITY_COL = 7   # physical column G under the old 7-column layout


def _migrate_drop_priority_column(backend, title: str, grid: list[list[str]]) -> None:
    """Mandatory layout migration, shared by both backends. If a tab still carries the
    old 7-column header (`Priority` in G3), delete physical column G so the grid
    realigns to the new 6-column layout (Tries stays in F), and realign the in-memory
    `grid` in place so the SAME poll cycle parses the new layout. Idempotent: once G3
    no longer reads `Priority` this is a no-op.

    Runs AFTER `_migrate_drop_detail_column`: a tab on the very old 8-column layout is
    first realigned to 7 columns (Priority lands in G3), then this drops G — both in
    one cycle."""
    if _cell(grid, C.HEADER_ROW, _OLD_PRIORITY_COL).lower() != "priority":
        return
    backend.delete_column(title, _OLD_PRIORITY_COL)
    idx = _OLD_PRIORITY_COL - 1
    for row in grid:
        if len(row) > idx:
            del row[idx]


def parse_control_grid(grid: list[list[str]]) -> list[ControlRow]:
    """Parse the `_control` intent-queue grid (header row 1, data from row 2).

    Returns rows oldest-first. `ts` is ISO-8601 and so sorts lexicographically;
    we key on `(ts, row)` so blank/equal timestamps still resolve oldest-first by
    sheet row (append order). A header-only or empty grid yields no rows.
    """
    rows: list[ControlRow] = []
    for r in range(C.CONTROL_FIRST_ROW, len(grid) + 1):
        cid = _cell(grid, r, C.COL_CTL_ID)
        action = _cell(grid, r, C.COL_CTL_ACTION)
        args = _cell(grid, r, C.COL_CTL_ARGS)
        status = _cell(grid, r, C.COL_CTL_STATUS)
        # Skip fully-blank rows; a row with any of the meaningful cells counts.
        if not any([cid, action, args, status]):
            continue
        rows.append(ControlRow(
            row=r, id=cid, ts=_cell(grid, r, C.COL_CTL_TS), action=action,
            args=args, status=status, result=_cell(grid, r, C.COL_CTL_RESULT)))
    rows.sort(key=lambda x: (x.ts, x.row))
    return rows


def _is_control_initialized(grid: list[list[str]]) -> bool:
    return _cell(grid, C.CONTROL_HEADER_ROW, C.COL_CTL_ID).lower() == "id"


def _split_allowlist(raw: str) -> list[str]:
    """Split a `_friends` `repos` cell into an allowlist. Accepts newline OR comma
    separators (the dialog writes newline-joined; a human may type commas), trims
    each entry and drops blanks, preserving order without duplicates."""
    parts = re.split(r"[\n,]+", raw or "")
    seen: list[str] = []
    for p in (x.strip() for x in parts):
        if p and p not in seen:
            seen.append(p)
    return seen


def parse_friends_grid(grid: list[list[str]]) -> list[Friend]:
    """Parse the `_friends` registry grid (header row 1, data from row 2). A row
    counts when it has a non-empty sheet id (col A); blank rows are skipped. A blank
    autonomy cell defaults to `gated` so a friend file is never accidentally
    full-autonomy."""
    friends: list[Friend] = []
    for r in range(C.FRIENDS_FIRST_ROW, len(grid) + 1):
        sid = _cell(grid, r, C.COL_FRIEND_SHEET_ID)
        if not sid:
            continue
        autonomy = _cell(grid, r, C.COL_FRIEND_AUTONOMY).strip().lower() or "gated"
        friends.append(Friend(
            sheet_id=sid,
            repos=_split_allowlist(_cell(grid, r, C.COL_FRIEND_REPOS)),
            recipient=_cell(grid, r, C.COL_FRIEND_RECIPIENT),
            autonomy=autonomy,
            link=_cell(grid, r, C.COL_FRIEND_LINK),
        ))
    return friends


def _is_friends_initialized(grid: list[list[str]]) -> bool:
    return _cell(grid, C.FRIENDS_HEADER_ROW, C.COL_FRIEND_SHEET_ID).lower() == "sheet_id"


def parse_skills_grid(grid: list[list[str]]) -> list[Skill]:
    """Parse the `_skills` catalog grid (header row 1, data from row 2). A row counts
    when it has a non-empty Skill name (col A); blank/placeholder rows are skipped."""
    skills: list[Skill] = []
    for r in range(C.SKILLS_FIRST_ROW, len(grid) + 1):
        name = _cell(grid, r, C.COL_SKILL_NAME)
        if not name:
            continue
        skills.append(Skill(
            name=name,
            description=_cell(grid, r, C.COL_SKILL_DESC),
            prompt=_cell(grid, r, C.COL_SKILL_PROMPT),
        ))
    return skills


def _is_skills_initialized(grid: list[list[str]]) -> bool:
    """Initialised under EITHER the old English name header (`Skill`) or the new
    Russian one (`Скилл`), so Russianisation never re-seeds (which would clobber
    curated prompts/prunes)."""
    h = _cell(grid, C.SKILLS_HEADER_ROW, C.COL_SKILL_NAME).strip().lower()
    return h in {"skill", C.SKILLS_HEADERS[0].lower()}


def _status_cf_requests(sid) -> list[dict]:
    """Build the per-status conditional-format `addConditionalFormatRule` requests for a
    sheet's Status column (rows from FIRST_TASK_ROW down): one rule per status in
    `STATUS_COLORS`, each painting the cell when its text equals that status. Pure (no
    API) so it is unit-testable; the caller clears existing rules first for idempotency."""
    fr = C.FIRST_TASK_ROW - 1
    col = C.COL_STATUS - 1
    reqs: list[dict] = []
    for i, (status, (r, g, b)) in enumerate(C.STATUS_COLORS.items()):
        reqs.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [_grid_range(sid, fr, 1000, col, col + 1)],
            "booleanRule": {
                "condition": {"type": "TEXT_EQ",
                              "values": [{"userEnteredValue": status}]},
                "format": {"backgroundColor": {"red": r, "green": g, "blue": b}}}}}})
    return reqs


# --------------------------------------------------------------------------
# Google backend
# --------------------------------------------------------------------------
class GoogleBackend:
    def __init__(self, sheet_id: str, sa_json: str):
        import gspread
        from google.oauth2.service_account import Credentials

        # `drive.file` lets the SA CREATE and SHARE the spreadsheets IT mints (the
        # friend files) without granting blanket Drive access. `spreadsheets` covers
        # all the cell reads/writes on the master + minted sheets.
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ]
        creds = Credentials.from_service_account_file(
            str(Path(sa_json).expanduser()), scopes=scopes
        )
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(sheet_id)
        self._ws_cache: dict[str, object] = {}
        self._initialized: set[str] = set()  # tabs whose schema we've confirmed
        # Per-cycle snapshot of every tab's grid (one batched read). None between
        # cycles → reads fall back to live per-tab fetches.
        self._grid_cache: dict[str, list[list[str]]] | None = None

    def _ws(self, title: str):
        if title not in self._ws_cache:
            self._ws_cache[title] = self._sh.worksheet(title)
        return self._ws_cache[title]

    # -- per-cycle batched snapshot (quota: ONE read replaces the per-tab fan-out) --
    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def read_all(self) -> dict[str, list[list[str]]]:
        """Every tab's cell grid in ONE API request: `spreadsheets.get` with
        `includeGridData` and a `formattedValue`-only field mask (so the payload stays
        small — no formatting/merges). Returns `{title: grid}`; empty/trailing cells
        are simply absent (the parsers tolerate ragged rows via `_cell`)."""
        meta = self._sh.fetch_sheet_metadata(params={
            "includeGridData": "true",
            "fields": "sheets(properties(title),data(rowData(values(formattedValue))))",
        })
        out: dict[str, list[list[str]]] = {}
        for sh in meta.get("sheets", []):
            title = sh.get("properties", {}).get("title", "")
            data = sh.get("data", [])
            rows = data[0].get("rowData", []) if data else []
            out[title] = [[c.get("formattedValue", "") for c in r.get("values", [])]
                          for r in rows]
        return out

    def begin_cycle(self) -> None:
        """Snapshot the whole spreadsheet so every read this cycle is served locally
        from one API call. Raises on failure — the caller decides to skip the sheet."""
        self._grid_cache = self.read_all()

    def end_cycle(self) -> None:
        self._grid_cache = None

    def _grid(self, title: str) -> list[list[str]]:
        """The tab's grid: from this cycle's snapshot if present, else a live fetch
        (a tab created mid-cycle, or a caller that didn't begin a cycle)."""
        if self._grid_cache is not None and title in self._grid_cache:
            return self._grid_cache[title]
        return self._ws(title).get_all_values()

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def list_tab_titles(self) -> list[str]:
        # Served from the cycle snapshot when one is active; otherwise re-fetch
        # metadata so newly added tabs are seen.
        if self._grid_cache is not None:
            return list(self._grid_cache.keys())
        self._sh = self._gc.open_by_key(self._sh.id)
        self._ws_cache.clear()
        return [ws.title for ws in self._sh.worksheets()]

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def read_tab(self, title: str) -> Tab:
        # Grid from the cycle snapshot (or a live fetch); bootstrap the schema from it
        # on first sight, never a separate get_all_values (quota-friendly).
        grid = self._grid(title)
        if title not in self._initialized:
            self.ensure_schema(title, grid)
            self._initialized.add(title)
        return parse_grid(title, grid)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def write_cell(self, title: str, row: int, col: int, value: str) -> None:
        self._ws(title).update_cell(row, col, value)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def write_cells(self, title: str, cells: list[tuple[int, int, str]]) -> None:
        """Write several (row, col, value) cells in ONE batch API call — a quarter of
        the round-trips (and an atomic update) versus N `write_cell`s. Used for the
        chat consume/answer writes, which always touch a small fixed set together."""
        if not cells:
            return
        import gspread.utils as gu
        data = [{"range": gu.rowcol_to_a1(r, c), "values": [[v]]} for r, c, v in cells]
        self._ws(title).batch_update(data)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def write_note(self, title: str, row: int, col: int, text: str) -> None:
        """Attach (or replace) a cell NOTE — used to surface the full spec for review on
        the Спека cell without growing the visible grid. `update_note` is idempotent."""
        import gspread.utils as gu
        self._ws(title).update_note(gu.rowcol_to_a1(row, col), text)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def delete_column(self, title: str, col: int) -> None:
        """Delete one physical 1-indexed column from the tab, shifting the columns to
        its right left by one (used by the drop-Detail layout migration)."""
        ws = self._ws(title)
        self._sh.batch_update({"requests": [{"deleteDimension": {"range": {
            "sheetId": ws.id, "dimension": "COLUMNS",
            "startIndex": col - 1, "endIndex": col}}}]})

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def delete_row(self, title: str, row: int) -> None:
        """Delete one physical 1-indexed row from the tab, shifting the rows below it up
        by one (used by the drop-vision-row layout migration)."""
        ws = self._ws(title)
        self._sh.batch_update({"requests": [{"deleteDimension": {"range": {
            "sheetId": ws.id, "dimension": "ROWS",
            "startIndex": row - 1, "endIndex": row}}}]})

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def create_tab(self, title: str) -> None:
        """Create an empty worksheet `title` if it does not already exist; reuse the
        existing one otherwise (idempotent, no duplicate). Mirrors the add-worksheet
        pattern of `ensure_repos_tab`/`ensure_control_schema` so handlers never reach
        into gspread directly. Caller stamps the schema afterwards via `ensure_schema`."""
        import gspread
        try:
            ws = self._sh.worksheet(title)
        except gspread.WorksheetNotFound:
            # Born wide enough for the task grid (A–F); the dedicated chat tab only uses
            # A/B, so this width covers both kinds of tab create_tab provisions.
            ws = self._sh.add_worksheet(title=title, rows=200, cols=C.COL_TRIES)
        self._ws_cache[title] = ws

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def ensure_schema(self, title: str, grid: list[list[str]] | None = None) -> None:
        ws = self._ws(title)
        if grid is None:
            grid = ws.get_all_values()
        # MANDATORY layout migration: runs on ALREADY-initialized tabs too (an old
        # tab has the Product Vision row at row 2 with the task header pushed to row 3),
        # so it must sit BEFORE the _is_initialized early-return. Deletes the old VISION
        # row so the header realigns to row 2. Idempotent. Runs FIRST so the column
        # migrations below inspect the realigned header row.
        _migrate_drop_vision_row(self, title, grid)
        # MANDATORY layout migration: drop the removed human "Detail" column (D) on a
        # tab still bootstrapped under the old 8-column layout. Like the heartbeat
        # migration it must run on ALREADY-initialized tabs, so it sits BEFORE the
        # _is_initialized early-return. Idempotent (no-op once D3 != "Detail").
        _migrate_drop_detail_column(self, title, grid)
        # MANDATORY layout migration: drop the removed human "Priority" column (G) on a
        # tab still bootstrapped under the old 7-column layout. Runs after the Detail
        # migration (which realigns Priority into G first). Idempotent (no-op once
        # G3 != "Priority").
        _migrate_drop_priority_column(self, title, grid)
        # MANDATORY layout migration: bring an already-bootstrapped tab from the old
        # English labels (`REPO_PATH`/`BRANCH`/`Task,…`) to the Russian ones. Runs on
        # already-initialized tabs too, so it sits BEFORE the _is_initialized early-
        # return. When it changes anything, re-prettify so the status colours + widths
        # land on the existing tab. Idempotent (no-op once the labels are Russian).
        relabeled = _migrate_russianize(self, title, grid)
        # Chat no longer lives on the repo tab — it has its own paired `_chat <repo>`
        # tab (see ensure_chat_schema / read_chat_tab). The repo tab's schema therefore
        # stamps NOTHING in the old J/K region; any stale J/K from before this shipped is
        # left untouched (harmless) and simply ignored by parse_grid.
        if _is_initialized(grid):
            if relabeled:
                try:
                    self.prettify(title)
                except Exception as e:  # noqa: BLE001 — formatting is cosmetic
                    log.warning("prettify failed on %r after relabel (schema ok): %s", title, e)
            return
        log.info("bootstrapping schema on tab %r", title)
        updates = [
            {"range": f"A{C.CONFIG_ROW}", "values": [[C.CONFIG_LABEL_REPO]]},
            {"range": f"C{C.CONFIG_ROW}", "values": [[C.CONFIG_LABEL_BRANCH]]},
            {"range": f"A{C.HEADER_ROW}:F{C.HEADER_ROW}", "values": [C.HEADERS]},
        ]
        ws.batch_update(updates)
        try:
            self.prettify(title)
        except Exception as e:  # noqa: BLE001 — formatting is cosmetic, never block
            log.warning("prettify failed on %r (schema still ok): %s", title, e)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def read_chat_tab(self, title: str) -> Tab:
        """Read one dedicated chat tab, bootstrapping its schema on the first read.
        Mirrors `read_tab` (one get_all_values per cycle) but parses the chat layout
        (B1 binding, A2 compose box, A4:B.. transcript)."""
        grid = self._grid(title)
        key = f"chat::{title}"
        if key not in self._initialized:
            self.ensure_chat_schema(title, grid)
            self._initialized.add(key)
        return parse_chat_grid(title, grid)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def ensure_chat_schema(self, title: str, grid: list[list[str]] | None = None) -> None:
        """Idempotently stamp the dedicated chat tab's schema: the A1 `Репозиторий` label,
        the A3/B3 headers, and the visible A2 compose-box placeholder when empty (never
        clobbering a typed question). The B1 binding is written by the provisioning path,
        not here. Formatting is cosmetic and best-effort — a failure never blocks chat."""
        ws = self._ws(title)
        if grid is None:
            grid = ws.get_all_values()
        # Seed the visible compose-box placeholder into an EMPTY A2 (independent of the
        # header guard so it also migrates a chat tab created before placeholder seeding).
        if _cell(grid, C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL) == "":
            self.write_cell(title, C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL, C.CHAT_INPUT_PLACEHOLDER)
        # Relabel an old chat tab's A1 (`REPO_PATH` → `Репозиторий`) in place; idempotent.
        if _cell(grid, C.CONFIG_ROW, 1) == _OLD_LABEL_REPO:
            self.write_cell(title, C.CONFIG_ROW, 1, C.CONFIG_LABEL_REPO)
        if _is_chat_initialized(grid):
            return
        log.info("bootstrapping schema on chat tab %r", title)
        ws.batch_update([
            {"range": f"A{C.CONFIG_ROW}", "values": [[C.CONFIG_LABEL_REPO]]},
            {"range": f"A{C.CHAT_HEADER_ROW}:B{C.CHAT_HEADER_ROW}", "values": [C.CHAT_HEADERS]},
        ])
        try:
            self._prettify_chat_tab(title)
        except Exception as e:  # noqa: BLE001 — chat formatting is cosmetic
            log.warning("chat prettify failed on %r (chat still works): %s", title, e)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def _prettify_chat_tab(self, title: str) -> None:
        """Format the dedicated chat tab: frozen top rows (so the compose box + pin stay
        on screen), bold A3/B3 headers, a highlighted A2 compose box + B2 pinned answer
        with hint notes, wrapped TEXT transcript, and generous A/B widths. Idempotent."""
        ws = self._ws(title)
        sid = ws.id

        def _range(r0: int, r1: int, c0: int, c1: int) -> dict:
            return _grid_range(sid, r0, r1, c0, c1)

        header_fmt = {
            "backgroundColor": {"red": 0.20, "green": 0.37, "blue": 0.31},
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        }
        qc = C.COL_CHAT_Q - 1   # 0-based A
        ac = C.COL_CHAT_A - 1   # 0-based B
        last = 1000
        requests: list[dict] = [
            # Freeze the top rows so the binding row, the compose box (A2) and the
            # pinned answer (B2) stay on screen while the transcript below scrolls.
            {"updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"frozenRowCount": C.CHAT_HEADER_ROW}},
                "fields": "gridProperties.frozenRowCount"}},
            # Hide the config row (row 1): the Репозиторий label + B1 binding are daemon
            # plumbing the human never edits (the ➕ menu writes the binding), mirroring
            # the repo task tab — "чат без лишнего", just compose box + headers +
            # transcript on screen. The binding is still read by position (B1), so this
            # is display-only; the frozen-row count is unchanged (the hidden row simply
            # collapses out of the frozen region, keeping A2/A3 on screen).
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": C.CONFIG_ROW - 1, "endIndex": C.CONFIG_ROW},
                "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
            # Bold the A1 Репозиторий label (CONFIG_LABEL_REPO).
            {"repeatCell": {
                "range": _range(C.CONFIG_ROW - 1, C.CONFIG_ROW, 0, 1),
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold"}},
            # Chat headers A3/B3.
            {"repeatCell": {
                "range": _range(C.CHAT_HEADER_ROW - 1, C.CHAT_HEADER_ROW, qc, ac + 1),
                "cell": {"userEnteredFormat": header_fmt},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            # Compose box A2: soft-green fill = "пиши сюда". TEXT so a digits-only
            # question is never coerced to a number.
            {"repeatCell": {
                "range": _range(C.CHAT_INPUT_ROW - 1, C.CHAT_INPUT_ROW, qc, qc + 1),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.85, "green": 0.95, "blue": 0.85},
                    "numberFormat": {"type": "TEXT"},
                    "wrapStrategy": "WRAP", "verticalAlignment": "TOP"},
                    "note": C.CHAT_INPUT_NOTE},
                "fields": "userEnteredFormat(backgroundColor,numberFormat,wrapStrategy,verticalAlignment),note"}},
            # Pinned latest answer B2.
            {"repeatCell": {
                "range": _range(C.CHAT_PINNED_ROW - 1, C.CHAT_PINNED_ROW, ac, ac + 1),
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"},
                                               "wrapStrategy": "WRAP",
                                               "verticalAlignment": "TOP"},
                         "note": C.CHAT_PINNED_NOTE},
                "fields": "userEnteredFormat(numberFormat,wrapStrategy,verticalAlignment),note"}},
            # Transcript: CLIP, never WRAP — a long question/answer must not grow its
            # row, so many turns stay visible at once (full text stays in the cell).
            # TEXT so questions/answers (incl. digits-only) stay text.
            {"repeatCell": {
                "range": _range(C.CHAT_FIRST_ROW - 1, last, qc, ac + 1),
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"},
                                               "wrapStrategy": "CLIP",
                                               "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(numberFormat,wrapStrategy,verticalAlignment)"}},
        ]
        # Generous A/B widths.
        for idx, w in ((qc, 320), (ac, 460)):
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": idx, "endIndex": idx + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        self._sh.batch_update({"requests": requests})
        # B1 binding dropdown from the _repos reference tab; harmless to skip if absent.
        try:
            self.set_repo_dropdown(title)
        except Exception as e:  # noqa: BLE001 — needs _repos tab; cosmetic
            log.debug("repo dropdown skipped on chat tab %r: %s", title, e)

    def _conditional_format_count(self, sid) -> int:
        """How many conditional-format rules currently exist on the sheet `sid`. Read
        from spreadsheet metadata so prettify can clear them before re-adding (keeping
        the status colouring idempotent).

        A read error PROPAGATES (it does not return 0): the only caller, `prettify`, is
        `@retry`-wrapped and best-effort, so a transient failure is retried with an
        accurate count and a persistent one skips this prettify entirely — leaving the
        existing rules intact. Returning 0 on error would emit zero deletes and then
        ADD the status rules on top, doubling the Status-column rules on every hiccup."""
        meta = self._sh.fetch_sheet_metadata()
        for s in meta.get("sheets", []):
            if s.get("properties", {}).get("sheetId") == sid:
                return len(s.get("conditionalFormats", []) or [])
        return 0  # the sheet has no rules yet (genuinely zero)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def prettify(self, title: str) -> None:
        """Apply the human-friendly layout: frozen header, a Status dropdown, sensible
        widths, ownership hints, and the hidden Tries column. Cosmetic — a failure here
        must never block the daemon, so callers wrap it. Idempotent + re-runnable.

        Also (re)writes the A–F header so a tab bootstrapped under an older schema is
        brought to the current 6-column layout.
        """
        ws = self._ws(title)
        ws.update(f"A{C.HEADER_ROW}:F{C.HEADER_ROW}", [C.HEADERS])
        sid = ws.id

        def _cols(start: int, end: int) -> dict:
            return {"sheetId": sid, "startColumnIndex": start, "endColumnIndex": end}

        def _range(r0: int, r1: int, c0: int, c1: int) -> dict:
            return _grid_range(sid, r0, r1, c0, c1)

        header_fmt = {
            "backgroundColor": {"red": 0.20, "green": 0.26, "blue": 0.37},
            "horizontalAlignment": "LEFT",
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        }
        bold = {"userEnteredFormat": {"textFormat": {"bold": True}}}
        # Header row index (0-based) and a generous task-row span for validation.
        hr = C.HEADER_ROW - 1
        fr = C.FIRST_TASK_ROW - 1
        last = 1000
        # Generous widths so the grid USES the screen and the human can actually read
        # the Задача (A) and Итог (E) columns without clicking each cell. Targets a
        # ~1366px laptop (≈1170px of grid + row gutter). Data cells CLIP (below), so a
        # long line never grows the row height. Tries (F) is hidden, so its width is moot.
        widths = [380, 140, 105, 145, 400, 46]   # A..F = Задача/Спека/Статус/Обновлено/Итог/Попытки
        notes = {  # 0-based column -> hint shown on the header cell
            0: ("Ты пишешь: что сделать (одной фразой). "
                f"Или /<скилл> — запустить готовый скилл (каталог — вкладка {C.SKILLS_TAB})."),
            1: "Демон: id OpenSpec-change.",
            2: "Ты ставишь: queued / retry / approved. Демон обновляет сам. Цвет = статус.",
            3: "Демон: время последнего обновления.",
            4: "Демон: итог или ошибка.",
            5: "Демон: число попыток (скрытая колонка).",
        }

        requests: list[dict] = [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"frozenRowCount": C.HEADER_ROW,
                                                  "frozenColumnCount": 1}},
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
            {"repeatCell": {
                "range": _range(hr, hr + 1, 0, C.COL_TRIES),
                "cell": {"userEnteredFormat": header_fmt},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}},
            # Bold the config/section labels A1 (REPO_PATH), C1 (BRANCH).
            {"repeatCell": {"range": _range(0, 1, 0, 1), "cell": bold,
                            "fields": "userEnteredFormat.textFormat.bold"}},
            {"repeatCell": {"range": _range(0, 1, 2, 3), "cell": bold,
                            "fields": "userEnteredFormat.textFormat.bold"}},
            # Status dropdown (non-strict: daemon-written values never get rejected).
            {"setDataValidation": {
                "range": _range(fr, last, C.COL_STATUS - 1, C.COL_STATUS),
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": s} for s in C.ALL_STATUSES]},
                         "showCustomUi": True, "strict": False}}},
            # Hide Tries (F): daemon-owned durable state, never human-facing.
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": C.COL_TRIES - 1, "endIndex": C.COL_TRIES},
                "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
            # Hide the config row (row 1): repo binding, branch and the daemon heartbeat
            # are plumbing the human doesn't touch day-to-day (the ➕ menu writes the
            # binding). Hiding leaves the tab as just headers + tasks — "таблица без
            # лишнего". The binding is still read by position (B1), so this is
            # display-only and changes nothing functional.
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": 1},
                "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
            # Log (E): CLIP, never WRAP — a long Log line must not grow the row's
            # height. Fixed-height data rows keep many tasks visible at once; the
            # full text stays in the cell (formula bar / on click).
            {"repeatCell": {
                "range": _range(fr, last, C.COL_LOG - 1, C.COL_LOG),
                "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP",
                                               "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        ]
        requests += [
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}}
            for i, w in enumerate(widths)
        ]
        requests += [
            {"repeatCell": {"range": _range(hr, hr + 1, c, c + 1),
                            "cell": {"note": note}, "fields": "note"}}
            for c, note in notes.items()
        ]
        # Hint on the repo-binding cell B1.
        requests.append({"repeatCell": {
            "range": _range(0, 1, 1, 2),
            "cell": {"note": "Привязка репозитория: выбери из списка (справочник "
                             f"{C.REPOS_TAB}) или впиши путь / git-url."},
            "fields": "note"}})
        # Colour-code the Status column. Clear any conditional-format rules already on
        # the sheet first (delete index 0 N times — each delete shifts the rest down),
        # then add the per-status rules, so re-running prettify never piles up duplicates.
        ncf = self._conditional_format_count(sid)
        requests += [{"deleteConditionalFormatRule": {"index": 0, "sheetId": sid}}
                     for _ in range(ncf)]
        requests += _status_cf_requests(sid)
        self._sh.batch_update({"requests": requests})
        # B1 dropdown from the _repos reference tab; harmless to skip if absent.
        try:
            self.set_repo_dropdown(title)
        except Exception as e:  # noqa: BLE001 — needs _repos tab; cosmetic
            log.debug("repo dropdown skipped on %r: %s", title, e)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def set_repo_dropdown(self, title: str) -> None:
        """Make B1 (repo binding) a dropdown sourced from the `_repos` tab, and
        highlight the cell so it's obvious you pick the repo here. Non-strict so a
        path/git-url can still be typed by hand."""
        ws = self._ws(title)
        b1 = {"sheetId": ws.id, "startRowIndex": C.CONFIG_ROW - 1,
              "endRowIndex": C.CONFIG_ROW, "startColumnIndex": 1, "endColumnIndex": 2}
        self._sh.batch_update({"requests": [
            {"setDataValidation": {
                "range": b1,
                "rule": {"condition": {"type": "ONE_OF_RANGE",
                                       "values": [{"userEnteredValue": f"='{C.REPOS_TAB}'!$A$2:$A"}]},
                         "showCustomUi": True, "strict": False}}},
            # Soft-yellow fill + border = "выбери репо здесь".
            {"repeatCell": {
                "range": b1,
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.70},
                    "textFormat": {"bold": True}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            {"updateBorders": {
                "range": b1,
                "top": {"style": "SOLID", "color": {"red": 0.85, "green": 0.65, "blue": 0.0}},
                "bottom": {"style": "SOLID", "color": {"red": 0.85, "green": 0.65, "blue": 0.0}},
                "left": {"style": "SOLID", "color": {"red": 0.85, "green": 0.65, "blue": 0.0}},
                "right": {"style": "SOLID", "color": {"red": 0.85, "green": 0.65, "blue": 0.0}}}},
        ]})

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def ensure_repos_tab(self, repos: list) -> None:
        """Create/refresh the `_repos` reference tab: a tidy, frozen table of
        selectable repos (name, path, whether it has openspec/)."""
        import gspread
        title = C.REPOS_TAB
        try:
            ws = self._sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(title=title, rows=max(50, len(repos) + 10), cols=3)
            self._ws_cache[title] = ws
        values = [["Репо", "Путь", "OpenSpec"]]
        values += [[r.name, str(r.path), "yes" if r.has_openspec else "no"] for r in repos]
        ws.clear()
        ws.update("A1", values)
        sid = ws.id
        header_fmt = {
            "backgroundColor": {"red": 0.20, "green": 0.26, "blue": 0.37},
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        }
        requests = [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": 3},
                "cell": {"userEnteredFormat": header_fmt},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        ]
        requests += [
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}}
            for i, w in enumerate([220, 460, 90])
        ]
        self._sh.batch_update({"requests": requests})

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def heartbeat(self, title: str, text: str) -> None:
        self._ws(title).update_cell(C.HEARTBEAT_ROW, C.HEARTBEAT_COL, text)

    # -- control intent queue -------------------------------------------------
    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def ensure_control_schema(self) -> None:
        """Create the `_control` worksheet if absent and write the frozen header
        `id|ts|action|args|status|result`. Idempotent + quota-friendly: once we've
        confirmed the header we cache and skip the read on later calls."""
        import gspread
        title = C.CONTROL_TAB
        if title in self._initialized:
            return
        try:
            ws = self._sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(title=title, rows=200, cols=len(C.CONTROL_HEADERS))
            self._ws_cache[title] = ws
        grid = ws.get_all_values()
        if _is_control_initialized(grid):
            self._initialized.add(title)
            return
        log.info("bootstrapping schema on control tab %r", title)
        ws.update("A1:F1", [C.CONTROL_HEADERS])
        sid = ws.id
        header_fmt = {
            "backgroundColor": {"red": 0.20, "green": 0.26, "blue": 0.37},
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        }
        try:
            self._sh.batch_update({"requests": [
                {"updateSheetProperties": {
                    "properties": {"sheetId": sid,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                              "startColumnIndex": 0,
                              "endColumnIndex": len(C.CONTROL_HEADERS)},
                    "cell": {"userEnteredFormat": header_fmt},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            ]})
        except Exception as e:  # noqa: BLE001 — formatting is cosmetic
            log.warning("control prettify failed (schema still ok): %s", e)
        self._initialized.add(title)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def read_control(self) -> list[ControlRow]:
        self.ensure_control_schema()
        return parse_control_grid(self._grid(C.CONTROL_TAB))

    # -- skills catalog -------------------------------------------------------
    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def ensure_skills_tab(self, skills: list | None = None) -> None:
        """Create + seed the `_skills` catalog ONCE (only when absent or header-less),
        then leave it alone — human prunes/edits are never clobbered. Idempotent +
        quota-friendly: once we've confirmed the header we cache and skip the read."""
        import gspread
        title = C.SKILLS_TAB
        if title in self._initialized:
            return
        catalog = list(skills) if skills is not None else list(C.DEFAULT_SKILLS)
        try:
            ws = self._sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(title=title, rows=max(50, len(catalog) + 10),
                                        cols=len(C.SKILLS_HEADERS))
            self._ws_cache[title] = ws
        grid = ws.get_all_values()
        if _is_skills_initialized(grid):
            # Seed-once already happened; never re-seed (would clobber curated
            # prompts/prunes). Just migrate the layout (Russian header + `Запуск`
            # column) in place — idempotent, leaves curated A/B/C text untouched.
            try:
                self._migrate_skills_layout(ws, grid)
            except Exception as e:  # noqa: BLE001 — migration is best-effort/cosmetic
                log.warning("skills layout migration failed (catalog still ok): %s", e)
            self._initialized.add(title)
            return
        log.info("seeding skills catalog %r with %d skills", title, len(catalog))
        values = [C.SKILLS_HEADERS]
        values += [[s.name, s.description, s.prompt, C.skill_trigger(s.name)]
                   for s in catalog]
        ws.update("A1", values)
        sid = ws.id
        header_fmt = {
            "backgroundColor": {"red": 0.20, "green": 0.26, "blue": 0.37},
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        }
        try:
            requests = [
                {"updateSheetProperties": {
                    "properties": {"sheetId": sid,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                              "startColumnIndex": 0,
                              "endColumnIndex": len(C.SKILLS_HEADERS)},
                    "cell": {"userEnteredFormat": header_fmt},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
                {"repeatCell": {  # CLIP the prompt column: a long playbook must not grow
                                  # the row, so more skills stay visible (full prompt
                                  # stays in the cell / formula bar).
                    "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 1000,
                              "startColumnIndex": C.COL_SKILL_PROMPT - 1,
                              "endColumnIndex": C.COL_SKILL_PROMPT},
                    "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP",
                                                   "verticalAlignment": "TOP"}},
                    "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
            ]
            requests += [
                {"updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS",
                              "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": w}, "fields": "pixelSize"}}
                for i, w in enumerate([160, 360, 520, 90])
            ]
            self._sh.batch_update({"requests": requests})
        except Exception as e:  # noqa: BLE001 — formatting is cosmetic
            log.warning("skills prettify failed (catalog still ok): %s", e)
        self._initialized.add(title)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def _migrate_skills_layout(self, ws, grid: list[list[str]]) -> None:
        """Bring an already-seeded `_skills` catalog from the old English header
        (`Skill, Description, Prompt`) to the Russian 4-column one and fill the visible
        `Запуск` trigger column — WITHOUT re-seeding rows or clobbering curated
        Описание/Промпт text. Idempotent: a no-op once the `Запуск` header is present."""
        if _cell(grid, C.SKILLS_HEADER_ROW, C.COL_SKILL_RUN) == C.SKILLS_HEADERS[-1]:
            return  # already migrated
        # An old 3-column catalog has no column D yet — grow the grid before writing it.
        if ws.col_count < C.COL_SKILL_RUN:
            ws.add_cols(C.COL_SKILL_RUN - ws.col_count)
        # Russianise the header row (A1:D1). This never touches data rows.
        ws.update(f"A{C.SKILLS_HEADER_ROW}:D{C.SKILLS_HEADER_ROW}", [C.SKILLS_HEADERS])
        # Fill the `Запуск` column for each catalog row that has a name but no trigger
        # yet — a single column write so curation in A/B/C is left exactly as-is.
        last = len(grid)
        run_col: list[list[str]] = []
        for r in range(C.SKILLS_FIRST_ROW, last + 1):
            name = _cell(grid, r, C.COL_SKILL_NAME)
            existing = _cell(grid, r, C.COL_SKILL_RUN)
            run_col.append([existing or (C.skill_trigger(name) if name else "")])
        if run_col:
            ws.update(f"D{C.SKILLS_FIRST_ROW}:D{last}", run_col)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def read_skills(self) -> list[Skill]:
        self.ensure_skills_tab()
        return parse_skills_grid(self._ws(C.SKILLS_TAB).get_all_values())

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def sync_skills(self) -> list[str]:
        """Operator-invoked top-up: append any `DEFAULT_SKILLS` whose name is absent
        from the catalog, leaving every existing row (and its curated text) untouched.
        Returns the names appended. Unlike the seed-once `ensure_skills_tab`, this is
        an EXPLICIT action — it re-adds skills that became available after the tab was
        first seeded; it never overwrites a curated row and never re-adds a name that
        is already present (so a row an operator edited stays as-is)."""
        self.ensure_skills_tab()
        ws = self._ws(C.SKILLS_TAB)
        existing = {s.name for s in parse_skills_grid(ws.get_all_values())}
        missing = [s for s in C.DEFAULT_SKILLS if s.name not in existing]
        if not missing:
            return []
        ws.append_rows(
            [[s.name, s.description, s.prompt, C.skill_trigger(s.name)] for s in missing],
            value_input_option="RAW")
        return [s.name for s in missing]

    # -- friends registry -----------------------------------------------------
    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def ensure_friends_schema(self) -> None:
        """Create the `_friends` registry tab if absent and stamp the frozen header
        `sheet_id|repos|recipient|autonomy|link`. Idempotent + quota-friendly: once
        confirmed we cache and skip the read, mirroring `ensure_control_schema`."""
        import gspread
        title = C.FRIENDS_TAB
        if title in self._initialized:
            return
        try:
            ws = self._sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(title=title, rows=200, cols=len(C.FRIENDS_HEADERS))
            self._ws_cache[title] = ws
        grid = ws.get_all_values()
        if _is_friends_initialized(grid):
            self._initialized.add(title)
            return
        log.info("bootstrapping schema on friends tab %r", title)
        ws.update("A1:E1", [C.FRIENDS_HEADERS])
        sid = ws.id
        header_fmt = {
            "backgroundColor": {"red": 0.20, "green": 0.26, "blue": 0.37},
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        }
        try:
            requests = [
                {"updateSheetProperties": {
                    "properties": {"sheetId": sid,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                              "startColumnIndex": 0,
                              "endColumnIndex": len(C.FRIENDS_HEADERS)},
                    "cell": {"userEnteredFormat": header_fmt},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            ]
            requests += [
                {"updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS",
                              "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": w}, "fields": "pixelSize"}}
                for i, w in enumerate([280, 320, 220, 90, 360])
            ]
            self._sh.batch_update({"requests": requests})
        except Exception as e:  # noqa: BLE001 — formatting is cosmetic
            log.warning("friends prettify failed (registry still ok): %s", e)
        self._initialized.add(title)

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def read_friends(self) -> list[Friend]:
        self.ensure_friends_schema()
        return parse_friends_grid(self._grid(C.FRIENDS_TAB))

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def append_friend(self, sheet_id: str, repos: list[str], recipient: str,
                      autonomy: str, link: str) -> None:
        """Append one registry row to `_friends`. The allowlist is stored newline-
        joined (round-trips through `_split_allowlist`)."""
        self.ensure_friends_schema()
        self._ws(C.FRIENDS_TAB).append_row(
            [sheet_id, "\n".join(repos), recipient, autonomy, link],
            value_input_option="RAW")

    # -- Drive seam: mint + share a new spreadsheet ---------------------------
    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def create_spreadsheet(self, title: str) -> tuple[str, str]:
        """Mint a brand-new spreadsheet under the service account, returning
        `(sheet_id, url)`. The file is owned by the SA; the caller shares it back to
        the owner + recipient. Used only by the `share_repos` flow."""
        sh = self._gc.create(title)
        return sh.id, f"https://docs.google.com/spreadsheets/d/{sh.id}/edit"

    @retry(stop=stop_after_attempt(5),
           wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
    def share_spreadsheet(self, sheet_id: str, email: str, role: str = "writer") -> None:
        """Share a minted spreadsheet with `email` at `role` (writer by default)."""
        self._gc.open_by_key(sheet_id).share(email, perm_type="user", role=role,
                                             notify=False)


# --------------------------------------------------------------------------
# Mock backend (local JSON)
# --------------------------------------------------------------------------
class MockBackend:
    """A local JSON file that mimics a multi-tab sheet. Used for dry-run."""

    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized: set[str] = set()
        # Per-cycle snapshot (parity with GoogleBackend): None → reads go live.
        self._grid_cache: dict[str, list[list[str]]] | None = None
        if not self.path.exists():
            self._save({"tabs": {}})

    def _load(self) -> dict:
        return json.loads(self.path.read_text() or '{"tabs": {}}')

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _grid(self, tab: dict) -> list[list[str]]:
        return tab.setdefault("grid", [])

    # -- per-cycle batched snapshot (parity with GoogleBackend) ----------------
    def read_all(self) -> dict[str, list[list[str]]]:
        return {t: v.get("grid", []) for t, v in self._load()["tabs"].items()}

    def begin_cycle(self) -> None:
        self._grid_cache = self.read_all()

    def end_cycle(self) -> None:
        self._grid_cache = None

    def _grid_cells(self, title: str) -> list[list[str]]:
        """The tab's grid from this cycle's snapshot if active, else live from JSON."""
        if self._grid_cache is not None and title in self._grid_cache:
            return self._grid_cache[title]
        return self._load()["tabs"].get(title, {}).get("grid", [])

    def list_tab_titles(self) -> list[str]:
        if self._grid_cache is not None:
            return list(self._grid_cache.keys())
        return list(self._load()["tabs"].keys())

    def read_tab(self, title: str) -> Tab:
        if title not in self._initialized:
            self.ensure_schema(title)
            self._initialized.add(title)
        return parse_grid(title, self._grid_cells(title))

    def write_cell(self, title: str, row: int, col: int, value: str) -> None:
        data = self._load()
        tab = data["tabs"].setdefault(title, {"grid": []})
        grid = tab.setdefault("grid", [])
        while len(grid) < row:
            grid.append([])
        r = grid[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = value
        self._save(data)

    def write_cells(self, title: str, cells: list[tuple[int, int, str]]) -> None:
        """Parity with GoogleBackend.write_cells — applies each cell to the JSON grid."""
        for r, c, v in cells:
            self.write_cell(title, r, c, v)

    def write_note(self, title: str, row: int, col: int, text: str) -> None:
        """Parity with GoogleBackend.write_note — store the cell note in the JSON."""
        data = self._load()
        tab = data["tabs"].setdefault(title, {"grid": []})
        tab.setdefault("notes", {})[f"{row}:{col}"] = text
        self._save(data)

    def get_note(self, title: str, row: int, col: int) -> str:
        """Test helper: read back a cell note (not part of the Backend protocol)."""
        return self._load()["tabs"].get(title, {}).get("notes", {}).get(f"{row}:{col}", "")

    def delete_column(self, title: str, col: int) -> None:
        """Parity with GoogleBackend.delete_column — drop the 1-indexed column from each
        grid row, shifting the rest left (used by the drop-Detail layout migration)."""
        data = self._load()
        tab = data["tabs"].setdefault(title, {"grid": []})
        idx = col - 1
        for row in tab.setdefault("grid", []):
            if len(row) > idx:
                del row[idx]
        self._save(data)

    def delete_row(self, title: str, row: int) -> None:
        """Parity with GoogleBackend.delete_row — drop the 1-indexed row from the grid,
        shifting the rest up (used by the drop-vision-row layout migration)."""
        data = self._load()
        tab = data["tabs"].setdefault(title, {"grid": []})
        grid = tab.setdefault("grid", [])
        idx = row - 1
        if len(grid) > idx:
            del grid[idx]
        self._save(data)

    def create_tab(self, title: str) -> None:
        """Create an empty tab if absent; a second call is a no-op (the grid is left
        untouched). Behavioural parity with GoogleBackend.create_tab."""
        data = self._load()
        data["tabs"].setdefault(title, {"grid": []})
        self._save(data)

    def ensure_schema(self, title: str, grid: list[list[str]] | None = None) -> None:
        data = self._load()
        tab = data["tabs"].setdefault(title, {"grid": []})
        grid = tab.get("grid", [])
        # Same MANDATORY migrations as GoogleBackend: drop the old Product Vision row
        # (row 2) FIRST so the header realigns to row 2, then drop the removed "Detail"
        # column (D) and the removed "Priority" column (G) on a tab still under an older
        # layout — all on already-initialized tabs.
        _migrate_drop_vision_row(self, title, grid)
        _migrate_drop_detail_column(self, title, grid)
        _migrate_drop_priority_column(self, title, grid)
        # Russianise old English labels/header in place (idempotent). Mock has no
        # formatting, so there is nothing to re-prettify — the relabel itself is enough.
        _migrate_russianize(self, title, grid)
        # Chat no longer lives on the repo tab (it has its own paired `_chat <repo>`
        # tab — see ensure_chat_schema); the repo tab's schema stamps nothing in J/K.
        if _is_initialized(grid):
            return
        self.write_cell(title, C.CONFIG_ROW, 1, C.CONFIG_LABEL_REPO)
        self.write_cell(title, C.CONFIG_ROW, 3, C.CONFIG_LABEL_BRANCH)
        for i, h in enumerate(C.HEADERS, start=1):
            self.write_cell(title, C.HEADER_ROW, i, h)

    def read_chat_tab(self, title: str) -> Tab:
        """Parity with GoogleBackend.read_chat_tab: bootstrap the chat schema on first
        read, then parse the chat layout (B1 binding, A2 compose box, A4:B transcript)."""
        key = f"chat::{title}"
        if key not in self._initialized:
            self.ensure_chat_schema(title)
            self._initialized.add(key)
        return parse_chat_grid(title, self._grid_cells(title))

    def ensure_chat_schema(self, title: str, grid: list[list[str]] | None = None) -> None:
        """Parity with GoogleBackend.ensure_chat_schema: stamp the A1 label + A3/B3
        headers and seed the A2 placeholder when empty (never clobbering a typed
        question). The B1 binding is written by the provisioning path, not here."""
        data = self._load()
        tab = data["tabs"].setdefault(title, {"grid": []})
        grid = tab.get("grid", [])
        if _cell(grid, C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL) == "":
            self.write_cell(title, C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL, C.CHAT_INPUT_PLACEHOLDER)
        if _cell(grid, C.CONFIG_ROW, 1) == _OLD_LABEL_REPO:
            self.write_cell(title, C.CONFIG_ROW, 1, C.CONFIG_LABEL_REPO)
        if _is_chat_initialized(grid):
            return
        self.write_cell(title, C.CONFIG_ROW, 1, C.CONFIG_LABEL_REPO)
        self.write_cell(title, C.CHAT_HEADER_ROW, C.COL_CHAT_Q, C.CHAT_HEADERS[0])
        self.write_cell(title, C.CHAT_HEADER_ROW, C.COL_CHAT_A, C.CHAT_HEADERS[1])

    def prettify(self, title: str) -> None:
        """No-op: local JSON has no formatting. Kept for backend parity."""

    def set_repo_dropdown(self, title: str) -> None:
        """No-op: local JSON has no data validation."""

    def ensure_repos_tab(self, repos: list) -> None:
        """Store the discovered repo names as a plain tab so mock runs see it."""
        data = self._load()
        grid = [["Репо", "Путь", "OpenSpec"]]
        grid += [[r.name, str(r.path), "yes" if r.has_openspec else "no"] for r in repos]
        data["tabs"][C.REPOS_TAB] = {"grid": grid}
        self._save(data)

    def heartbeat(self, title: str, text: str) -> None:
        self.write_cell(title, C.HEARTBEAT_ROW, C.HEARTBEAT_COL, text)

    # -- control intent queue (parity with GoogleBackend) ---------------------
    def ensure_control_schema(self) -> None:
        """Create the `_control` tab (header row only) if absent. Idempotent: a
        second call sees the header already present and adds nothing — no data
        rows, no duplicate header."""
        data = self._load()
        tab = data["tabs"].setdefault(C.CONTROL_TAB, {"grid": []})
        if _is_control_initialized(tab.get("grid", [])):
            return
        for i, h in enumerate(C.CONTROL_HEADERS, start=1):
            self.write_cell(C.CONTROL_TAB, C.CONTROL_HEADER_ROW, i, h)

    def read_control(self) -> list[ControlRow]:
        self.ensure_control_schema()
        return parse_control_grid(self._grid_cells(C.CONTROL_TAB))

    # -- skills catalog (parity with GoogleBackend) ---------------------------
    def ensure_skills_tab(self, skills: list | None = None) -> None:
        """Create + seed the `_skills` catalog ONCE (only when absent/header-less).
        A second call sees the header and adds nothing — prunes/edits are never
        clobbered. Behavioural parity with GoogleBackend.ensure_skills_tab."""
        data = self._load()
        tab = data["tabs"].setdefault(C.SKILLS_TAB, {"grid": []})
        existing = tab.get("grid", [])
        if _is_skills_initialized(existing):
            # Seed-once already happened: migrate the layout in place (Russian header +
            # `Запуск` column) without re-seeding or clobbering curated A/B/C. Idempotent.
            if _cell(existing, C.SKILLS_HEADER_ROW, C.COL_SKILL_RUN) != C.SKILLS_HEADERS[-1]:
                for i, h in enumerate(C.SKILLS_HEADERS, start=1):
                    self.write_cell(C.SKILLS_TAB, C.SKILLS_HEADER_ROW, i, h)
                for r in range(C.SKILLS_FIRST_ROW, len(existing) + 1):
                    name = _cell(existing, r, C.COL_SKILL_NAME)
                    if name and not _cell(existing, r, C.COL_SKILL_RUN):
                        self.write_cell(C.SKILLS_TAB, r, C.COL_SKILL_RUN,
                                        C.skill_trigger(name))
            return
        catalog = list(skills) if skills is not None else list(C.DEFAULT_SKILLS)
        grid = [C.SKILLS_HEADERS]
        grid += [[s.name, s.description, s.prompt, C.skill_trigger(s.name)]
                 for s in catalog]
        data["tabs"][C.SKILLS_TAB] = {"grid": grid}
        self._save(data)

    def read_skills(self) -> list[Skill]:
        self.ensure_skills_tab()
        data = self._load()
        tab = data["tabs"].get(C.SKILLS_TAB, {"grid": []})
        return parse_skills_grid(tab.get("grid", []))

    def sync_skills(self) -> list[str]:
        """Append any `DEFAULT_SKILLS` missing from the catalog (parity with
        GoogleBackend.sync_skills); curated rows are never touched."""
        self.ensure_skills_tab()
        data = self._load()
        tab = data["tabs"].setdefault(C.SKILLS_TAB, {"grid": []})
        grid = tab.setdefault("grid", [])
        existing = {s.name for s in parse_skills_grid(grid)}
        missing = [s for s in C.DEFAULT_SKILLS if s.name not in existing]
        for s in missing:
            grid.append([s.name, s.description, s.prompt, C.skill_trigger(s.name)])
        if missing:
            self._save(data)
        return [s.name for s in missing]

    # -- friends registry (parity with GoogleBackend) -------------------------
    def ensure_friends_schema(self) -> None:
        """Create the `_friends` tab (header row only) if absent. Idempotent."""
        data = self._load()
        tab = data["tabs"].setdefault(C.FRIENDS_TAB, {"grid": []})
        if _is_friends_initialized(tab.get("grid", [])):
            return
        for i, h in enumerate(C.FRIENDS_HEADERS, start=1):
            self.write_cell(C.FRIENDS_TAB, C.FRIENDS_HEADER_ROW, i, h)

    def read_friends(self) -> list[Friend]:
        self.ensure_friends_schema()
        return parse_friends_grid(self._grid_cells(C.FRIENDS_TAB))

    def append_friend(self, sheet_id: str, repos: list[str], recipient: str,
                      autonomy: str, link: str) -> None:
        """Append one registry row after the last used row (parity with
        GoogleBackend.append_friend; allowlist stored newline-joined)."""
        self.ensure_friends_schema()
        data = self._load()
        tab = data["tabs"].setdefault(C.FRIENDS_TAB, {"grid": []})
        grid = tab.setdefault("grid", [])
        row = len(grid) + 1
        for col, val in enumerate(
                [sheet_id, "\n".join(repos), recipient, autonomy, link], start=1):
            self.write_cell(C.FRIENDS_TAB, row, col, val)


def make_backend(cfg: C.Config) -> Backend:
    if cfg.backend == "mock":
        log.info("using MOCK sheet backend at %s", cfg.mock_path)
        return MockBackend(cfg.mock_path)
    log.info("using GOOGLE sheet backend (sheet_id=%s)", cfg.sheet_id)
    return GoogleBackend(cfg.sheet_id, cfg.sa_json)


def make_friend_backend(cfg: C.Config, sheet_id: str) -> Backend:
    """A backend pinned to a registered friend sheet (Stage 2 multi-sheet polling).
    Same kind as the master backend, just a different spreadsheet id. For the mock
    backend each friend sheet is a sibling JSON file so a dry-run can simulate the
    multi-sheet loop without Google."""
    if cfg.backend == "mock":
        p = Path(cfg.mock_path).expanduser().parent / f"friend-{sheet_id}.json"
        return MockBackend(str(p))
    return GoogleBackend(sheet_id, cfg.sa_json)
