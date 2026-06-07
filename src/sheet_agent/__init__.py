"""Autonomous agent orchestrator driven by a Google Sheet control plane.

Each worksheet (tab) in the sheet binds to one git repository. Rows are
human-authored tasks. A robust, self-restarting supervisor polls the sheet,
dispatches short-lived `claude -p` coding agents (spec via OpenSpec, then
implement → test → commit → push → deploy), and writes statuses back.

Only repositories that use OpenSpec are eligible for work.
"""

__version__ = "0.1.0"
