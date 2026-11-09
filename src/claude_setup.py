#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: claude_setup.py
ROLE: Point Claude Desktop / Claude Code at this StoryCraft (the connector in mcp_server/)
DESCRIPTION:
StoryCraft starts its connector by itself, but Claude only reaches it once its config names
mcp_server/storycraft_mcp.py of THIS install. This module says whether that is the case and fixes it:
  - Claude Desktop: adds or repoints "storycraft" in claude_desktop_config.json (backup first, the
    rest of the file left as it was; a file that isn't valid JSON is never overwritten);
  - Claude Code: the `claude mcp add` command to paste in a terminal (and whether ~/.claude.json has it).
No GUI here (gui/views/ai_view.py shows it).
"""
import json
import os
import shlex
import sys
import time

NAME = "storycraft"


def server_command(install_dir):
    """(python, script) Claude must run. The script is standard library only; the app's venv python is used."""
    script = os.path.join(install_dir, "mcp_server", "storycraft_mcp.py")
    if sys.platform == "win32":
        py = os.path.join(install_dir, "venv", "Scripts", "python.exe")
    else:
        py = os.path.join(install_dir, "venv", "bin", "python3")
    if not os.path.exists(py):
        py = sys.executable
    return py, script


def desktop_config_path():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "Claude")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"), "Claude")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "Claude")
    return os.path.join(base, "claude_desktop_config.json")


def _entry_state(entry, install_dir):
    if not isinstance(entry, dict):
        return "missing"
    args = entry.get("args") or []
    _, script = server_command(install_dir)
    norm = lambda p: os.path.normcase(os.path.realpath(p))
    if any(isinstance(a, str) and norm(a) == norm(script) for a in args):
        return "ok"
    return "other"          # points at another copy (an older install, a dev folder...)


def desktop_state(install_dir):
    """'no_app' (Claude Desktop not installed), 'missing', 'other', 'ok' or 'bad_file' (not valid JSON)."""
    path = desktop_config_path()
    if not os.path.isdir(os.path.dirname(path)):
        return "no_app"
    if not os.path.exists(path):
        return "missing"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "bad_file"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return _entry_state((servers or {}).get(NAME), install_dir)


def connect_desktop(install_dir):
    """Add or repoint the entry. Returns (ok, backup_path_or_error)."""
    path = desktop_config_path()
    data, backup = {}, ""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw) if raw.strip() else {}
        except Exception as e:
            return False, f"invalid JSON: {e}"
        if not isinstance(data, dict):
            return False, "invalid JSON: not an object"
        backup = f"{path}.storycraft-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        with open(backup, "w", encoding="utf-8") as f:
            f.write(raw)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    py, script = server_command(install_dir)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = data["mcpServers"] = {}
    servers[NAME] = {"command": py, "args": [script]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return True, backup


def code_command(install_dir):
    py, script = server_command(install_dir)
    q = (lambda s: f'"{s}"') if sys.platform == "win32" else shlex.quote
    return f"claude mcp add -s user {NAME} -- {q(py)} {q(script)}"


def code_state(install_dir):
    """'missing', 'other' or 'ok' for the user-wide entry in ~/.claude.json."""
    path = os.path.join(os.path.expanduser("~"), ".claude.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "missing"
    return _entry_state((data.get("mcpServers") or {}).get(NAME), install_dir)
