"""Notes from the user to AI coding assistants (Claude Code, Cursor, ...).

They live in AGENT_NOTES.md at the project root. CLAUDE.md imports that file and AGENTS.md tells
other assistants to read it, so every new AI session sees the open notes. Finished notes move to
AGENT_NOTES_ARCHIVE.md, which isn't loaded automatically (keeps the AI's context small).

File format: a fixed header, then one section per note:

    ## 2026-11-17 15:02 | bug | open
    The text of the note.

    <details><summary>Board snapshot</summary> ... </details>
"""
import datetime
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES_PATH = os.path.join(ROOT, "AGENT_NOTES.md")
ARCHIVE_PATH = os.path.join(ROOT, "AGENT_NOTES_ARCHIVE.md")
KINDS = ["bug", "test result", "idea", "instruction"]

HEADER = """# Notes for AI agents

Written by the user in the Focus Device developer panel (Developer > Agent notes).
**AI assistants: read these before working on this project.** They are bug reports, results from
testing real hardware, ideas and standing instructions. When you've handled a note, change `open`
to `done` in its heading and say what you did under it. The panel moves done notes to
AGENT_NOTES_ARCHIVE.md.
"""
ARCHIVE_HEADER = """# Archived notes for AI agents

Notes the user marked done. Kept for history; not loaded into AI sessions automatically.
"""
_ENTRY = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \| ([a-z ]+?) \| (open|done)\s*$")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def load(path=None):
    """List of notes, oldest first: {"when", "kind", "status", "body"}."""
    path = path or NOTES_PATH
    notes, cur = [], None
    for line in _read(path).splitlines():
        m = _ENTRY.match(line)
        if m:
            cur = {"when": m.group(1), "kind": m.group(2), "status": m.group(3), "body": []}
            notes.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    for n in notes:
        n["body"] = "\n".join(n["body"]).strip("\n")
    return notes


def save(notes, path=None, header=HEADER):
    path = path or NOTES_PATH
    parts = [header.rstrip("\n")]
    for n in notes:
        parts.append("## %s | %s | %s\n%s" % (n["when"], n["kind"], n["status"], n["body"].strip("\n")))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n\n".join(parts) + "\n")
    os.replace(tmp, path)


def _clean(text):
    # A line that looks like a note heading would split the note when the file is read back.
    out = []
    for line in text.strip().splitlines():
        out.append("#" + line if line.startswith("## ") else line)
    return "\n".join(out)


def add(kind, text, snapshot=None, path=None, when=None):
    """Append a note. Returns it. kind is one of KINDS; snapshot is Markdown (or None)."""
    text = _clean(text)
    if not text:
        raise ValueError("the note is empty")
    kind = kind.lower().strip() if kind.lower().strip() in KINDS else "instruction"
    when = when or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    body = text
    if snapshot:
        body += "\n\n<details><summary>Board snapshot</summary>\n\n%s\n\n</details>" % snapshot.strip()
    notes = load(path)
    note = {"when": when, "kind": kind, "status": "open", "body": body}
    notes.append(note)
    save(notes, path)
    return note


def set_status(index, status, path=None):
    notes = load(path)
    notes[index]["status"] = status
    save(notes, path)


def delete(index, path=None):
    notes = load(path)
    del notes[index]
    save(notes, path)


def archive_done(path=None, archive_path=None):
    """Move done notes to the archive file. Returns how many moved."""
    archive_path = archive_path or ARCHIVE_PATH
    notes = load(path)
    done = [n for n in notes if n["status"] == "done"]
    if not done:
        return 0
    save(load(archive_path) + done, archive_path, ARCHIVE_HEADER)
    save([n for n in notes if n["status"] != "done"], path)
    return len(done)


def ensure_file(path=None):
    path = path or NOTES_PATH
    if not os.path.exists(path):
        save([], path)
