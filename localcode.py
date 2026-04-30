#!/usr/bin/env python3
from __future__ import annotations

import ast
import datetime
import difflib
import fnmatch
import fcntl
import glob
import json

import sqlite3
import math
import os
import platform
import re
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from re import Match
from typing import Any, Dict, Final, List, Optional, Set, Tuple, TypedDict, Union

# TypedDict definitions for better type hints
class SystemInfo(TypedDict, total=False):
    """System information dictionary."""
    os: str
    release: str
    machine: str
    python: str
    cwd: str
    shell: str
    path: str
    venv: bool
    tools: List[str]
    versions: Dict[str, str]
    distro: str
    distro_version: str
    distro_id: str
    windows_edition: str
    windows_version: str
    memory_total_gb: float
    memory_available_gb: float
    cpu_cores: int
    disk_total_gb: float
    disk_free_gb: float
    venv_path: str
    venv_python_binary: str
    venv_python_version: str
    local_venv_path: str
    local_venv_python_binary: str
    local_venv_python_version: str
    user: str
    language: str
    locale: str
    filesystem_encoding: str
    string_encoding: str

class Message(TypedDict, total=False):
    """Chat message for llama.cpp API."""
    role: str
    content: str
    tool_calls: List[Dict[str, Any]]

class ToolCall(TypedDict, total=False):
    """Tool call from llama.cpp response."""
    id: str
    type: str
    function: Dict[str, Any]

class ToolResult(TypedDict, total=False):
    """Result from tool execution."""
    ok: bool
    error: str
    path: str
    file_count: int
    message: str
    git: str
    output: List[str]
    exit_code: int
    result: Any
    url: str
    title: str
    valid_syntax: bool
    body: str
    skill_name: str
    denied: bool
    classification: str
    is_new_host: bool
    interactive: bool
    host: str
    command: str
    approval_type: str
    user_approved: bool

class Skill:
    """A skill discovered from a SKILL.md file."""
    def __init__(self, name: str, description: str, body: str, path: Path,
                 disable_model_invocation: bool = False,
                 user_invocable: bool = True) -> None:
        self.name = name
        self.description = description
        self.body = body
        self.path = path
        self.disable_model_invocation = disable_model_invocation
        self.user_invocable = user_invocable

    def __repr__(self) -> str:
        return f"Skill({self.name!r})"

class PythonElement(TypedDict):
    """Python AST element with location info."""
    name: str
    type: str
    start_line: int
    end_line: int

class LlamaResponse(TypedDict, total=False):
    """llama.cpp chat completion response."""
    choices: List[Dict[str, Any]]
    usage: Dict[str, int]
    timings: Dict[str, Any]

# Version and app constants
VERSION: Final[int] = 3
APP_NAME: Final[str] = "localcode"

# Configuration
LLAMA_HOST: str = os.getenv("LLAMA_HOST", "http://localhost:8080")
MODEL: str = os.getenv("LLAMA_MODEL", "local-model")  # Model name (for display, llama.cpp uses loaded model)
MAX_FILE_SIZE: Final[int] = 256 * 1024  # 256KB
MAX_LINE_LENGTH: Final[int] = 500
MAX_TOOL_LOOPS: Final[int] = 50
DEFAULT_BRIDGE_PORT: Final[int] = 9876
GIT_COMMAND_TIMEOUT: Final[int] = 30  # seconds
BROWSER_EXECUTE_TIMEOUT: Final[int] = 12  # seconds
BROWSER_COMMAND_TIMEOUT: Final[int] = 30  # seconds
HTTP_REQUEST_TIMEOUT: Final[int] = 600  # seconds
# Optional: temperature and other generation params
TEMPERATURE: float = float(os.getenv("LLAMA_TEMPERATURE", "0.7"))
MAX_TOKENS: int = int(os.getenv("LLAMA_MAX_TOKENS", "40000"))

TOOLS: Final[List[Dict[str, Any]]] = [
    {
        "type": "function",
        "name": "get_repo_map",
        "description": "Get a repository map showing all files and their structure. For Python files, includes function/class locations with line numbers. Call once without a pattern to see the complete repository. Use pattern parameter only to filter for specific files when needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Optional glob pattern to filter files (e.g., '*.py', 'src/*', 'tests/*'). Omit this parameter or leave empty to show ALL files in the repository.",
                },
                "include_details": {
                    "type": "boolean",
                    "description": "If true, include line numbers for Python functions/classes. Default: true.",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write content to a file. Creates new files or overwrites existing ones (with confirmation). Python files (.py) are syntax-checked before writing — invalid syntax will be rejected.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path"},
                "content": {"type": "string", "description": "Full file contents"},
                "overwrite": {"type": "boolean", "description": "If true, overwrite existing file. Default: false (will fail if file exists)."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": "Apply one exact find/replace edit to an existing text file. The find text must match exactly, including whitespace. Use shortest unique non-regex snippet. Python files (.py) are syntax-checked after the edit — invalid syntax will be rejected.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path"},
                "find": {"type": "string", "description": "Exact text to replace"},
                "replace": {"type": "string", "description": "Replacement text; empty string deletes the match"},
            },
            "required": ["path", "find", "replace"],
        },
    },
    {
        "type": "function",
        "name": "run_shell_command",
        "description": "Run one shell command locally. The user will be asked to approve it first. Output is truncated to 500 lines with 500 characters per line.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "A single shell command"},
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "commit_changes",
        "description": "Create a git commit for all current changes with a concise commit message.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Git commit message"},
            },
            "required": ["message"],
        },
    },
    {
        "type": "function",
        "name": "browser_execute",
        "description": "Execute JavaScript in the currently active browser tab. Returns captured console logs and result. Use for debugging, inspection, or navigation (window.location = '...'). Always targets the active tab.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "JavaScript code to execute"}
            },
            "required": ["code"],
        },
    },
    {
        "type": "function",
        "name": "ssh_command",
        "description": "Run a shell command on a remote server via SSH. Uses existing SSH configuration (~/.ssh/config). First connection to a new host requires user confirmation. SSH keys and host setup should be configured beforehand. For commands requiring password input (e.g., sudo), set interactive=true to enable PTY-based interactive mode where you can enter passwords when prompted. The agent will still receive all command output.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "SSH host or alias from ~/.ssh/config (e.g., 'meeseeks1' or 'datatalk@meeseeks1')"},
                "command": {"type": "string", "description": "Command to execute remotely"},
                "user": {"type": "string", "description": "SSH username (optional, overrides host specification if provided)"},
                "interactive": {"type": "boolean", "description": "If true, enables interactive PTY mode for password prompts (e.g., sudo). You can enter passwords when prompted, and the agent will still receive all output. Default: false.", "default": False},
            },
            "required": ["host", "command"],
        },
    },
    {
        "type": "function",
        "name": "invoke_skill",
        "description": "Load a skill to extend capabilities. Skills provide detailed instructions for specific tasks. The user must approve before the skill is loaded. Use this when the task matches a skill's description from the Available Skills list.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name (without the / prefix)"},
                "arguments": {"type": "string", "description": "Arguments to pass to the skill (will be substituted as $ARGUMENTS, $0, $1, etc.)"},
            },
            "required": ["name"],
        },
    },
]

# AGENTS.md content (loaded at startup)
_AGENTS_MD_CONTENT: str = ""
_AGENTS_MD_PATH: Optional[Path] = None

# CLAUDE.md content (loaded at startup)
_CLAUDE_MD_CONTENT: str = ""
_CLAUDE_MD_PATH: Optional[Path] = None

# --- Skill discovery and parsing ---

def _parse_skill_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Parse YAML frontmatter from a SKILL.md file using regex.

    Returns:
        Tuple of (frontmatter dict, body text).
    """
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if not m:
        return {}, text.strip()

    raw = m.group(1)
    body = m.group(2).strip()
    fm: Dict[str, str] = {}
    for line in raw.splitlines():
        if ':' not in line:
            continue
        key, _, val = line.partition(':')
        fm[key.strip()] = val.strip()
    return fm, body

def _substitute_arguments(body: str, arguments: str) -> str:
    """Substitute $ARGUMENTS, $0, $1, etc. in skill body text."""
    # Split arguments by whitespace (respecting quotes)
    parts = re.findall(r'"[^"]*"|\'[^\']*\'|(\S+)', arguments)
    args = [p.strip('"\'') for p in parts]

    # Replace $N first (before $ARGUMENTS to avoid double-substitution)
    for i, arg in enumerate(args):
        body = body.replace(f"${i}", arg)
        body = body.replace(f"$ARGUMENTS[{i}]", arg)

    # Replace $ARGUMENTS with full string
    body = body.replace("$ARGUMENTS", arguments)
    return body

def _discover_skills() -> List[Skill]:
    """Discover skills from personal and project directories.

    Project skills override personal skills with the same name.
    """
    personal_dir = Path.home() / ".localcode" / "skills"
    project_dirs = [Path(".localcode") / "skills", Path(".agents") / "skills"]

    skills: Dict[str, Skill] = {}

    # Load personal skills first (lower priority)
    if personal_dir.is_dir():
        for skill_file in sorted(personal_dir.rglob("SKILL.md")):
            skill = _parse_skill_file(skill_file, skill_file.parent.name)
            if skill:
                skills[skill.name] = skill

    # Load project skills (override personal)
    for project_dir in project_dirs:
        if project_dir.is_dir():
            for skill_file in sorted(project_dir.rglob("SKILL.md")):
                skill = _parse_skill_file(skill_file, skill_file.parent.name)
                if skill:
                    skills[skill.name] = skill

    return list(skills.values())

def _parse_skill_file(path: Path, default_name: str) -> Optional[Skill]:
    """Parse a single SKILL.md file into a Skill object."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    fm, body = _parse_skill_frontmatter(text)
    name = fm.get("name", default_name).lower().replace("_", "-")
    description = fm.get("description", "")
    if not description and body:
        # Use first paragraph as description
        desc_match = re.match(r'^(.*?)(\n\n|\Z)', body)
        description = desc_match.group(1).strip() if desc_match else ""

    disable_model = fm.get("disable-model-invocation", "false").lower() == "true"
    user_invocable = fm.get("user-invocable", "true").lower() != "false"

    return Skill(
        name=name,
        description=description,
        body=body,
        path=path,
        disable_model_invocation=disable_model,
        user_invocable=user_invocable,
    )

def _build_skills_index(skills: List[Skill]) -> str:
    """Build a markdown index of available skills for the system prompt."""
    if not skills:
        return ""

    lines = ["## Available Skills", ""]
    for skill in skills:
        if skill.disable_model_invocation:
            continue  # Hide from model's context
        desc = skill.description or "(no description)"
        lines.append(f"- **/{skill.name}**: {desc}")
    lines.append("")
    lines.append("To invoke a skill, use the `invoke_skill` tool with the skill name (without the /) and any arguments.")
    lines.append("")
    return "\n".join(lines)

def _load_agents_md() -> Tuple[str, Optional[Path]]:
    """Load AGENTS.md content from project and/or user directories.

    Checks in order:
      1. Current directory (project-level)
      2. ~/.localcode/AGENTS.md (user-level)

    Returns:
        Tuple of (content, path) if found, or ("", None) if not found.
    """
    # Project-level AGENTS.md
    project_path = Path("AGENTS.md")
    if project_path.exists():
        try:
            return project_path.read_text(encoding="utf-8").strip(), project_path
        except Exception:
            pass

    # User-level AGENTS.md
    user_path = Path.home() / ".localcode" / "AGENTS.md"
    if user_path.exists():
        try:
            return user_path.read_text(encoding="utf-8").strip(), user_path
        except Exception:
            pass

    return "", None


def _load_claude_md() -> Tuple[str, Optional[Path]]:
    """Load CLAUDE.md content from project and/or user directories.

    Checks in order:
      1. Current directory (project-level)
      2. ~/.localcode/CLAUDE.md (user-level)

    Returns:
        Tuple of (content, path) if found, or ("", None) if not found.
    """
    # Project-level CLAUDE.md
    project_path = Path("CLAUDE.md")
    if project_path.exists():
        try:
            return project_path.read_text(encoding="utf-8").strip(), project_path
        except Exception:
            pass

    # User-level CLAUDE.md
    user_path = Path.home() / ".localcode" / "CLAUDE.md"
    if user_path.exists():
        try:
            return user_path.read_text(encoding="utf-8").strip(), user_path
        except Exception:
            pass

    return "", None


_AGENTS_MD_CONTENT, _AGENTS_MD_PATH = _load_agents_md()
_CLAUDE_MD_CONTENT, _CLAUDE_MD_PATH = _load_claude_md()

SYSTEM_PROMPT: Final[str] = (
    "You are a coding expert working inside a local repository tool called 'localcode'.\n\n"
    "Use tools via function calls. Never output XML, markdown code blocks, or any other format for tool calls.\n\n"
    "Behavior rules:\n"
    "- Think step by step before deciding to use tools.\n"
    "- Answer normally when no tool is needed.\n"
    "- Use get_repo_map once at the start to get the complete repository overview (all files, with Python line numbers). Only use pattern filter if you need to focus on specific files.\n"
    "- Use shell commands (cat, grep, head, tail, wc) to read file contents or search for patterns. **For file modifications, always prefer edit_file first**. **edit_file has no size limitations** - use it regardless of edit size. Only fall back to sed or inline Python for modifications if edit_file truly cannot express the change. Note: shell command output is truncated to 500 lines with 500 characters per line.\n"
    "- Use write_file to create new files or overwrite existing ones (requires confirmation for overwrite).\n"
    "- Use edit_file for precise find/replace edits. Make the 'find' string as short and unique as possible.\n"
    "- **Both write_file and edit_file automatically validate Python syntax for .py files** — if syntax is invalid, the operation is rejected with an error. You do not need to manually check syntax after using these tools. The result will include `valid_syntax: true` on success.\n"
    "- Preserve original formatting, whitespace, and surrounding code style exactly.\n"
    "- If an edit's exact find text is not found, use cat/grep to find the correct text first.\n"
    "- If an edit's exact find text is not found, read the file again and use a more precise match.\n"
    "- Only run shell commands when genuinely necessary (use cat/grep for reading files).\n"
    "- After changes, call commit_changes with a short message if and only if files were actually modified.\n"
    "- Keep all user-facing answers concise.\n\n"
    "File operations:\n"
    "- get_repo_map: Shows ALL files in the repository. Python files include function/class line numbers. Non-Python files are listed without details. Excludes venv/, node_modules/, .git/, __pycache__/, data/. Call once without pattern for complete overview.\n"
    "- cat file.py: Read full file content. Use grep/sed to search within files. Use edit_file for modifications.\n"
    "- write_file: Create new files or overwrite (with confirmation). Use for full file rewrites. Python files are syntax-checked.\n"
    "- edit_file: Precise find/replace edits. Works for any edit size. Python files are syntax-checked.\n"
    "- Files in excluded directories (venv/, node_modules/, .git/, __pycache__/, data/, etc.) are not shown in repo map.\n\n"
    "Important: Use the provided tools via function calling. Do not output tool calls as raw text, JSON, or XML in your messages.\n\n"
    "When copying code from tool responses for edit_file, always use the exact raw text from inside the ``` blocks. "
    "Never use the escaped JSON version (with \\n or \\\"). Copy the literal file content only."
)

def get_system_prompt(skills: Optional[List[Skill]] = None) -> str:
    """Return the system prompt with AGENTS.md and skills index appended.

    Args:
        skills: Optional list of discovered skills to include in the prompt.

    Returns:
        Complete system prompt string.
    """
    prompt = SYSTEM_PROMPT
    if _AGENTS_MD_CONTENT:
        prompt += "\n\n### AGENTS.md\n" + _AGENTS_MD_CONTENT
    if _CLAUDE_MD_CONTENT:
        prompt += "\n\n### CLAUDE.md\n" + _CLAUDE_MD_CONTENT
    if skills:
        prompt += "\n\n" + _build_skills_index(skills)
    return prompt

def ansi(code: str) -> str:
    """Return ANSI escape sequence for terminal styling.

    Args:
        code: ANSI escape code (e.g., '1m' for bold, '31m' for red).

    Returns:
        Formatted ANSI escape string.
    """
    return f"\033[{code}"

def styled(text: str, style: str) -> str:
    """Wrap text with ANSI style codes for terminal output.

    Args:
        text: The text to style.
        style: ANSI style code (e.g., '1m' for bold, '32m' for green).

    Returns:
        Text wrapped with style codes and reset code.
    """
    return f"{ansi(style)}{text}{ansi('0m')}"

def run(shell_cmd: str) -> Optional[str]:
    """Run shell command and return stripped output or None on error.

    Args:
        shell_cmd: Shell command string to execute.

    Returns:
        Command output stripped of whitespace, or None if command fails.
    """
    try:
        return subprocess.check_output(
            shell_cmd, shell=True, text=True, stderr=subprocess.STDOUT
        ).strip()
    except Exception:
        return None

_TMUX_WIN: Optional[str] = run("tmux display-message -p '#{window_id}' 2>/dev/null")

def title(t: str) -> None:
    """Set terminal title and tmux window name if running in tmux.

    Args:
        t: Title string to set.
    """
    print(f"\033]0;{t}\007", end="", flush=True)
    if _TMUX_WIN:
        run(f"tmux rename-window -t {_TMUX_WIN} {t!r} 2>/dev/null")

def render_md(text: str) -> str:
    """Render markdown-like text with ANSI styling for terminal output.

    Supports:
    - Code blocks (```) with dark background
    - Inline code (`) with dark background
    - Links [text](url) as clickable terminal links
    - Headers (#, ##, ###) with yellow styling
    - Bold (**text**) and italic (*text* or _text_)

    Args:
        text: Markdown-formatted text to render.

    Returns:
        ANSI-styled string for terminal display.
    """
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    result: List[str] = []
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            inner = part[3:-3]
            if inner.startswith("\n"):
                inner = inner[1:]
            elif "\n" in inner:
                inner = inner.split("\n", 1)[1]
            inner_lines = inner.split("\n")
            inner = "\n".join(f"{line}{ansi('K')}" for line in inner_lines) + ansi("K")
            result.append(f"\n{ansi('48;5;236;37m')}{inner}{ansi('0m')}")
        elif part.startswith("`") and part.endswith("`"):
            result.append(f"{ansi('48;5;236m')}{part[1:-1]}{ansi('0m')}")
        else:
            part = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                lambda m: f"\033]8;;{m.group(2)}\033\\{ansi('4;34m')}{m.group(1)}{ansi('0m')}\033]8;;\033\\",
                part,
            )
            part = re.sub(r"\*\*(.+?)\*\*", lambda m: f"{ansi('1m')}{m.group(1)}{ansi('22m')}", part)
            part = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", lambda m: f"{ansi('3m')}{m.group(1)}{ansi('23m')}", part)
            part = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", lambda m: f"{ansi('3m')}{m.group(1)}{ansi('23m')}", part)

            def format_header(m: Match[str]) -> str:
                level, text_ = len(m.group(1)), m.group(2)
                if level == 1:
                    return f"{ansi('1;4;33m')}{text_}{ansi('0m')}"
                if level == 2:
                    return f"{ansi('1;33m')}{text_}{ansi('0m')}"
                return f"{ansi('33m')}{text_}{ansi('0m')}"

            part = re.sub(r"^(#{1,3}) (.+)$", format_header, part, flags=re.MULTILINE)
            result.append(part)
    return "".join(result)

def format_tool_call_display(name: str, args: Dict[str, Any]) -> str:
    """Format tool call arguments for user-friendly display.

    Instead of showing raw JSON, this returns a concise, meaningful representation
    of what the tool call does, tailored to each tool type.

    Args:
        name: The tool/function name.
        args: The tool arguments dictionary.

    Returns:
        A formatted string describing the tool call.
    """
    if name == "run_shell_command":
        cmd = args.get("command", "")
        return cmd if cmd else ""

    elif name == "commit_changes":
        message = args.get("message", "")
        return f'"{message}"' if message else ""

    elif name == "edit_file":
        path = args.get("path", "")
        find = args.get("find", "")
        replace = args.get("replace", "")
        find_preview = (find[:50] + "...") if len(find) > 50 else find
        return f"path={path}, find={find_preview!r}"

    elif name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        lines = len(content.splitlines()) if content else 0
        return f"path={path} ({lines} lines)"

    elif name == "get_repo_map":
        pattern = args.get("pattern", "")
        include_details = args.get("include_details", True)
        if pattern:
            return f"pattern={pattern!r}"
        return "" if include_details else "include_details=false"

    elif name == "browser_execute":
        code = args.get("code", "")
        code_preview = (code[:60] + "...") if len(code) > 60 else code
        return code_preview if code_preview else ""

    # Default: show minimal JSON for unknown tools
    return json.dumps(args, ensure_ascii=False)[:200]

def format_python_code(content: str, indent_size: int = 4) -> str:
    """Minimal Python code formatter.

    Handles common issues: trailing whitespace, inconsistent indentation,
    extra blank lines, missing file ending newline.

    Args:
        content: Python source code to format.
        indent_size: Number of spaces per indentation level (default 4).

    Returns:
        Formatted Python source code.
    """
    lines = content.split('\n')
    formatted: List[str] = []
    prev_was_blank = False

    for line in lines:
        # Strip trailing whitespace
        line = line.rstrip()

        # Convert tabs to spaces (if needed)
        line = line.expandtabs(indent_size)

        # Skip consecutive blank lines (keep at most one)
        is_blank = len(line.strip()) == 0
        if is_blank:
            if not prev_was_blank and formatted:
                formatted.append('')
            prev_was_blank = True
            continue

        prev_was_blank = False
        formatted.append(line)

    # Ensure single trailing newline
    result = '\n'.join(formatted)
    if not result.endswith('\n'):
        result += '\n'

    # Remove multiple trailing newlines
    result = result.rstrip('\n') + '\n'

    return result

def truncate(lines: List[str], n: int = 500, max_line_len: int = MAX_LINE_LENGTH) -> List[str]:
    """Truncate list of lines to fit within specified limits.

    Keeps first 20 and last 100 lines if truncation occurs, with [TRUNCATED] marker.
    Also truncates individual lines exceeding max_line_len.

    Args:
        lines: List of lines to truncate.
        n: Maximum number of lines to return (default 500).
        max_line_len: Maximum length of individual lines (default MAX_LINE_LENGTH).

    Returns:
        Truncated list of lines.
    """
    def trunc_line(line: str) -> str:
        return line if len(line) <= max_line_len else line[:max_line_len] + "..."

    lines = [trunc_line(line) for line in lines]
    return lines if len(lines) <= n else lines[:20] + ["[TRUNCATED]"] + lines[-100:]

def smart_truncate(lines: List[str], keep_first: int = 1, keep_last: int = 1, max_line_len: int = 80) -> List[str]:
    """Smart truncation for terminal display.

    Shows first and last lines, with line count summary in between.
    Great for quickly understanding output structure without clutter.

    Args:
        lines: List of lines to truncate.
        keep_first: Number of lines to keep from start.
        keep_last: Number of lines to keep from end.
        max_line_len: Maximum length of individual lines.

    Returns:
        Smart-truncated list of lines.
    """
    if not lines:
        return lines

    def trunc_line(line: str) -> str:
        return line if len(line) <= max_line_len else line[:max_line_len] + "..."

    lines = [trunc_line(line) for line in lines]

    if len(lines) <= keep_first + keep_last:
        return lines

    skipped = len(lines) - keep_first - keep_last
    return lines[:keep_first] + [f"... {skipped} lines skipped ..."] + lines[-keep_last:]

_CACHED_SYSTEM_INFO: Optional[SystemInfo] = None

def _get_distro_info() -> Dict[str, str]:
    """Get Linux distribution information.

    Returns:
        Dictionary with distro name, version, and id, or empty strings if not available.
    """
    result = {"distro": "", "distro_version": "", "distro_id": ""}

    # Try platform.freedesktop_os_release() first (Python 3.10+)
    if sys.version_info >= (3, 10):
        try:
            os_release = platform.freedesktop_os_release()
            if os_release:
                result["distro"] = os_release.get("NAME", "")
                result["distro_version"] = os_release.get("VERSION", "")
                result["distro_id"] = os_release.get("ID", "")
                # Clean up quotes if present
                result["distro"] = result["distro"].strip('"\'')
                result["distro_version"] = result["distro_version"].strip('"\'')
        except Exception:
            pass

    # Fall back to parsing /etc/os-release manually
    if not result["distro_id"]:
        try:
            os_release_path = "/etc/os-release"
            if os.path.exists(os_release_path):
                with open(os_release_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            key, value = line.split("=", 1)
                            value = value.strip('"\'')
                            if key == "NAME" and not result["distro"]:
                                result["distro"] = value
                            elif key == "VERSION" and not result["distro_version"]:
                                result["distro_version"] = value
                            elif key == "ID" and not result["distro_id"]:
                                result["distro_id"] = value
        except Exception:
            pass

    return result

def _get_windows_info() -> Dict[str, str]:
    """Get Windows-specific information.

    Returns:
        Dictionary with Windows edition and version, or empty strings if not available.
    """
    result = {"windows_edition": "", "windows_version": ""}

    if platform.system() == "Windows":
        try:
            import winreg

            # Get Windows version info from registry
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            )
            for name in ["ProductName", "DisplayVersion", "CurrentBuild"]:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                    if name == "ProductName":
                        result["windows_edition"] = value
                    elif name == "DisplayVersion":
                        result["windows_version"] = value
                except FileNotFoundError:
                    pass

            winreg.CloseKey(key)
        except Exception:
            pass

    return result

def _get_system_resources() -> Dict[str, Any]:
    """Get system resource information.

    Returns:
        Dictionary with memory, CPU, and disk information.
    """
    result: Dict[str, Any] = {}

    # Memory info
    try:
        if platform.system() == "Windows":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                ]
            memory_status = MEMORYSTATUSEX()
            memory_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status))
            result["memory_total_gb"] = round(memory_status.ullTotalPhys / (1024**3), 1)
            result["memory_available_gb"] = round(memory_status.ullAvailPhys / (1024**3), 1)
        else:
            # Unix-like systems
            with open("/proc/meminfo", "r") as f:
                meminfo = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(":")
                        value = int(parts[1])  # in kB
                        meminfo[key] = value
                result["memory_total_gb"] = round(meminfo.get("MemTotal", 0) / (1024**2), 1)
                result["memory_available_gb"] = round(meminfo.get("MemAvailable", 0) / (1024**2), 1)
    except Exception:
        pass

    # CPU core count
    try:
        result["cpu_cores"] = os.cpu_count() or 0
    except Exception:
        pass

    # Disk space on current partition
    try:
        cwd = os.getcwd()
        usage = os.statvfs(cwd)
        total_gb = (usage.f_blocks * usage.f_frsize) / (1024**3)
        free_gb = (usage.f_bavail * usage.f_frsize) / (1024**3)
        result["disk_total_gb"] = round(total_gb, 1)
        result["disk_free_gb"] = round(free_gb, 1)
    except Exception:
        pass

    return result

def _get_venv_info() -> Dict[str, str]:
    """Get virtual environment information.

    Returns:
        Dictionary with venv path and python binary info if in a venv.
    """
    result: Dict[str, str] = {}

    # Check if we're in a virtual environment
    venv_path = os.environ.get("VIRTUAL_ENV")
    conda_env = os.environ.get("CONDA_DEFAULT_ENV")

    if venv_path:
        result["venv_path"] = venv_path
        # Find python binary in venv
        python_binary = os.path.join(venv_path, "bin", "python")
        if not os.path.exists(python_binary):
            python_binary = os.path.join(venv_path, "Scripts", "python.exe")
        if os.path.exists(python_binary):
            result["venv_python_binary"] = python_binary
            try:
                version_output = run(f"{python_binary} --version")
                if version_output:
                    result["venv_python_version"] = version_output.strip()
            except Exception:
                pass
    elif conda_env:
        result["venv_path"] = sys.prefix
        python_binary = sys.executable
        result["venv_python_binary"] = python_binary
        result["venv_python_version"] = f"Python {sys.version.split()[0]}"
    else:
        # Check for local .venv or venv directory in current folder
        for venv_name in [".venv", "venv"]:
            local_venv = os.path.join(os.getcwd(), venv_name)
            if os.path.isdir(local_venv):
                python_binary = os.path.join(local_venv, "bin", "python")
                if not os.path.exists(python_binary):
                    python_binary = os.path.join(local_venv, "Scripts", "python.exe")
                if os.path.exists(python_binary):
                    result["local_venv_path"] = local_venv
                    result["local_venv_python_binary"] = python_binary
                    try:
                        version_output = run(f"{python_binary} --version")
                        if version_output:
                            result["local_venv_python_version"] = version_output.strip()
                    except Exception:
                        pass
                break

    return result

def _get_user_and_locale() -> Dict[str, str]:
    """Get user and locale information.

    Returns:
        Dictionary with username, locale, and encoding info.
    """
    result: Dict[str, str] = {}

    # Username
    try:
        result["user"] = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    except Exception:
        pass

    # Locale/Language
    try:
        result["language"] = os.environ.get("LANG", "")
        result["locale"] = os.environ.get("LC_ALL", "") or os.environ.get("LC_CTYPE", "") or ""
    except Exception:
        pass

    # Filesystem encoding
    try:
        result["filesystem_encoding"] = sys.getfilesystemencoding() or "unknown"
        result["string_encoding"] = sys.getdefaultencoding() or "unknown"
    except Exception:
        pass

    return result

def system_summary() -> SystemInfo:
    """Return cached system information dictionary.

    Gathers OS details, Python version, available tools, and tool versions.
    Results are cached to avoid repeated system calls.

    Returns:
        Dictionary containing:
        - os: Operating system name
        - release: OS release version
        - machine: Machine architecture
        - python: Python version
        - cwd: Current working directory
        - shell: Default shell
        - path: PATH environment variable
        - venv: Whether running in virtual environment
        - tools: List of available tools
        - versions: Dictionary of tool versions
        - distro: Linux distribution name (if applicable)
        - distro_version: Linux distribution version (if applicable)
        - distro_id: Linux distribution ID (if applicable)
        - windows_edition: Windows edition (if applicable)
        - windows_version: Windows version (if applicable)
    """
    global _CACHED_SYSTEM_INFO
    if _CACHED_SYSTEM_INFO is not None:
        return _CACHED_SYSTEM_INFO
    try:
        tools: List[str] = [
            "apt",
            "bash",
            "curl",
            "docker",
            "gcc",
            "git",
            "make",
            "node",
            "npm",
            "perl",
            "pip",
            "python3",
            "sh",
            "tar",
            "unzip",
            "wget",
            "zip",
        ]
        versions: Dict[str, str] = {
            tool: (run(f"{tool} --version") or "").split("\n")[0][:80]
            for tool in ["git", "python3", "pip", "node", "npm", "docker", "gcc"]
            if shutil.which(tool)
        }

        # Get distribution-specific info
        distro_info = _get_distro_info() if platform.system() == "Linux" else {}
        windows_info = _get_windows_info() if platform.system() == "Windows" else {}

        # Get system resources
        resources_info = _get_system_resources()

        # Get virtual environment info
        venv_info = _get_venv_info()

        # Get user and locale info
        user_locale_info = _get_user_and_locale()

        _CACHED_SYSTEM_INFO = {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "cwd": os.getcwd(),
            "shell": os.environ.get("SHELL") or os.environ.get("ComSpec") or "",
            "path": os.environ.get("PATH", ""),
            "venv": bool(os.environ.get("VIRTUAL_ENV") or sys.prefix != sys.base_prefix),
            "tools": [tool for tool in tools if shutil.which(tool)],
            "versions": {k: v for k, v in versions.items() if v},
            **distro_info,
            **windows_info,
            **resources_info,
            **venv_info,
            **user_locale_info,
        }
    except Exception as e:
        # Log error but return empty dict to avoid breaking the application
        print(styled(f"Warning: Could not gather system info: {e}", "93m"), file=sys.stderr)
        _CACHED_SYSTEM_INFO = {}
    return _CACHED_SYSTEM_INFO

def safe_repo_path(root: str, rel_path: str) -> Path:
    """Ensure path is within repository root to prevent path traversal.

    Args:
        root: Repository root directory.
        rel_path: Relative path to validate.

    Returns:
        Validated Path object within repo root.

    Raises:
        ValueError: If path escapes repository root.
    """
    p = Path(root, rel_path)
    resolved = p.resolve(strict=False)
    root_resolved = Path(root).resolve()
    if not str(resolved).startswith(str(root_resolved)):
        raise ValueError(f"path escapes repo: {rel_path}")
    return p

def safe_read_file(
    path: str, root: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Safely read file with security and size checks.

    Validates file exists, checks for symlinks pointing outside repo,
    verifies file is regular (not special), and enforces size limits.

    Args:
        path: File path to read (absolute or relative).
        root: Repository root for path validation (None for absolute paths).

    Returns:
        Tuple of (content, error):
        - content: File contents or "[empty]" for empty files, None on error.
        - error: Error message string, None on success.
    """
    p = Path(path) if root is None else safe_repo_path(root, path)
    if not p.exists():
        return None, "not found"
    if p.is_symlink():
        try:
            target = p.resolve()
            root_path = Path(root).resolve() if root else Path.cwd().resolve()
            if not str(target).startswith(str(root_path)):
                return None, f"symlink points outside repo: {target}"
        except (OSError, ValueError) as e:
            return None, f"symlink error: {e}"
    try:
        mode = p.stat().st_mode
        if not stat.S_ISREG(mode):
            return None, "special file (not regular)"
    except OSError as e:
        return None, f"cannot stat: {e}"
    try:
        size = p.stat().st_size
        if size > MAX_FILE_SIZE:
            size_kb = size / 1024
            size_str = f"{size_kb:.1f}KB" if size_kb < 1024 else f"{size_kb / 1024:.1f}MB"
            return None, f"file too large: {size_str}"
    except OSError as e:
        return None, f"cannot check size: {e}"
    try:
        content = p.read_text()
        return content if content else "[empty]", None
    except PermissionError:
        return None, "permission denied"
    except UnicodeDecodeError:
        return None, "binary/not UTF-8"
    except OSError as e:
        return None, f"read error: {e}"

def get_map(root: str, pattern: Optional[str] = None, include_details: bool = True) -> str:
    """Generate repository file map showing ALL files and Python definitions with line numbers.

    Uses smart filesystem scanning (no git dependency), excludes common clutter directories,
    and extracts Python def/class/method locations with line numbers. By default shows all files.

    Args:
        root: Repository root directory.
        pattern: Optional glob pattern to filter files (e.g., '*.py', 'src/*'). Omit to show all files.
        include_details: If true, include line numbers for Python elements.

    Returns:
        Formatted markdown string with all files and Python definitions (when include_details is True).
    """
    BINARY_EXT: Set[str] = {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp",
        ".mp3", ".mp4", ".wav", ".avi", ".mov",
        ".zip", ".tar", ".gz", ".rar", ".7z", ".pdf",
        ".exe", ".dll", ".so", ".dylib", ".pyc", ".whl", ".egg",
        ".woff", ".woff2", ".ttf", ".eot",
    }

    EXCLUDE_DIRS: Set[str] = {
        ".git", "node_modules", "__pycache__", "venv", ".venv",
        ".tox", "dist", "build", ".eggs", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "htmlcov", ".coverage",
        "env", ".env", "data", "datasets", "models", "cache",
        "*.egg-info", ".ipynb_checkpoints"
    }

    output = []
    file_count = 0
    excluded_found: Set[str] = set()
    root_path = Path(root)

    # Walk filesystem with early pruning
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune excluded directories IN PLACE (prevents descent)
        original_dirnames = dirnames.copy()
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

        # Track excluded directories we found
        for d in original_dirnames:
            if d in EXCLUDE_DIRS:
                rel_dir = Path(dirpath) / d
                try:
                    excluded_found.add(str(rel_dir.relative_to(root_path)))
                except ValueError:
                    excluded_found.add(d)

        for filename in filenames:
            filepath = Path(dirpath) / filename
            try:
                rel_path = filepath.relative_to(root_path)
                rel_path_str = str(rel_path)
            except ValueError:
                continue

            # Apply pattern filter if provided
            if pattern and not fnmatch.fnmatch(rel_path_str, pattern):
                continue

            # Check binary by extension first (fast)
            if filepath.suffix.lower() in BINARY_EXT:
                output.append(f"{rel_path_str} [binary]")
                file_count += 1
                continue

            # Check for binary content (sample first bytes)
            try:
                with open(filepath, "rb") as f:
                    chunk = f.read(512)
                    if b"\x00" in chunk:
                        output.append(f"{rel_path_str} [binary]")
                        file_count += 1
                        continue
            except Exception:
                output.append(f"{rel_path_str} [unreadable]")
                file_count += 1
                continue

            # Process Python files for element details
            if include_details and filepath.suffix == ".py":
                output.append(f"{rel_path_str}:")
                elements = _extract_python_elements(filepath)
                for elem in elements:
                    output.append(f"  {elem['type']} {elem['name']} ({elem['start_line']}-{elem['end_line']})")
            else:
                output.append(rel_path_str)

            file_count += 1

    # Add excluded directories to output
    if excluded_found:
        output.append("")
        output.append("# Excluded directories:")
        for exc_dir in sorted(excluded_found):
            output.append(f"  {exc_dir}/")

    return "\n".join(output)

def _extract_python_elements(filepath: Path) -> List[PythonElement]:
    """Extract Python functions, classes, and methods with line numbers.

    Args:
        filepath: Path to Python file.

    Returns:
        List of dicts with name, type ('def' or 'class'), and line ranges.
    """
    try:
        source = filepath.read_text()
        tree = ast.parse(source)
        elements: List[PythonElement] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                elements.append({
                    "name": node.name,
                    "type": "def",
                    "start_line": node.lineno,
                    "end_line": getattr(node, 'end_lineno', node.lineno)
                })
            elif isinstance(node, ast.ClassDef):
                # Add the class itself
                elements.append({
                    "name": node.name,
                    "type": "class",
                    "start_line": node.lineno,
                    "end_line": getattr(node, 'end_lineno', node.lineno)
                })
                # Add methods within the class
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        elements.append({
                            "name": f"{node.name}.{item.name}",
                            "type": "def",
                            "start_line": item.lineno,
                            "end_line": getattr(item, 'end_lineno', item.lineno)
                        })

        # Sort by line number
        return sorted(elements, key=lambda e: e["start_line"])
    except Exception:
        return []

def validate_path_for_shell(path: str) -> str:
    """Validate and sanitize a path for use in shell commands.

    Ensures the path is absolute, exists, and contains no shell metacharacters.

    Args:
        path: Path string to validate.

    Returns:
        Validated absolute path string.

    Raises:
        ValueError: If path is invalid or contains dangerous characters.
    """
    # Check for shell metacharacters
    dangerous_chars = set(';&|`$\\(){}[]<>!#\n\r')
    if any(c in path for c in dangerous_chars):
        raise ValueError(f"Path contains invalid characters: {path!r}")

    # Resolve to absolute path
    p = Path(path).resolve()

    # Verify it exists and is a directory
    if not p.exists():
        raise ValueError(f"Path does not exist: {path!r}")
    if not p.is_dir():
        raise ValueError(f"Path is not a directory: {path!r}")

    return str(p)

def is_safe_read_command(cmd: str) -> bool:
    """Check if a shell command is a safe read-only operation.

    Whitelists: cat, sed, head, tail, wc, grep, find, ls, pwd, echo, date
    with safe patterns (no pipes to dangerous commands, no redirection to files).

    Args:
        cmd: Shell command to check.

    Returns:
        True if command appears safe for auto-approval.
    """
    safe_commands = {"cat", "sed", "head", "tail", "wc", "grep", "find", "ls", "pwd", "echo", "date", "file", "which"}

    # Remove leading whitespace and get first word
    cmd_stripped = cmd.strip()
    first_word = cmd_stripped.split()[0] if cmd_stripped.split() else ""

    # Check if it's a safe command
    if first_word not in safe_commands:
        return False

    # Check for command substitution patterns (backticks and $())
    # These can chain commands even if the base command is safe
    if "`" in cmd_stripped or "$(" in cmd_stripped:
        return False

    # Check for dangerous patterns
    dangerous_patterns = [
        "| rm", "| xargs rm", "| sh", "| bash", "| eval",
        ">", ">>", "2>", "&>",
        "; rm", "; mv", "; cp", "; chmod", "; chown",
        "&& rm", "|| rm",
    ]

    for pattern in dangerous_patterns:
        if pattern in cmd_stripped:
            return False

    # For sed, check it's only using safe operations (no in-place editing)
    if first_word == "sed":
        if "-i" in cmd_stripped or ">>" in cmd_stripped or ">" in cmd_stripped:
            return False

    # For find, check it's not executing commands
    if first_word == "find":
        if "-exec" in cmd_stripped or "-delete" in cmd_stripped:
            return False

    return True

def run_shell_interactive(cmd: str, stream_output: bool = True) -> Tuple[List[str], int]:
    """Run shell command interactively with live output.

    Streams command output line by line, handles Ctrl+C gracefully.

    Args:
        cmd: Shell command to execute.
        stream_output: Whether to print output as it's generated (default: True).

    Returns:
        Tuple of (output_lines, exit_code):
        - output_lines: List of output lines.
        - exit_code: Command exit code.
    """
    output_lines: List[str] = []
    process = subprocess.Popen(
        cmd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if stream_output:
                print(line, end="", flush=True)
            output_lines.append(line.rstrip("\n"))
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=2)
        except Exception:
            pass
        output_lines.append("[INTERRUPTED]")
        if stream_output:
            print("\n[INTERRUPTED]")
    return output_lines, process.returncode

def run_ssh_interactive_pty(cmd: str) -> Tuple[List[str], int]:
    """Run SSH command with PTY for interactive password prompts.

    Uses pty.fork() to create a proper controlling terminal so sudo recognizes
    it as a real TTY. Captures output for the agent while allowing user input.

    Args:
        cmd: SSH command to execute.

    Returns:
        Tuple of (output_lines, exit_code):
        - output_lines: List of output lines (captured from PTY).
        - exit_code: Command exit code.
    """
    import pty
    import select
    import os
    import signal

    output_lines: List[str] = []
    buffer = ""
    exit_code = None

    # Fork a new process with PTY
    pid, master_fd = pty.fork()

    if pid == 0:
        # Child process: execute the command
        os.execvp('sh', ['sh', '-c', cmd])
    else:
        # Parent process: monitor and interact
        try:
            # Set master fd to non-blocking mode
            fd_flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, fd_flags | os.O_NONBLOCK)

            try:
                while exit_code is None:
                    # Use select to wait for data on both master_fd (output) and stdin (input)
                    readable, _, _ = select.select([master_fd, sys.stdin.fileno()], [], [], 0.1)

                    for fd in readable:
                        if fd == master_fd:
                            # Read output from PTY master
                            try:
                                data = os.read(master_fd, 4096).decode('utf-8', errors='replace')

                                if not data:
                                    # EOF - process likely finished
                                    break

                                buffer += data

                                # Process complete lines
                                while '\n' in buffer:
                                    line, buffer = buffer.split('\n', 1)
                                    output_lines.append(line)
                                    print(line, end='\n', flush=True)

                            except OSError:
                                break
                        elif fd == sys.stdin.fileno():
                            # Read input from user and forward to PTY
                            try:
                                input_data = os.read(sys.stdin.fileno(), 4096)
                                if input_data:
                                    # Write user input to PTY master
                                    os.write(master_fd, input_data)
                            except OSError:
                                pass

                    # Check if child process has completed
                    child_pid, status = os.waitpid(pid, os.WNOHANG)
                    if child_pid != 0:
                        exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else os.WTERMSIG(status)

            except KeyboardInterrupt:
                os.kill(pid, signal.SIGINT)
                output_lines.append("[INTERRUPTED]")
                print("\n[INTERRUPTED]", flush=True)

            # Read any remaining output
            time.sleep(0.1)
            try:
                remaining = os.read(master_fd, 4096).decode('utf-8', errors='replace')
                if remaining:
                    buffer += remaining
                    if buffer:
                        output_lines.append(buffer)
                        print(buffer, end='', flush=True)
            except OSError:
                pass

            # If we haven't gotten exit code yet, wait for it
            if exit_code is None:
                try:
                    _, status = os.waitpid(pid, 0)
                    exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else os.WTERMSIG(status)
                except ChildProcessError:
                    pass

        finally:
            os.close(master_fd)

    return output_lines, exit_code if exit_code is not None else 0

def lint_py(path: str, content: str) -> Tuple[bool, Optional[str]]:
    """Check Python file for syntax errors.

    Args:
        path: File path (must end with .py to be checked).
        content: File contents to parse.

    Returns:
        Tuple of (is_valid, error_message):
        - is_valid: True if syntax is valid or not a Python file.
        - error_message: Syntax error string if invalid, None otherwise.
    """
    if not path.endswith(".py"):
        return True, None
    try:
        ast.parse(content)
        return True, None
    except SyntaxError as e:
        return False, str(e)

class Spinner:
    """Animated terminal spinner for showing progress.

    Displays a rotating spinner with pulsing color effect in a background thread.
    """

    def __init__(self) -> None:
        """Initialize spinner."""
        self.stop_event: threading.Event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the spinner animation in a background thread."""
        def spin() -> None:
            print()
            chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            i = 0
            while not self.stop_event.is_set():
                wave = (math.sin(time.time() * 4) + 1) / 2
                rgb_val = int(100 + wave * 155)
                color_code = f"\033[1m\033[38;2;{rgb_val};{rgb_val};255m"
                reset_code = "\033[0m"
                print(f"\r{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')} {color_code}{chars[i % len(chars)]}{reset_code} ", end="", flush=True)
                i += 1
                time.sleep(0.08)

        self.thread = threading.Thread(target=spin, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the spinner animation and clean up."""
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        print("\r", end="", flush=True)

class LocalCode:
    """Core agent orchestrating the interactive coding session.

    Handles repo context, tool execution (read/edit/run/commit),
    llama.cpp server communication, and the REPL loop while following
    strict safety and precision rules.
    """

    def __init__(self) -> None:
        """Initialize LocalCode agent.

        Sets up repository context, checks llama.cpp server connectivity,
        and starts the browser bridge server.
        """
        self.repo_root: str = run("git rev-parse --show-toplevel") or os.getcwd()
        self.pending_notes: List[str] = []
        self.messages: List[Message] = []  # Conversation history for llama.cpp
        self.last_usage: Optional[Dict[str, int]] = None
        self.total_tokens: int = 0
        self._tokens_estimated: bool = False  # True if tokens are estimated (not from API)
        self.bridge_port: int = self._get_bridge_port()
        self._map_cache: Dict[Tuple[Optional[str], bool], str] = {}
        self._map_mtime: Dict[Tuple[Optional[str], bool], float] = {}
        self._initial_context_sent: bool = False  # Track if initial context was sent
        self._conversation_log: Optional[ConversationLog] = None
        self.skills: List[Skill] = _discover_skills()

        self._check_llama_server()
        self._start_bridge_if_needed()

    def _check_llama_server(self) -> None:
        """Check if llama.cpp server is reachable.

        Prints connection status and helpful instructions if server is unavailable.
        """
        try:
            req = urllib.request.Request(
                f"{LLAMA_HOST}/health",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print(styled(f"✓ llama.cpp server connected at {LLAMA_HOST}", "32m"))
                    return
        except Exception:
            pass
        print(styled(f"⚠ Warning: llama.cpp server not reachable at {LLAMA_HOST}", "93m"))
        print(styled("  Start it with: llama-server -m <model.gguf> --port 8080", "90m"))
        print(styled("  Or set LLAMA_HOST environment variable", "90m"))

    def get_repo_map(self, pattern: Optional[str] = None, include_details: bool = True) -> str:
        """Get repository map with optional pattern filtering.

        Args:
            pattern: Optional glob pattern to filter files.
            include_details: If true, include line numbers for Python elements.

        Returns:
            Formatted repository map string.
        """
        # Cache key based on parameters
        cache_key = (pattern, include_details)

        # Check for any file system changes by looking at newest file mtime
        root_path = Path(self.repo_root)
        if not root_path.exists():
            current_mtime = 0.0
        else:
            # Get the most recent modification time of any file in the repo
            try:
                current_mtime = max(
                    p.stat().st_mtime
                    for p in root_path.rglob("*")
                    if p.is_file() and not p.is_symlink()
                )
            except (ValueError, OSError):
                current_mtime = root_path.stat().st_mtime

        cached_mtime = self._map_mtime.get(cache_key, -1)
        if cache_key in self._map_cache and abs(current_mtime - cached_mtime) < 0.1:
            return self._map_cache[cache_key]

        self._map_cache[cache_key] = get_map(self.repo_root, pattern, include_details)
        self._map_mtime[cache_key] = current_mtime
        return self._map_cache[cache_key]

    def _get_bridge_port(self) -> int:
        """Find an available port for the browser bridge.

        Starts from DEFAULT_BRIDGE_PORT (or LOCALCODE_BRIDGE_PORT env var)
        and searches for the first unused port.

        Returns:
            Available port number.
        """
        port = int(os.getenv("LOCALCODE_BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT)))
        for p in range(port, port + 20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("localhost", p)) != 0:
                    return p
        return port

    def _start_bridge_if_needed(self) -> None:
        """Start integrated bridge server in a daemon thread."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("localhost", self.bridge_port)) == 0:
                    print(styled(f"✓ Bridge already running on {self.bridge_port}", "32m"))
                    return
        except Exception:
            pass

        def run_bridge():
            server_address = ("localhost", self.bridge_port)
            # Allow reuse to avoid "Address already in use" errors (TIME_WAIT etc.)
            class ReuseTCPServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
            httpd = ReuseTCPServer(server_address, BridgeHandler)
            httpd.daemon_threads = True
            print(styled(f"✓ Bridge listening on {self.bridge_port} (threaded)", "32m"))
            try:
                httpd.serve_forever()
            except:
                pass

        thread = threading.Thread(target=run_bridge, daemon=True)
        thread.start()
        time.sleep(0.5)  # allow startup

    def llama_request(self, messages: List[Message], tools: Optional[List[Dict[str, Any]]] = None, slot_id: int = 0) -> Optional[LlamaResponse]:
        """Send a chat completion request to llama.cpp server.

        Args:
            messages: List of conversation messages.
            tools: Optional list of tool definitions.
            slot_id: llama.cpp slot ID for cache reuse (default: 0 for main conversation, 1 for safety checks).
        """
        # Convert tools to OpenAI format expected by llama.cpp
        openai_tools = None
        if tools:
            openai_tools = []
            for tool in tools:
                if tool.get("type") == "function":
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.get("name"),
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {}),
                        }
                    })

        payload: Dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "stop": ["<|eot_id|>", "<|end_of_text|>"],
            "stream": False,
            "cache_prompt": True,
            "id_slot": slot_id,
        }

        if openai_tools:
            payload["tools"] = openai_tools
            payload["tool_choice"] = "auto"

        req = urllib.request.Request(
            f"{LLAMA_HOST}/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"localcode/{VERSION}",
            },
            data=json.dumps(payload).encode(),
        )

        spinner = Spinner()
        spinner.start()
        request_start_time = time.time()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                request_latency_ms = (time.time() - request_start_time) * 1000

                self.last_usage = body.get("usage")
                if self.last_usage:
                    prompt_tokens = self.last_usage.get("prompt_tokens", 0)
                    completion_tokens = self.last_usage.get("completion_tokens", 0)
                    if self._tokens_estimated:
                        # Replace estimated total with actual API prompt tokens
                        self.total_tokens = prompt_tokens
                    else:
                        self.total_tokens += prompt_tokens
                    self._tokens_estimated = False  # Reset when we get real API data

                # Extract cache info from timings
                tokens_cached = 0
                timings = body.get("timings", {})
                if timings:
                    tokens_cached = timings.get("cache_n", 0)

                # Log model request metrics if conversation log is available
                if self._conversation_log:
                    self._conversation_log.log_model_request(
                        latency_ms=request_latency_ms,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cache_hits=tokens_cached
                    )

                spinner.stop()

                # Build token info string with visual bar
                CONTEXT_WINDOW = 90000
                ctx_pct = (prompt_tokens / CONTEXT_WINDOW) * 100
                cache_pct = (tokens_cached / prompt_tokens) * 100 if prompt_tokens > 0 else 0

                # Create visual bar (20 chars) representing context usage
                bar_len = 20
                filled = int((ctx_pct / 100) * bar_len)
                filled = max(1, min(filled, bar_len)) if ctx_pct > 0 else 0
                bar = '█' * filled + '░' * (bar_len - filled)

                # Color the bar based on context usage
                if ctx_pct < 60:
                    bar_color = '32m'  # green
                elif ctx_pct < 90:
                    bar_color = '33m'  # yellow
                else:
                    bar_color = '31m'  # red

                print(f"{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')} {styled('✓', '32m')} {styled(bar, bar_color)}  {ctx_pct:.1f}% ctx - {cache_pct:.0f}% cached\n")
                return body
        except urllib.error.HTTPError as e:
            spinner.stop()
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:1200]
            except Exception:
                pass
            print(styled(f"HTTP {e.code}: {e.reason}", "31m"))
            if error_body:
                print(styled(error_body, "31m"))
            return None
        except urllib.error.URLError as e:
            spinner.stop()
            print(styled(f"Connection error: {e.reason}", "31m"))
            print(styled(f"Is llama-server running at {LLAMA_HOST}?", "93m"))
            return None
        except KeyboardInterrupt:
            spinner.stop()
            print(styled("[user interrupted]", "93m"))
            return None
        except Exception as e:
            spinner.stop()
            print(styled(f"Err: {e}", "31m"))
            return None

    def build_user_message(self, request: str) -> str:
        """Build the user message content with context.

        Minimal context: system summary and time sent only once for cache efficiency.
        Agent uses get_repo_map tool and shell commands (cat/grep) to read files.
        """
        now = datetime.datetime.now().astimezone()
        day = now.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        current_time = now.strftime(f"%A {day}{suffix} of %B %Y, %H:%M %Z")

        parts = []

        # Send system summary and time only once for cache efficiency
        if not self._initial_context_sent:
            parts.append(f"### System Summary\n{json.dumps(system_summary(), separators=(',', ':'))}")
            parts.append(f"### Current Time\n{current_time}")
            self._initial_context_sent = True

        if self.pending_notes:
            parts.append("### Extra Context\n" + "\n\n".join(self.pending_notes))
            self.pending_notes.clear()

        global _bridge_state
        if _bridge_state.get("url"):
            parts.append(f"### Browser State\nURL: {_bridge_state.get('url')}\nTitle: {_bridge_state.get('title', '')}")

        parts.append(f"### Request\n{request}")

        return "\n\n".join(parts)

    def get_messages_with_system(self) -> List[Message]:
        """Return messages list with system prompt prepended."""
        return [{"role": "system", "content": get_system_prompt(self.skills)}] + self.messages

    def extract_text(self, response: LlamaResponse) -> str:
        """Extract text content from OpenAI-compatible response."""
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return message.get("content", "") or ""

    def extract_reasoning_content(self, response: LlamaResponse) -> str:
        """Extract reasoning_content from OpenAI-compatible response."""
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return message.get("reasoning_content", "") or ""

    def extract_tool_calls(self, response: LlamaResponse) -> List[ToolCall]:
        """Extract tool calls from OpenAI-compatible response."""
        choices = response.get("choices", [])
        if not choices:
            return []
        message = choices[0].get("message", {})
        return message.get("tool_calls", []) or []

    def get_finish_reason(self, response: LlamaResponse) -> str:
        """Get finish reason from response."""
        choices = response.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("finish_reason", "") or ""

    def print_assistant_text(self, text: str) -> None:
        if not text:
            return
        print(render_md(text))
        print()

    def tool_get_repo_map(self, args: Dict[str, Any]) -> ToolResult:
        pattern = args.get("pattern", "")
        include_details = args.get("include_details", True)

        result = self.get_repo_map(pattern if pattern else None, include_details)

        # Count files in output (lines that are file paths, not comments or indented)
        file_count = len([
            line for line in result.split("\n")
            if line and not line.startswith("#") and not line.startswith("  ") and not line.startswith("Excluded:")
        ])

        print(styled(f"Repository map ({file_count} files)", "36m"))
        print(result)

        return {"ok": True, "file_count": file_count, "map": result}

    def tool_write_file(self, args: Dict[str, Any]) -> ToolResult:
        if "path" not in args:
            return {"ok": False, "error": "missing required argument: path"}
        if "content" not in args:
            return {"ok": False, "error": "missing required argument: content"}
        path = args["path"]
        content = args["content"]
        overwrite = args.get("overwrite", False)

        try:
            p = safe_repo_path(self.repo_root, path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        # Track if file exists and if user confirmed overwrite
        file_existed = p.exists()
        user_confirmed = True

        # Check if file exists
        if file_existed:
            if not overwrite:
                return {"ok": False, "error": "file already exists (set overwrite=true to replace)"}

            # Confirm overwrite
            print(styled(f"⚠ {path} already exists. Overwrite? (y/n): ", "93m"), end="")
            sys.stdout.flush()
            try:
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = "n"

            if answer != "y":
                # Log failed operation
                if self._conversation_log:
                    self._conversation_log.log_file_operation(
                        operation="write",
                        file_path=path,
                        success=False,
                        user_confirmed=False
                    )
                return {"ok": False, "error": "user cancelled overwrite"}

            user_confirmed = True

        ok, lint_error = lint_py(path, content)
        if not ok:
            print(styled(f"Lint Fail {path}: {lint_error}", "31m"))
            return {"ok": False, "error": f"python syntax error: {lint_error}"}

        try:
            p.parent.mkdir(parents=True, exist_ok=True)

            # Show diff if overwriting existing file
            if file_existed and user_confirmed:
                old_content = p.read_text()
                diff_lines = list(
                    difflib.unified_diff(
                        old_content.splitlines(),
                        content.splitlines(),
                        fromfile=path,
                        tofile=path,
                        lineterm="",
                    )
                )
                for d in diff_lines:
                    if d.startswith(("---", "+++")):
                        continue
                    color = "32m" if d.startswith("+") else "31m" if d.startswith("-") else "0m"
                    print(styled(d, color))
            else:
                # New file: show all lines
                for ln in content.splitlines():
                    print(styled(f"+{ln}", "32m"))

            p.write_text(content)
            print(styled(f"{'Created' if not file_existed else 'Overwrote'} {path}", "32m"))

            # Log successful file operation
            if self._conversation_log:
                self._conversation_log.log_file_operation(
                    operation="write" if not file_existed else "overwrite",
                    file_path=path,
                    success=True,
                    user_confirmed=user_confirmed
                )

            return {"ok": True, "path": path, "valid_syntax": True}
        except (PermissionError, OSError) as e:
            # Log failed operation
            if self._conversation_log:
                self._conversation_log.log_file_operation(
                    operation="write",
                    file_path=path,
                    success=False,
                    user_confirmed=user_confirmed
                )
            return {"ok": False, "error": str(e)}

    def tool_edit_file(self, args: Dict[str, Any]) -> ToolResult:
        path = args["path"]
        find = args["find"]
        replace = args["replace"]

        try:
            p = safe_repo_path(self.repo_root, path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if not p.exists():
            return {"ok": False, "error": "file not found"}

        content, error = safe_read_file(path, self.repo_root)
        if error:
            return {"ok": False, "error": error}
        if content == "[empty]":
            content = ""

        if find not in content:
            return {"ok": False, "error": "exact find text not found"}

        new_content = content.replace(find, replace, 1)
        ok, lint_error = lint_py(path, new_content)
        if not ok:
            print(styled(f"Lint Fail {path}: {lint_error}", "31m"))
            return {"ok": False, "error": f"python syntax error: {lint_error}"}

        if new_content == content:
            return {"ok": False, "error": "no-op edit"}

        diff_lines = list(
            difflib.unified_diff(
                content.splitlines(),
                new_content.splitlines(),
                fromfile=path,
                tofile=path,
                lineterm="",
            )
        )
        for d in diff_lines:
            if d.startswith(("---", "+++")):
                continue
            color = "32m" if d.startswith("+") else "31m" if d.startswith("-") else "0m"
            print(styled(d, color))

        try:
            p.write_text(new_content)

            # Count lines changed
            added = sum(1 for d in diff_lines if d.startswith("+") and not d.startswith("+++") )
            removed = sum(1 for d in diff_lines if d.startswith("-") and not d.startswith("---"))

            print(styled(f"Applied {path} (+{added} -{removed})", "32m"))

            # Log successful file operation
            if self._conversation_log:
                self._conversation_log.log_file_operation(
                    operation="edit",
                    file_path=path,
                    success=True,
                    lines_changed=added + removed
                )

            return {"ok": True, "path": path, "lines_added": added, "lines_removed": removed, "valid_syntax": True}
        except (PermissionError, OSError) as e:
            # Log failed operation
            if self._conversation_log:
                self._conversation_log.log_file_operation(
                    operation="edit",
                    file_path=path,
                    success=False
                )
            return {"ok": False, "error": str(e)}

    def _is_command_safe(self, cmd: str) -> Tuple[bool, Optional[str]]:
        """Check if a command is safe using rule-based whitelist.

        Args:
            cmd: Shell command to check.

        Returns:
            Tuple of (is_safe, classification):
            - is_safe: True if command can auto-run, False if user approval needed.
            - classification: 'safe' if whitelisted, 'other' otherwise.
        """
        # First, fast rule-based check for known safe commands
        if is_safe_read_command(cmd):
            return (True, 'safe')

        # Non-whitelisted commands require user approval
        return (False, 'other')

    def tool_run_shell_command(self, args: Dict[str, Any]) -> ToolResult:
        cmd = args["command"].strip()
        if not cmd:
            return {"ok": False, "error": "empty command"}

        # Check command safety using rule-based + LLM classification
        is_safe, classification = self._is_command_safe(cmd)

        if is_safe:
            answer = "y"
        else:
            print(f"{styled('[?] $ ' + cmd, '48;5;226;30m')}")

            title(f"⏳ {APP_NAME}")
            print(styled("(y/n): ", "1m"), end="")
            sys.stdout.flush()
            try:
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = "n"

        # Determine approval type for logging
        if is_safe:
            approval_type = "auto"
        elif answer == "y":
            approval_type = "manual_yes"
        else:
            approval_type = "manual_no"

        if answer != "y":
            return {
                "ok": False,
                "denied": True,
                "error": "user denied",
                "classification": classification,
                "approval_type": approval_type
            }

        try:
            # For auto-approved commands, don't stream output live - only show smart-truncated version
            stream_output = not is_safe
            output_lines, exit_code = run_shell_interactive(cmd, stream_output=stream_output)

            # For auto-approved commands, print smart-truncated output to terminal
            # but send full output to LLM
            if is_safe:
                # Smart truncate: first line, line count, last line
                terminal_output = "\n".join(smart_truncate(output_lines, keep_first=1, keep_last=1, max_line_len=80))
                print(styled(f"$ {cmd}", "90m"))
                print(styled(terminal_output, "90m"))
            else:
                print(f"{styled(f'$ {cmd}', '90m')}")

            # Always send full (but reasonably truncated) output to LLM
            return {
                "ok": True,
                "command": cmd,
                "exit_code": exit_code,
                "output": "\n".join(truncate(output_lines)),
                "classification": classification,
                "approval_type": approval_type,
            }
        except Exception as e:
            return {
                "ok": False,
                "command": cmd,
                "error": str(e),
                "classification": classification,
                "approval_type": approval_type
            }

    def tool_commit_changes(self, args: Dict[str, Any]) -> ToolResult:
        message = args["message"].strip()

        # Format modified Python files before committing
        formatted_files = self._format_modified_python_files()
        if formatted_files:
            print(styled(f"Formatted {len(formatted_files)} Python file(s):", "33m"))
            for f in formatted_files:
                print(f"  - {f}")

        result = _do_git_commit(self.repo_root, message)

        # Automatically compress after successful commit (unit of work is complete)
        if result.get("ok"):
            self.cmd_compress()

        return result

    def _format_modified_python_files(self) -> List[str]:
        """Format all modified Python files in the repo.

        Returns:
            List of formatted file paths.
        """
        formatted: List[str] = []

        # Get list of modified Python files
        try:
            result = subprocess.run(
                ["git", "-C", self.repo_root, "diff", "--name-only", "--diff-filter=AM", "HEAD"],
                capture_output=True,
                text=True,
                timeout=GIT_COMMAND_TIMEOUT,
            )
            if result.returncode != 0:
                return formatted

            modified_files = result.stdout.strip().split('\n')
        except (subprocess.TimeoutExpired, Exception):
            return formatted

        for rel_path in modified_files:
            if not rel_path.endswith('.py'):
                continue

            try:
                full_path = Path(self.repo_root) / rel_path
                if not full_path.exists():
                    continue

                content = full_path.read_text(encoding='utf-8')
                formatted_content = format_python_code(content)

                if formatted_content != content:
                    full_path.write_text(formatted_content, encoding='utf-8')
                    formatted.append(rel_path)
            except Exception:
                continue

        return formatted

    def tool_browser_execute(self, args: Dict[str, Any]) -> ToolResult:
        code = args.get("code", "").strip()
        if not code:
            return {"ok": False, "error": "empty code"}

        print(f"{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')} wants to execute in browser:")
        print(f"  {styled(code, '48;5;236;37m')}")
        title(f"⏳ {APP_NAME} (browser)")

        try:
            req = urllib.request.Request(
                f"http://localhost:{self.bridge_port}/execute",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"code": code}).encode(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read().decode())
            print(styled("✓ Sent to browser extension", "32m"))
            return {"ok": True, "code": code[:80] + "...", "status": "executed", "result": result}
        except Exception as e:
            return {"ok": False, "error": f"bridge not reachable (port {self.bridge_port}): {e}. Make sure Chrome extension is loaded + popup port matches."}

    # =========================================================================
    # SSH Tools
    # =========================================================================

    def tool_ssh_command(self, args: Dict[str, Any]) -> ToolResult:
        """Execute a shell command on a remote server via SSH.

        Args:
            args: Dictionary with 'host', 'command', optional 'user', and optional 'interactive' keys.

        Returns:
            ToolResult with command output or error.
        """
        host = args.get("host", "").strip()
        command = args.get("command", "").strip()
        user = args.get("user", "").strip()
        interactive = args.get("interactive", False)

        if not host:
            return {"ok": False, "error": "empty host"}
        if not command:
            return {"ok": False, "error": "empty command"}

        # Build SSH host string
        ssh_host = host
        if user and "@" not in host:
            ssh_host = f"{user}@{host}"
        elif user and "@" in host:
            # User provided in both places - user param takes precedence
            ssh_host = f"{user}@{host.split('@')[-1]}"

        # Track new hosts for first-time confirmation
        if not hasattr(self, '_ssh_seen_hosts'):
            self._ssh_seen_hosts: set = set()

        is_new_host = ssh_host not in self._ssh_seen_hosts

        # Check command safety
        is_safe, classification = self._is_command_safe(command)

        # Determine if we need approval
        needs_approval = is_new_host or not is_safe

        if needs_approval:
            # Build approval message
            approval_msg = f"SSH Command on {ssh_host}: {command}"
            if is_new_host:
                approval_msg += " (NEW HOST - verify this is correct)"

            if is_new_host:
                print(f"{styled('[NEW HOST] ssh {ssh_host} $ ' + command, '48;5;226;30m')}")
                print(styled("  First connection to this host. Verify host is correct before proceeding.", '1;30m'))
            else:
                print(f"{styled('[APPROVE] ssh {ssh_host} $ ' + command, '48;5;236;37m')}")

            title(f"⏳ {APP_NAME}")
            print(styled("(y/n): ", "1m"), end="")
            sys.stdout.flush()
            try:
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = "n"

            if answer != "y":
                return {
                    "ok": False,
                    "denied": True,
                    "error": "user denied",
                    "classification": classification,
                    "is_new_host": is_new_host
                }

            # Mark host as seen
            self._ssh_seen_hosts.add(ssh_host)

        # Execute SSH command
        try:
            if interactive:
                # Interactive mode: use PTY for password prompts
                ssh_cmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no {ssh_host} '{command}'"
                print(f"{styled(f'ssh {ssh_host} $ {command}', '90m')}")
                print(styled("  [Interactive mode - you can enter passwords when prompted]", "36m"))
                output_lines, exit_code = run_ssh_interactive_pty(ssh_cmd)
            else:
                # Non-interactive mode: standard execution
                ssh_cmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no {ssh_host} '{command}'"
                output_lines, exit_code = run_shell_interactive(ssh_cmd, stream_output=True)
                print(f"{styled(f'ssh {ssh_host} $ {command}', '90m')}")

            return {
                "ok": True,
                "host": ssh_host,
                "command": command,
                "exit_code": exit_code,
                "output": "\n".join(truncate(output_lines)),
                "classification": classification,
                "is_new_host": is_new_host,
                "interactive": interactive,
            }
        except Exception as e:
            return {
                "ok": False,
                "host": ssh_host,
                "command": command,
                "error": str(e),
                "classification": classification,
                "interactive": interactive,
            }

    def tool_invoke_skill(self, args: Dict[str, Any]) -> ToolResult:
        """Load a skill after user approval.

        Args:
            args: Dictionary with 'name' (required) and 'arguments' (optional).

        Returns:
            ToolResult with the rendered skill content or error.
        """
        name = args.get("name", "").strip().lower().replace("_", "-")
        arguments = args.get("arguments", "")

        if not name:
            return {"ok": False, "error": "empty skill name"}

        # Find the skill
        skill = None
        for s in self.skills:
            if s.name == name:
                skill = s
                break

        if skill is None:
            available = ", ".join(f"/{s.name}" for s in self.skills) if self.skills else "(none)"
            return {"ok": False, "error": f"unknown skill: {name!r}. Available: {available}"}

        # Ask user for approval
        desc = skill.description or "(no description)"
        args_display = f" with args: {arguments}" if arguments else ""
        print(f"{styled(f'[SKILL] /{skill.name}{args_display}', '48;5;226;37m')}")
        print(styled(f"  {desc}", '90m'))
        print(styled(f"  Source: {skill.path}", '36m'))
        title(f"⏳ {APP_NAME}")
        print(styled("  Load this skill? (y/n): ", "1m"), end="")
        sys.stdout.flush()
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"

        if answer != "y":
            return {"ok": False, "denied": True, "error": "user denied skill invocation"}

        # Substitute arguments into skill body
        body = _substitute_arguments(skill.body, arguments)

        # Log the skill invocation
        if self._conversation_log:
            self._conversation_log.log_tool_execution(
                tool_name=f"invoke_skill({name})",
                duration_ms=0,
                success=True,
                exit_code=0,
                error=None,
                user_approved=True,
                approval_type="approved",
                classification="skill",
                tool_arguments={"name": name, "arguments": arguments}
            )

        print(styled(f"✓ Skill /{name} loaded", "32m"))
        return {
            "ok": True,
            "skill_name": name,
            "message": f"Skill '{name}' has been loaded with the following instructions:",
            "body": body,
        }

    def execute_tool(self, name: str, args: Dict[str, Any]) -> ToolResult:
        if name == "get_repo_map":
            return self.tool_get_repo_map(args)
        if name == "write_file":
            return self.tool_write_file(args)
        if name == "edit_file":
            return self.tool_edit_file(args)
        if name == "run_shell_command":
            return self.tool_run_shell_command(args)
        if name == "commit_changes":
            return self.tool_commit_changes(args)
        if name == "browser_execute":
            return self.tool_browser_execute(args)
        if name == "ssh_command":
            return self.tool_ssh_command(args)
        if name == "invoke_skill":
            return self.tool_invoke_skill(args)
        return {"ok": False, "error": f"unknown tool: {name}"}

    def run_agent_turn(self, request: str) -> None:
        # Initialize conversation log if needed
        if not self._conversation_log:
            self._conversation_log = ConversationLog(self.repo_root)
            self._conversation_log.start_session()

        # Add user message to conversation history
        user_content = self.build_user_message(request)
        self.messages.append({"role": "user", "content": user_content})

        # Log the user message
        self._conversation_log.append_message({"role": "user", "content": user_content})

        response = self.llama_request(self.get_messages_with_system(), TOOLS)
        if not response:
            return

        loops = 0
        while True:
            loops += 1
            if loops > MAX_TOOL_LOOPS:
                print(styled("Stopped: too many tool loops.", "31m"))
                return

            text = self.extract_text(response)
            reasoning = self.extract_reasoning_content(response)
            tool_calls = self.extract_tool_calls(response)
            finish_reason = self.get_finish_reason(response)

            # Add assistant message to history
            assistant_msg: Dict[str, Any] = {"role": "assistant"}
            if text:
                assistant_msg["content"] = text
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            if text or tool_calls:
                self.messages.append(assistant_msg)
                # Log the assistant message
                self._conversation_log.append_message(assistant_msg)

            # Print reasoning content first if present (thinking output)
            if reasoning and reasoning.strip():
                print(styled("Thinking:", "36m"))
                print(styled(reasoning, "90m"))
                print()

            if text:
                self.print_assistant_text(text)

            # If no tool calls or finish_reason is "stop", we're done
            if not tool_calls or finish_reason == "stop":
                return

            # Process tool calls
            for call in tool_calls:
                call_id = call.get("id", "")
                function = call.get("function", {})
                name = function.get("name", "")
                raw_args = function.get("arguments", "{}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception as e:
                    result = {"ok": False, "error": f"invalid JSON arguments: {e}"}
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result),
                    })
                    continue

                # Format tool call display in a user-friendly way
                display_args = format_tool_call_display(name, args)
                print(
                    f"{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')} "
                    f"{styled(name, '1;36m')} "
                    f"{styled(display_args, '90m')}"
                )

                # Time the tool execution
                tool_start_time = time.time()
                result = self.execute_tool(name, args)
                tool_duration_ms = (time.time() - tool_start_time) * 1000

                # Add tool result to messages
                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
                self.messages.append(tool_result_msg)
                # Log the tool result
                self._conversation_log.append_message(tool_result_msg)

                # Log tool execution metrics
                if self._conversation_log:
                    self._conversation_log.log_tool_execution(
                        tool_name=name,
                        duration_ms=tool_duration_ms,
                        success=result.get("ok", False),
                        exit_code=result.get("exit_code"),
                        error=result.get("error"),
                        user_approved="denied" not in result,
                        approval_type=result.get("approval_type"),
                        classification=result.get("classification"),
                        tool_arguments=args
                    )

            # Continue conversation with tool results
            response = self.llama_request(self.get_messages_with_system(), TOOLS)
            if not response:
                return

    def cmd_add(self, pattern: str) -> None:
        """Show files matching pattern (for reference, agent should use shell commands)."""
        found = glob.glob(pattern, root_dir=self.repo_root, recursive=True)
        files = [f for f in found if Path(self.repo_root, f).is_file()]
        print(styled(f"Files matching '{pattern}':", "36m"))
        for f in sorted(files)[:50]:
            print(f"  {f}")
        if len(files) > 50:
            print(f"  ... and {len(files) - 50} more")
        print(f"\nTotal: {len(files)} files")
        print("Tip: Use 'cat file.py', 'grep pattern file.py', or 'sed' to read files. Use edit_file for modifications.")

    def cmd_ctx(self) -> None:
        """Show current context status for the AI."""
        print(styled("=== Context Status ===", "1;36m"))
        print(styled(f"Initial context sent: {'Yes' if self._initial_context_sent else 'No'}", "90m"))
        print(styled(f"Repo map cache entries: {len(self._map_cache)}", "90m"))
        print(styled(f"Conversation messages: {len(self.messages)}", "90m"))
        print()

    def cmd_compress(self) -> None:
        """Compress conversation by truncating large tool outputs.

        Replaces large outputs with summaries while preserving tool metadata.
        - Repo maps: Replace with file count note
        - Shell commands: Keep exit code, truncate output to last 10 lines
        - File reads: Keep first/last 5 lines, compress middle
        - Other tools: Keep metadata, truncate large outputs
        """
        compressed_count = 0
        bytes_saved = 0

        for msg in self.messages:
            if msg.get("role") != "tool":
                continue

            content = msg.get("content", "")
            if not content:
                continue

            old_size = len(content)

            try:
                result = json.loads(content)

                # Check if this was a repo map tool call
                if result.get("ok") and "file_count" in result and "output" not in result:
                    # This is likely a repo map result - compress it
                    file_count = result.get("file_count", 0)
                    result = {
                        "ok": True,
                        "compressed": True,
                        "note": f"Repository map compressed (showed {file_count} files)"
                    }
                    msg["content"] = json.dumps(result)
                    compressed_count += 1
                    bytes_saved += old_size - len(msg["content"])
                    continue

                # Handle shell command outputs
                if result.get("ok") and "output" in result:
                    output = result["output"]
                    if isinstance(output, str) and len(output) > 500:
                        lines = output.split("\n")
                        if len(lines) > 20:
                            # Keep first 5 and last 5 lines
                            kept_lines = lines[:5] + [f"  ... ({len(lines) - 10} lines compressed) ..."] + lines[-5:]
                            result["output"] = "\n".join(kept_lines)
                            result["compressed"] = True
                            msg["content"] = json.dumps(result)
                            if old_size > len(msg["content"]):
                                compressed_count += 1
                                bytes_saved += old_size - len(msg["content"])

                # Handle file read/write operations with large content
                elif "content" in result and isinstance(result["content"], str):
                    if len(result["content"]) > 1000:
                        lines = result["content"].split("\n")
                        if len(lines) > 20:
                            kept_lines = lines[:5] + [f"  ... ({len(lines) - 10} lines compressed) ..."] + lines[-5:]
                            result["content"] = "\n".join(kept_lines)
                            result["compressed"] = True
                            msg["content"] = json.dumps(result)
                            if old_size > len(msg["content"]):
                                compressed_count += 1
                                bytes_saved += old_size - len(msg["content"])

                # Generic compression for any large output field
                elif old_size > 2000:
                    # Truncate the entire JSON if it's very large
                    result["compressed"] = True
                    result["note"] = f"Tool result compressed ({old_size:,} bytes)"
                    msg["content"] = json.dumps(result)
                    compressed_count += 1
                    bytes_saved += old_size - len(msg["content"])

            except (json.JSONDecodeError, KeyError, TypeError):
                # If we can't parse it, leave it alone
                pass

        if compressed_count == 0:
            print(styled("No tool outputs large enough to compress.", "93m"))
        else:
            print(styled(f"Compressed {compressed_count} tool result(s)", "32m"))
            print(styled(f"Saved ~{bytes_saved:,} bytes", "90m"))
            # Recalculate token estimate based on compressed content
            self.total_tokens = self._estimate_tokens_from_messages()
            self._tokens_estimated = True
            print(styled(f"Estimated tokens after compression: ~{self.total_tokens:,}", "90m"))

    def _estimate_tokens_from_messages(self) -> int:
        """Estimate total tokens from current messages using bytes/4 heuristic.

        Returns:
            Estimated token count based on message content size.
        """
        total_bytes = 0
        for msg in self.messages:
            content = msg.get("content", "")
            if content:
                total_bytes += len(content)
        # Rough estimate: ~4 bytes per token for English text
        return max(0, total_bytes // 4)

    def cmd_status(self) -> None:
        print(styled(f"Repository: {self.repo_root}", "36m"))
        print(styled(f"Bridge: integrated (port {self.bridge_port})", "36m"))
        print(styled(f"Server: {LLAMA_HOST}", "36m"))
        global _bridge_state
        if _bridge_state.get("url"):
            print(styled(f"Browser: {_bridge_state.get('title', 'No title')} @ {_bridge_state.get('url')}", "36m"))
        if self.messages:
            print(styled(f"Conversation: {len(self.messages)} messages", "32m"))
        print(styled(f"Model: {MODEL}", "90m"))
        print(styled(f"Total tokens: ~{self.total_tokens:,}", "90m"))

    def cmd_log(self, session_id: str = "") -> None:
        """List conversation sessions or show details of a specific session.

        Args:
            session_id: Optional session ID to show details for.
        """
        if not self._conversation_log:
            self._conversation_log = ConversationLog(self.repo_root)

        sessions = self._conversation_log.list_sessions(repo_path=self.repo_root)

        if not sessions:
            print(styled("No conversation sessions found.", "93m"))
            print(styled("  Sessions are saved to ~/.localcode/conversations.db", "90m"))
            return

        if session_id:
            # Show details of specific session
            for s in sessions:
                if s["id"] == session_id:
                    relative_time = self._conversation_log.format_relative_time(s["started_at"])
                    print(styled(f"Session started {relative_time}", "1;36m"))
                    print(styled(f"  Messages: {s['messages']}", "90m"))
                    print(styled(f"  Started: {self._conversation_log.format_timestamp(s['started_at'])}", "90m"))
                    print(styled(f"  Last activity: {self._conversation_log.format_timestamp(s['last_message_at'])}", "90m"))
                    return

            print(styled(f"Session not found: {session_id}", "31m"))
            return

        # List all sessions with interactive selection
        print(styled("Recent conversations", "1;36m"))
        print(styled("Type the number to load a session", "90m"))
        print()

        for i, s in enumerate(sessions[:15], 1):
            relative_time = self._conversation_log.format_relative_time(s["last_message_at"])
            msg_count = s["messages"]

            # Build a nice preview from the last user message
            preview = ""
            if s.get("last_user_message"):
                content = s["last_user_message"].get("content", "")
                request_match = re.search(r"### Request\n(.+)", content, re.DOTALL)
                if request_match:
                    preview = request_match.group(1).strip()
                else:
                    preview = content[:80]
                preview = preview.replace("\n", " ").strip()
                if len(preview) > 60:
                    preview = preview[:57] + "..."

            # Format the line
            time_colored = styled(relative_time, "36m") if relative_time in ("just now", "1 minute ago", "2 minutes ago", "3 minutes ago", "4 minutes ago", "5 minutes ago") else relative_time

            print(f"  {i}. {time_colored} — {msg_count} messages")
            if preview:
                preview_wrapped = styled(f"    \"{preview}\"", "90m")
                print(preview_wrapped)
            print()

        if len(sessions) > 15:
            print(styled(f"  ... and {len(sessions) - 15} more sessions (use /log for details)", "90m"))
            print()

        print("Commands:")
        print("  /load <number> - Load a session")
        print("  /log <session_id> - Show session details")

    def cmd_load(self, session_id: str) -> None:
        """Load a previous conversation session.

        Args:
            session_id: Session ID or number from /log list.
        """
        if not self._conversation_log:
            self._conversation_log = ConversationLog(self.repo_root)

        # Check if it's a number (index from /log list)
        sessions = self._conversation_log.list_sessions(repo_path=self.repo_root)
        if session_id.isdigit():
            idx = int(session_id) - 1
            if 0 <= idx < len(sessions):
                session_id = sessions[idx]["id"]
            else:
                print(styled(f"Invalid session number: {session_id}", "31m"))
                return

        messages = self._conversation_log.load_session(session_id)

        if not messages:
            print(styled(f"Session not found or empty: {session_id}", "31m"))
            return

        # Clear current conversation and load the session
        self.messages = messages
        self._initial_context_sent = True
        self.total_tokens = 0
        self._tokens_estimated = False

        print(styled(f"Loaded {len(messages)} messages from {session_id}", "32m"))

        # Show summary of loaded session
        user_msgs = sum(1 for m in messages if m.get("role") == "user")
        assistant_msgs = sum(1 for m in messages if m.get("role") == "assistant")
        tool_msgs = sum(1 for m in messages if m.get("role") == "tool")
        print(styled(f"  User messages: {user_msgs}", "90m"))
        print(styled(f"  Assistant messages: {assistant_msgs}", "90m"))
        print(styled(f"  Tool results: {tool_msgs}", "90m"))

    def cmd_skills(self) -> None:
        """List all available skills."""
        # Re-discover skills in case they changed
        self.skills = _discover_skills()

        if not self.skills:
            print(styled("No skills found.", "93m"))
            print(styled("  Create skills in ~/.localcode/skills/<name>/SKILL.md", "90m"))
            print(styled("  or .localcode/skills/<name>/SKILL.md", "90m"))
            return

        print(styled(f"Available skills ({len(self.skills)}):", "1;36m"))
        for skill in self.skills:
            flags = []
            if skill.disable_model_invocation:
                flags.append("user-only")
            if not skill.user_invocable:
                flags.append("model-only")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            print(f"  {styled(f'/{skill.name}', '1;37m')}{flag_str} — {skill.description or '(no description)'}")
            source = "personal" if str(Path.home()) in str(skill.path) else "project"
            print(styled(f"    {source}: {skill.path}", "90m"))

    def _announce_agents_md(self) -> None:
        """Announce if AGENTS.md was loaded and from where."""
        if _AGENTS_MD_PATH:
            source = "project" if not str(Path.home()) in str(_AGENTS_MD_PATH) else "user"
            print(styled(f"AGENTS.md loaded from {source}: {_AGENTS_MD_PATH}", "90m"))
        if _CLAUDE_MD_PATH:
            source = "project" if not str(Path.home()) in str(_CLAUDE_MD_PATH) else "user"
            print(styled(f"CLAUDE.md loaded from {source}: {_CLAUDE_MD_PATH}", "90m"))

    def shell_user_command(self, shell_cmd: str) -> None:
        output_lines, exit_code = run_shell_interactive(shell_cmd)
        title(f"❓ {APP_NAME}")
        try:
            answer = input("\aAdd to context? [t]runcated/[f]ull/[n]o: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"

        if answer in ("t", "f"):
            body = "\n".join(truncate(output_lines) if answer == "t" else output_lines)
            self.pending_notes.append(f"$ {shell_cmd}\n{body}")
            print(styled("Added to context", "93m"))

    def repl(self) -> None:
        print(
            f"{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')}"
            f" {styled(' ' + MODEL + ' ', '48;5;236;37m')}"
            f" {styled(' ' + LLAMA_HOST + ' ', '48;5;236;90m')}"
            f" {styled(' ctrl+d to send ', '48;5;236;37m')}"
        )

        self.cmd_skills()
        self._announce_agents_md()

        while True:
            title(f"❓ {APP_NAME}")
            last = self.last_usage or {}

            # Use estimated tokens after compression, otherwise use actual API tokens
            if self._tokens_estimated:
                prompt_tokens = self.total_tokens
                completion_tokens = 0
                token_label = "est"
            else:
                prompt_tokens = last.get("prompt_tokens", 0)
                completion_tokens = last.get("completion_tokens", 0)
                token_label = "actual"

            # Status line for local models (no cost, just tokens)
            print(
                styled(f"input: {prompt_tokens:,}" + (" [est]" if self._tokens_estimated else ""), "90m")
                + styled(" • ", "2;90m")
                + styled(f"msgs: {len(self.messages)}", "2;90m")
            )
            print(f"\a{styled('❯ ', '48;2;60;60;180;37m')}", end="", flush=True)
            input_lines = []
            try:
                while True:
                    input_lines.append(input())
            except EOFError:
                if not input_lines:
                    print("\nGoodbye!")
                    title("")
                    break
            except KeyboardInterrupt:
                print()
                continue

            user_input = "\n".join(input_lines).strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                command, _, arg = user_input.partition(" ")
                if command == "/exit":
                    print("Bye!")
                    title("")
                    break
                elif command == "/add":
                    self.cmd_add(arg)
                elif command == "/clear":
                    self.messages.clear()
                    self.pending_notes.clear()
                    self.total_tokens = 0
                    self._tokens_estimated = False
                    self._initial_context_sent = False  # Reset context tracking
                    self._map_cache.clear()
                    self._map_mtime.clear()
                    print("Conversation cleared (context tracking reset).")
                elif command == "/undo":
                    out = run(f"git -C {self.repo_root} reset --hard HEAD~1")
                    if out:
                        print(out)
                elif command == "/ctx":
                    self.cmd_ctx()
                elif command == "/status":
                    self.cmd_status()
                elif command == "/compress":
                    self.cmd_compress()
                elif command == "/log":
                    self.cmd_log(arg.strip())
                elif command == "/load":
                    self.cmd_load(arg.strip())
                elif command == "/skills":
                    self.cmd_skills()
                elif command == "/help":
                    print("/add <glob> - List files matching pattern")
                    print("/ctx - Show context status")
                    print("/status - Show repo info")
                    print("/compress - Compress large tool outputs (repo maps, shell outputs, etc.)")
                    print("/log - List conversation sessions")
                    print("/load <session_id> - Load a previous session")
                    print("/skills - List available skills")
                    print("/<skill-name> - Invoke a skill")
                    print("/clear - Clear conversation")
                    print("/undo - Undo commit")
                    print("/exit - Exit")
                    print("!<cmd> - Shell command")
                    print()
                    print("Tools:")
                    print("  get_repo_map - Show repo structure with line numbers")
                    print("  write_file - Create/overwrite files")
                    print("  edit_file - Find/replace edits")
                    print("  run_shell_command - Run shell commands (cat, grep, etc.)")
                    print("  commit_changes - Git commit")
                    print("  browser_execute - Run JS in browser")
                    print("  invoke_skill - Load a skill for extended capabilities")
                else:
                    # Try to invoke a skill by name
                    skill = None
                    for s in self.skills:
                        if s.name == command:
                            skill = s
                            break
                    if skill and skill.user_invocable:
                        body = _substitute_arguments(skill.body, arg.strip())
                        self.run_agent_turn(f"Invoke skill /{skill.name}:\n\n{body}")
                    else:
                        print(styled(f"Unknown command: {command}", "31m"))
                continue

            if user_input.startswith("!"):
                shell_cmd = user_input[1:].strip()
                if shell_cmd:
                    self.shell_user_command(shell_cmd)
                continue

            self.run_agent_turn(user_input)

# === Integrated Browser Bridge ===
_bridge_lock: threading.Lock = threading.Lock()
_bridge_pending: Optional[str] = None
_bridge_result: Optional[ToolResult] = None
_bridge_state: Dict[str, Union[str, int]] = {"url": "", "title": "", "timestamp": 0}
_bridge_pending_time: float = 0.0

class BridgeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith('/command'):
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            global _bridge_pending, _bridge_pending_time
            with _bridge_lock:
                if _bridge_pending and (time.time() - _bridge_pending_time < BROWSER_COMMAND_TIMEOUT):
                    resp = {'command': 'execute', 'code': _bridge_pending}
                    _bridge_pending = None
                    _bridge_pending_time = 0.0
                    self.wfile.write(json.dumps(resp).encode('utf-8'))
                else:
                    self.wfile.write(json.dumps({}).encode('utf-8'))
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Accept')
        self.end_headers()

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8')) if content_length > 0 else {}

            global _bridge_pending, _bridge_result, _bridge_state, _bridge_pending_time

            if self.path.startswith('/execute'):
                with _bridge_lock:
                    _bridge_pending = data.get('code', '')
                    _bridge_pending_time = time.time()
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                start_time = time.time()
                while time.time() - start_time < BROWSER_EXECUTE_TIMEOUT:
                    with _bridge_lock:
                        if _bridge_result is not None:
                            result = _bridge_result
                            _bridge_result = None
                            _bridge_pending = None
                            _bridge_pending_time = 0.0
                            self.wfile.write(json.dumps(result).encode('utf-8'))
                            return
                    time.sleep(0.3)
                with _bridge_lock:
                    _bridge_pending = None
                    _bridge_pending_time = 0.0
                self.wfile.write(json.dumps({
                    'ok': False,
                    'error': 'timeout waiting for browser response'
                }).encode('utf-8'))

            elif self.path.startswith('/result'):
                with _bridge_lock:
                    _bridge_result = data
                    _bridge_pending = None
                    _bridge_pending_time = 0.0
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

            elif self.path.startswith('/update'):
                with _bridge_lock:
                    _bridge_state.clear()
                    _bridge_state.update(data)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'updated'}).encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

    def log_message(self, format: str, *args: str) -> None:
        pass  # quiet

# =========================================================================
# =========================================================================
# =========================================================================
# Conversation Logging (SQLite-based)
# =========================================================================

class ConversationLog:
    """Persistent logging for conversations using SQLite.

    Stores conversation logs in the user's home directory to prevent
    accidental git commits. Uses a single SQLite database for easy
    filtering and querying.
    """

    _db_path: Optional[Path] = None

    @classmethod
    def _get_db_path(cls) -> Path:
        """Get the path to the conversations database."""
        if cls._db_path is None:
            home_dir = Path.home()
            db_dir = home_dir / ".localcode"
            db_dir.mkdir(parents=True, exist_ok=True)
            cls._db_path = db_dir / "conversations.db"
            cls._init_db()
        return cls._db_path

    @classmethod
    def _init_db(cls) -> None:
        """Initialize the database schema if it doesn't exist."""
        if cls._db_path is None:
            return
        conn = sqlite3.connect(cls._db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                repo_path TEXT NOT NULL,
                repo_name TEXT,
                started_at REAL NOT NULL,
                last_message_at REAL NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS model_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                latency_ms REAL NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                cache_hits INTEGER DEFAULT 0,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tool_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_id INTEGER,
                tool_name TEXT NOT NULL,
                tool_arguments TEXT,
                started_at REAL NOT NULL,
                completed_at REAL,
                duration_ms REAL,
                success INTEGER NOT NULL,
                exit_code INTEGER,
                error_message TEXT,
                user_approved INTEGER DEFAULT 1,
                approval_type TEXT,
                classification TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (message_id) REFERENCES messages(id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                tool_execution_id INTEGER,
                operation TEXT NOT NULL,
                file_path TEXT NOT NULL,
                success INTEGER NOT NULL,
                user_confirmed INTEGER DEFAULT 1,
                lines_changed INTEGER,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        ''')
        # Migrate: add tool_arguments column if it doesn't exist yet
        try:
            cursor.execute('ALTER TABLE tool_executions ADD COLUMN tool_arguments TEXT')
        except sqlite3.OperationalError:
            pass  # column already exists

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo_path)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sessions_time ON sessions(last_message_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_requests_session ON model_requests(session_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tool_executions_session ON tool_executions(session_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tool_executions_name ON tool_executions(tool_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_operations_session ON file_operations(session_id)')
        conn.commit()
        conn.close()

    def __init__(self, repo_root: str) -> None:
        """Initialize conversation logger for a specific repository.

        Args:
            repo_root: The root path of the current repository.
        """
        self.repo_root = repo_root
        self.repo_name = Path(repo_root).name
        self.current_session_id: Optional[str] = None
        self._start_time: Optional[float] = None

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self._get_db_path())

    def start_session(self) -> str:
        """Start a new conversation session.

        Returns:
            Session ID (timestamp-based).
        """
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = f"session_{timestamp}"
        self.current_session_id = session_id
        self._start_time = time.time()

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO sessions (id, repo_path, repo_name, started_at, last_message_at) VALUES (?, ?, ?, ?, ?)',
            (session_id, self.repo_root, self.repo_name, self._start_time, self._start_time)
        )
        conn.commit()
        conn.close()

        return session_id

    def append_message(self, message: Message) -> None:
        """Append a message to the current session.

        Args:
            message: Message dict with role and content.
        """
        if not self.current_session_id:
            self.start_session()

        conn = self._get_conn()
        cursor = conn.cursor()

        # Insert the message
        content_json = json.dumps(message, ensure_ascii=False)
        cursor.execute(
            'INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)',
            (self.current_session_id, message.get("role", "unknown"), content_json, time.time())
        )

        # Update last_message_at for the session
        cursor.execute(
            'UPDATE sessions SET last_message_at = ? WHERE id = ?',
            (time.time(), self.current_session_id)
        )

        conn.commit()
        conn.close()

    def load_session(self, session_id: str) -> List[Message]:
        """Load messages from a specific session.

        Args:
            session_id: The session ID to load.

        Returns:
            List of messages from the session.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT content FROM messages WHERE session_id = ? ORDER BY created_at ASC',
            (session_id,)
        )
        rows = cursor.fetchall()
        conn.close()

        messages = []
        for row in rows:
            try:
                messages.append(json.loads(row[0]))
            except json.JSONDecodeError:
                continue
        return messages

    def list_sessions(self, repo_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """List available sessions with metadata.

        Args:
            repo_path: Optional filter by repository path. If None, returns all sessions.

        Returns:
            List of session info dicts sorted by last message time (newest first).
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        if repo_path:
            cursor.execute(
                '''SELECT id, repo_path, repo_name, started_at, last_message_at,
                    (SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id) as message_count,
                    (SELECT content FROM messages WHERE messages.session_id = sessions.id AND role = 'user' ORDER BY created_at DESC LIMIT 1) as last_user_message
                    FROM sessions WHERE repo_path = ? ORDER BY last_message_at DESC''',
                (repo_path,)
            )
        else:
            cursor.execute(
                '''SELECT id, repo_path, repo_name, started_at, last_message_at,
                    (SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id) as message_count,
                    (SELECT content FROM messages WHERE messages.session_id = sessions.id AND role = 'user' ORDER BY created_at DESC LIMIT 1) as last_user_message
                    FROM sessions ORDER BY last_message_at DESC'''
            )

        rows = cursor.fetchall()
        conn.close()

        sessions = []
        for row in rows:
            # Parse the last user message JSON
            last_user_msg = None
            if row[6]:
                try:
                    last_user_msg = json.loads(row[6])
                except json.JSONDecodeError:
                    last_user_msg = None

            sessions.append({
                "id": row[0],
                "repo_path": row[1],
                "repo_name": row[2],
                "started_at": row[3],
                "last_message_at": row[4],
                "messages": row[5],
                "last_user_message": last_user_msg
            })
        return sessions

    def format_timestamp(self, timestamp: float) -> str:
        """Format a Unix timestamp for display.

        Args:
            timestamp: Unix timestamp.

        Returns:
            Formatted timestamp string.
        """
        dt = datetime.datetime.fromtimestamp(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def format_relative_time(self, timestamp: float) -> str:
        """Format a timestamp as relative time (e.g., '2 hours ago').

        Args:
            timestamp: Unix timestamp.

        Returns:
            Human-readable relative time string.
        """
        now = datetime.datetime.now()
        dt = datetime.datetime.fromtimestamp(timestamp)
        diff = now - dt
        seconds = diff.total_seconds()

        if seconds < 60:
            return "just now"
        elif seconds < 120:
            return "1 minute ago"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes} minutes ago"
        elif seconds < 7200:
            return "1 hour ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hours ago"
        elif seconds < 172800:
            return "yesterday"
        elif seconds < 259200:
            return "2 days ago"
        elif seconds < 604800:
            days = int(seconds / 86400)
            return f"{days} days ago"
        elif seconds < 1209600:
            return "1 week ago"
        elif seconds < 2419200:
            weeks = int(seconds / 604800)
            return f"{weeks} weeks ago"
        elif seconds < 31536000:
            weeks = int(seconds / 604800)
            return f"{weeks} weeks ago"
        else:
            years = int(seconds / 31536000)
            return f"{years} year{'s' if years != 1 else ''} ago"

    def log_model_request(self, latency_ms: float, prompt_tokens: int,
                          completion_tokens: int, cache_hits: int = 0) -> None:
        """Log model request metrics.

        Args:
            latency_ms: Response time in milliseconds.
            prompt_tokens: Number of prompt tokens.
            completion_tokens: Number of completion tokens.
            cache_hits: Number of cached tokens.
        """
        if not self.current_session_id:
            return

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO model_requests
               (session_id, timestamp, latency_ms, prompt_tokens, completion_tokens, cache_hits)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (self.current_session_id, time.time(), latency_ms,
             prompt_tokens, completion_tokens, cache_hits)
        )
        conn.commit()
        conn.close()

    def log_tool_execution(self, tool_name: str, duration_ms: float, success: bool,
                          exit_code: Optional[int] = None, error: Optional[str] = None,
                          user_approved: bool = True, approval_type: Optional[str] = None,
                          classification: Optional[str] = None,
                          tool_arguments: Optional[Dict[str, Any]] = None) -> int:
        """Log tool execution with timing and outcome.

        Args:
            tool_name: Name of the tool executed.
            duration_ms: Execution time in milliseconds.
            success: Whether the tool execution was successful.
            exit_code: Exit code (for shell commands).
            error: Error message if failed.
            user_approved: Whether user approved (for dangerous operations).
            approval_type: Type of approval ('auto', 'manual_yes', 'manual_no').
            classification: Command classification ('safe', 'dangerous', 'malicious').
            tool_arguments: The raw arguments dict passed to the tool (stored as JSON).

        Returns:
            The ID of the inserted tool execution record.
        """
        if not self.current_session_id:
            return -1

        started_at = time.time() - (duration_ms / 1000.0)
        args_json = json.dumps(tool_arguments, ensure_ascii=False) if tool_arguments else None

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO tool_executions
               (session_id, tool_name, tool_arguments, started_at, completed_at, duration_ms,
                success, exit_code, error_message, user_approved, approval_type, classification)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (self.current_session_id, tool_name, args_json, started_at, time.time(), duration_ms,
             1 if success else 0, exit_code, error,
             1 if user_approved else 0, approval_type, classification)
        )
        tool_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return tool_id

    def log_file_operation(self, operation: str, file_path: str, success: bool,
                          user_confirmed: bool = True, lines_changed: Optional[int] = None,
                          tool_execution_id: Optional[int] = None) -> None:
        """Log a file operation.

        Args:
            operation: Type of operation ('read', 'write', 'edit', 'delete').
            file_path: Path to the file.
            success: Whether the operation succeeded.
            user_confirmed: Whether user confirmed (for overwrites).
            lines_changed: Number of lines changed (for edits).
            tool_execution_id: Associated tool execution ID.
        """
        if not self.current_session_id:
            return

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO file_operations
               (session_id, tool_execution_id, operation, file_path, success,
                user_confirmed, lines_changed)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (self.current_session_id, tool_execution_id, operation, file_path,
             1 if success else 0, 1 if user_confirmed else 0, lines_changed)
        )
        conn.commit()
        conn.close()

def _do_git_commit(repo_root: str, message: str) -> ToolResult:
    """Perform a git commit operation.

    Args:
        repo_root: Repository root path.
        message: Commit message.

    Returns:
        Dict with ok status and result details.
    """
    if not message or not message.strip():
        return {"ok": False, "error": "empty commit message"}

    # Validate repo_root to prevent command injection
    try:
        safe_repo_root = validate_path_for_shell(repo_root)
    except ValueError as e:
        return {"ok": False, "error": f"Invalid repo path: {e}"}

    # Use subprocess with list arguments instead of shell commands
    try:
        # Check git status
        status_result = subprocess.run(
            ["git", "-C", safe_repo_root, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT,
        )
        if not status_result.stdout.strip():
            return {"ok": False, "error": "nothing to commit"}

        # Stage all changes
        add_result = subprocess.run(
            ["git", "-C", safe_repo_root, "add", "-A"],
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT,
        )
        if add_result.returncode != 0:
            return {"ok": False, "error": f"git add failed: {add_result.stderr.strip()}"}

        # Commit changes
        commit_result = subprocess.run(
            ["git", "-C", safe_repo_root, "commit", "-m", message],
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT,
        )
        if commit_result.returncode != 0:
            return {"ok": False, "error": f"git commit failed: {commit_result.stderr.strip()}"}

        output = commit_result.stdout.strip()
        print(styled(output, "32m"))
        return {"ok": True, "message": message, "git": output}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git command timed out"}
    except Exception as e:
        return {"ok": False, "error": f"git error: {e}"}

def main() -> None:
    LocalCode().repl()

def commit_changes(message: str) -> ToolResult:
    """Standalone commit function for tool usage."""
    repo_root_raw = run("git rev-parse --show-toplevel") or os.getcwd()
    return _do_git_commit(repo_root_raw, message)

if __name__ == "__main__":
    main()
