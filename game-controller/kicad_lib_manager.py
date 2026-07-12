#!/usr/bin/env python3
"""
kicad_lib_manager.py
=====================

A wrapper around `easyeda2kicad` that maintains ONE combined KiCad library
(one .kicad_sym symbol file, one .pretty footprint folder, one .3dshapes
3D-model folder) living inside your KiCad project directory.

It's driven by a simple interactive text menu -- just run it and pick
options, no need to remember command-line flags. It also registers the
library in the project's sym-lib-table / fp-lib-table files so it shows up
automatically in KiCad.

Requirements
------------
    pip install easyeda2kicad   (this is also done automatically for you)

Layout created
--------------
<project_dir>/
    <lib_subdir>/
        <lib_name>.kicad_sym        <- all symbols
        <lib_name>.pretty/          <- all footprints
        <lib_name>.3dshapes/        <- all 3D models
        parts.json                  <- metadata (LCSC id -> symbol/footprint/models)
    sym-lib-table                   <- entry added automatically
    fp-lib-table                    <- entry added automatically

Usage
-----
    python3 kicad_lib_manager.py

    ... then use the on-screen menu to list / add / remove parts.

Optional startup flags (all can also be changed later from the Settings
menu once the program is running):
    --project-dir DIR   Directory containing the .kicad_pro file (default: .)
    --lib-subdir NAME   Subfolder inside project-dir to hold the library
                         (default: easyeda_lib)
    --lib-name NAME     Base filename for the library (default: parts)
    --no-auto-install   Start with the automatic 'pip install --upgrade
                         easyeda2kicad' check turned off
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

METADATA_FILENAME = "parts.json"


# --------------------------------------------------------------------------- #
# Low level helpers
# --------------------------------------------------------------------------- #

def pip_install_or_upgrade_easyeda2kicad() -> bool:
    """Run `pip install --upgrade easyeda2kicad`. This installs it if missing,
    or upgrades it to the latest version if already present. Returns True on
    success, False if pip itself failed (e.g. no network) -- in which case we
    fall back to whatever is already installed, if anything."""
    print("Checking for easyeda2kicad updates (pip install --upgrade easyeda2kicad) ...")

    def run_pip(extra_args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "easyeda2kicad", *extra_args],
            capture_output=True, text=True,
        )

    try:
        result = run_pip([])
        if result.returncode != 0 and "externally-managed-environment" in result.stderr:
            # Common on Debian/Ubuntu (PEP 668). Safe to override here since we're
            # only touching the single easyeda2kicad package, not system packages.
            print("    System Python is externally managed; retrying with --break-system-packages ...")
            result = run_pip(["--break-system-packages"])

        if result.returncode != 0:
            print(
                "WARNING: pip install/upgrade of easyeda2kicad failed:\n"
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )
            return False
        # pip prints "Requirement already satisfied" or "Successfully installed ..."
        last_line = next((l for l in reversed(result.stdout.strip().splitlines()) if l.strip()), "")
        print(f"    {last_line}")
        return True
    except FileNotFoundError:
        print("WARNING: could not run pip (python -m pip not available); skipping auto-install/upgrade.", file=sys.stderr)
        return False


def find_easyeda2kicad_cmd(auto_install: bool = True) -> list[str]:
    """Return the command (as an argv list) used to invoke easyeda2kicad,
    installing or upgrading it via pip first unless auto_install is False."""
    if auto_install:
        pip_install_or_upgrade_easyeda2kicad()

    exe = shutil.which("easyeda2kicad")
    if exe:
        return [exe]
    # Fall back to running it as a module with the current Python interpreter
    # (handy when installed into KiCad's bundled Python).
    try:
        subprocess.run(
            [sys.executable, "-m", "easyeda2kicad", "--help"],
            capture_output=True, check=True,
        )
        return [sys.executable, "-m", "easyeda2kicad"]
    except Exception:
        pass

    print(
        "ERROR: could not find or install the 'easyeda2kicad' tool.\n"
        "Try installing it manually:  pip install easyeda2kicad",
        file=sys.stderr,
    )
    sys.exit(1)


def top_level_symbol_names(content: str) -> set[str]:
    """Return the names of the top-level (symbol "Name" ...) entries in a
    .kicad_sym file, ignoring nested sub-unit symbols such as Name_0_1."""
    names: set[str] = set()
    depth = 0
    in_str = False
    i, n = 0, len(content)
    while i < n:
        c = content[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "(":
            depth += 1
            if depth == 2 and content.startswith("(symbol ", i):
                m = re.match(r'\(symbol\s+"([^"]+)"', content[i:i + 400])
                if m:
                    names.add(m.group(1))
            i += 1
            continue
        if c == ")":
            depth -= 1
            i += 1
            continue
        i += 1
    return names


def extract_symbol_block(content: str, name: str) -> str | None:
    """Return the full text of the top-level (symbol "name" ...) block."""
    marker = f'(symbol "{name}"'
    idx = content.find(marker)
    if idx == -1:
        return None
    depth = 0
    in_str = False
    i, n = idx, len(content)
    while i < n:
        c = content[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    return content[idx:i]


def remove_symbol_block(content: str, name: str) -> tuple[str, bool]:
    """Remove the top-level (symbol "name" ...) block from a .kicad_sym
    file's text. Returns (new_content, removed)."""
    marker = f'(symbol "{name}"'
    idx = content.find(marker)
    if idx == -1:
        return content, False
    depth = 0
    in_str = False
    i, n = idx, len(content)
    while i < n:
        c = content[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    end = i
    start = idx
    while start > 0 and content[start - 1] in " \t":
        start -= 1
    if start > 0 and content[start - 1] == "\n":
        start -= 1
    return content[:start] + content[end:], True


def extract_property(block: str, prop_name: str) -> str | None:
    m = re.search(rf'\(property\s+"{re.escape(prop_name)}"\s+"((?:[^"\\]|\\.)*)"', block)
    if not m:
        return None
    return m.group(1).replace('\\"', '"')


# --------------------------------------------------------------------------- #
# Library-table (sym-lib-table / fp-lib-table) handling
# --------------------------------------------------------------------------- #

def ensure_lib_table_entry(table_path: Path, tag: str, name: str, uri: str, lib_type: str = "KiCad") -> bool:
    """Make sure `name` is registered in the given lib table file, pointing
    at `uri`. Returns True if the file was created/modified."""
    entry_line = f'  (lib (name "{name}")(type "{lib_type}")(uri "{uri}")(options "")(descr ""))\n'

    if table_path.exists():
        content = table_path.read_text()
        if re.search(rf'\(lib\s*\(name\s*"{re.escape(name)}"', content):
            return False  # already registered, leave it alone
        idx = content.rstrip().rfind(")")
        if idx == -1:
            content = f"({tag}\n{entry_line})\n"
        else:
            content = content[:idx].rstrip("\n ") + "\n" + entry_line + ")\n"
        table_path.write_text(content)
        return True
    else:
        table_path.write_text(f"({tag}\n  (version 7)\n{entry_line})\n")
        return True


# --------------------------------------------------------------------------- #
# Metadata (parts.json)
# --------------------------------------------------------------------------- #

def load_metadata(lib_dir: Path) -> dict:
    meta_path = lib_dir / METADATA_FILENAME
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    return {"parts": {}}


def save_metadata(lib_dir: Path, metadata: dict) -> None:
    (lib_dir / METADATA_FILENAME).write_text(json.dumps(metadata, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# Core commands
# --------------------------------------------------------------------------- #

class LibPaths:
    def __init__(self, project_dir: Path, lib_subdir: str, lib_name: str):
        self.project_dir = project_dir
        self.lib_subdir = lib_subdir
        self.lib_dir = project_dir / lib_subdir
        self.lib_name = lib_name
        self.output_prefix = self.lib_dir / lib_name  # what we pass to --output
        self.sym_file = self.lib_dir / f"{lib_name}.kicad_sym"
        self.pretty_dir = self.lib_dir / f"{lib_name}.pretty"
        self.shapes_dir = self.lib_dir / f"{lib_name}.3dshapes"
        self.sym_table = project_dir / "sym-lib-table"
        self.fp_table = project_dir / "fp-lib-table"


def cmd_init(paths: LibPaths) -> None:
    paths.lib_dir.mkdir(parents=True, exist_ok=True)
    sym_uri = f"${{KIPRJMOD}}/{paths.lib_dir.relative_to(paths.project_dir)}/{paths.lib_name}.kicad_sym"
    fp_uri = f"${{KIPRJMOD}}/{paths.lib_dir.relative_to(paths.project_dir)}/{paths.lib_name}.pretty"
    added_sym = ensure_lib_table_entry(paths.sym_table, "sym_lib_table", paths.lib_name, sym_uri)
    added_fp = ensure_lib_table_entry(paths.fp_table, "fp_lib_table", paths.lib_name, fp_uri)
    if added_sym:
        print(f"Registered symbol library '{paths.lib_name}' in {paths.sym_table}")
    if added_fp:
        print(f"Registered footprint library '{paths.lib_name}' in {paths.fp_table}")
    if not added_sym and not added_fp:
        print(f"Library '{paths.lib_name}' already registered in project lib tables.")


def cmd_add(paths: LibPaths, lcsc_ids: list[str], use_cache: bool, debug: bool, auto_install: bool = True) -> None:
    cmd_init(paths)  # make sure directories + lib tables exist first
    base_cmd = find_easyeda2kicad_cmd(auto_install=auto_install)
    metadata = load_metadata(paths.lib_dir)

    for lcsc_id in lcsc_ids:
        lcsc_id = lcsc_id.strip()
        if not lcsc_id:
            continue

        before_symbols = top_level_symbol_names(paths.sym_file.read_text()) if paths.sym_file.exists() else set()
        before_fp = {p.name for p in paths.pretty_dir.glob("*.kicad_mod")} if paths.pretty_dir.exists() else set()
        before_3d = {p.name for p in paths.shapes_dir.glob("*")} if paths.shapes_dir.exists() else set()

        cmd = base_cmd + [
            "--full",
            f"--lcsc_id={lcsc_id}",
            "--output", str(paths.output_prefix),
            "--overwrite",
            "--project-relative",
        ]
        if use_cache:
            cmd.append("--use-cache")
        if debug:
            cmd.append("--debug")

        print(f"\n==> Fetching {lcsc_id} ...")
        result = subprocess.run(cmd, cwd=str(paths.project_dir))
        if result.returncode != 0:
            print(f"WARNING: easyeda2kicad failed for {lcsc_id} (exit {result.returncode}), skipping.", file=sys.stderr)
            continue

        after_symbols = top_level_symbol_names(paths.sym_file.read_text()) if paths.sym_file.exists() else set()
        after_fp = {p.name for p in paths.pretty_dir.glob("*.kicad_mod")} if paths.pretty_dir.exists() else set()
        after_3d = {p.name for p in paths.shapes_dir.glob("*")} if paths.shapes_dir.exists() else set()

        new_symbols = after_symbols - before_symbols
        new_fp = after_fp - before_fp
        new_3d = sorted(after_3d - before_3d)

        symbol_name = next(iter(new_symbols), None)
        if symbol_name is None:
            # Symbol probably already existed (re-add of same part with --overwrite);
            # fall back to any existing metadata entry, else try matching by id later.
            existing = metadata["parts"].get(lcsc_id)
            symbol_name = existing["symbol"] if existing else None

        footprint_file = next(iter(new_fp), None)
        if footprint_file is None:
            existing = metadata["parts"].get(lcsc_id)
            footprint_file = existing.get("footprint_file") if existing else None

        value = description = None
        if symbol_name and paths.sym_file.exists():
            block = extract_symbol_block(paths.sym_file.read_text(), symbol_name)
            if block:
                value = extract_property(block, "Value")
                description = extract_property(block, "ki_description")

        metadata["parts"][lcsc_id] = {
            "lcsc_id": lcsc_id,
            "symbol": symbol_name,
            "footprint_file": footprint_file,
            "model_files": new_3d if new_3d else metadata["parts"].get(lcsc_id, {}).get("model_files", []),
            "value": value,
            "description": description,
            "added": datetime.date.today().isoformat(),
        }
        print(f"    symbol   : {symbol_name}")
        print(f"    footprint: {footprint_file}")
        print(f"    3d model : {', '.join(new_3d) if new_3d else '(none/unchanged)'}")

    save_metadata(paths.lib_dir, metadata)
    print(f"\nLibrary updated: {paths.lib_dir}")


def cmd_remove(paths: LibPaths, identifiers: list[str]) -> None:
    metadata = load_metadata(paths.lib_dir)
    parts = metadata["parts"]

    for ident in identifiers:
        entry = None
        key = None
        if ident in parts:
            key, entry = ident, parts[ident]
        else:
            for k, v in parts.items():
                if v.get("symbol") == ident:
                    key, entry = k, v
                    break

        if entry is None:
            print(f"WARNING: no part found matching '{ident}', skipping.", file=sys.stderr)
            continue

        symbol_name = entry.get("symbol")
        footprint_file = entry.get("footprint_file")
        model_files = entry.get("model_files") or []

        if symbol_name and paths.sym_file.exists():
            content = paths.sym_file.read_text()
            new_content, removed = remove_symbol_block(content, symbol_name)
            if removed:
                paths.sym_file.write_text(new_content)
                print(f"Removed symbol '{symbol_name}' from {paths.sym_file.name}")
            else:
                print(f"WARNING: symbol '{symbol_name}' not found in {paths.sym_file.name}", file=sys.stderr)

        if footprint_file:
            fp_path = paths.pretty_dir / footprint_file
            if fp_path.exists():
                fp_path.unlink()
                print(f"Removed footprint {fp_path.name}")

        for model_name in model_files:
            model_path = paths.shapes_dir / model_name
            if model_path.exists():
                model_path.unlink()
                print(f"Removed 3D model {model_path.name}")

        del parts[key]
        print(f"Removed part {key} ({symbol_name}) from library.")

    save_metadata(paths.lib_dir, metadata)


def get_rows(paths: LibPaths) -> list[dict]:
    metadata = load_metadata(paths.lib_dir)
    return sorted(metadata["parts"].values(), key=lambda p: (p.get("symbol") or "").lower())


def print_table(rows: list[dict], numbered: bool = False) -> None:
    if not rows:
        print("Library is empty.")
        return

    idx_w = len(str(len(rows))) if numbered else 0
    lcsc_w = max(len("LCSC ID"), max(len(p["lcsc_id"]) for p in rows))
    sym_w = max(len("Symbol"), max(len(p.get("symbol") or "") for p in rows))
    val_w = max(len("Value"), max(len(p.get("value") or "") for p in rows))

    header = f'{"#":<{idx_w}}  ' if numbered else ""
    header += f'{"LCSC ID":<{lcsc_w}}  {"Symbol":<{sym_w}}  {"Value":<{val_w}}  Added'
    print(header)
    print("-" * len(header))
    for i, p in enumerate(rows, start=1):
        line = f'{i:<{idx_w}}  ' if numbered else ""
        line += (
            f'{p["lcsc_id"]:<{lcsc_w}}  '
            f'{(p.get("symbol") or ""):<{sym_w}}  '
            f'{(p.get("value") or ""):<{val_w}}  '
            f'{p.get("added", "")}'
        )
        print(line)


def cmd_list(paths: LibPaths) -> None:
    rows = get_rows(paths)
    print_table(rows)
    if rows:
        print(f"\n{len(rows)} part(s) in {paths.sym_file}")


# --------------------------------------------------------------------------- #
# Interactive text-menu UI
# --------------------------------------------------------------------------- #

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str) -> str:
    return code if USE_COLOR else ""


RESET = _c("\033[0m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
CYAN = _c("\033[36m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
RED = _c("\033[31m")

STATE = {"auto_install": True}


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def prompt(text: str) -> str:
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def confirm(text: str) -> bool:
    return prompt(f"{text} [y/N]: ").lower() in ("y", "yes")


def pause(msg: str = "Press Enter to continue...") -> None:
    try:
        input(f"\n{DIM}{msg}{RESET}")
    except (EOFError, KeyboardInterrupt):
        pass


def banner(paths: LibPaths) -> None:
    print(f"{BOLD}{CYAN}=== KiCad EasyEDA Library Manager ==={RESET}")
    print(f"  Project dir : {paths.project_dir}")
    print(f"  Library     : {paths.lib_dir}  (lib name '{paths.lib_name}')")
    print(f"  Auto-update easyeda2kicad on add: {'on' if STATE['auto_install'] else 'off'}")
    print()


def menu_list(paths: LibPaths) -> None:
    clear_screen()
    banner(paths)
    print(f"{BOLD}Parts in library{RESET}\n")
    cmd_list(paths)
    pause()


def menu_add(paths: LibPaths) -> None:
    clear_screen()
    banner(paths)
    print(f"{BOLD}Add part(s) by LCSC number{RESET}\n")
    raw = prompt("Enter LCSC number(s), separated by spaces or commas (blank to cancel): ")
    if not raw:
        return
    lcsc_ids = [x for x in re.split(r"[\s,]+", raw) if x]
    print()
    cmd_add(paths, lcsc_ids, use_cache=False, debug=False, auto_install=STATE["auto_install"])
    pause()


def menu_remove(paths: LibPaths) -> None:
    clear_screen()
    banner(paths)
    print(f"{BOLD}Remove part(s){RESET}\n")
    rows = get_rows(paths)
    print_table(rows, numbered=True)
    if not rows:
        pause()
        return
    print()
    raw = prompt("Enter number(s) to remove (e.g. '1 3'), or LCSC id(s)/symbol name(s) (blank to cancel): ")
    if not raw:
        return
    tokens = [x for x in re.split(r"[\s,]+", raw) if x]
    identifiers = []
    for t in tokens:
        if t.isdigit() and 1 <= int(t) <= len(rows):
            identifiers.append(rows[int(t) - 1]["lcsc_id"])
        else:
            identifiers.append(t)
    print(f"\nAbout to remove: {', '.join(identifiers)}")
    if not confirm("Are you sure?"):
        print("Cancelled.")
        pause()
        return
    print()
    cmd_remove(paths, identifiers)
    pause()


def menu_init(paths: LibPaths) -> None:
    clear_screen()
    banner(paths)
    print(f"{BOLD}Initialize / register library in this project{RESET}\n")
    cmd_init(paths)
    pause()


def menu_settings(paths: LibPaths) -> LibPaths:
    while True:
        clear_screen()
        banner(paths)
        print(f"{BOLD}Settings{RESET}\n")
        print(f"  1) Project dir     : {paths.project_dir}")
        print(f"  2) Library subdir  : {paths.lib_subdir}")
        print(f"  3) Library name    : {paths.lib_name}")
        print(f"  4) Auto-update easyeda2kicad on add: {'on' if STATE['auto_install'] else 'off'}")
        print("  0) Back to main menu")
        choice = prompt("\n> ")
        if choice == "1":
            new_dir = prompt("New project directory: ")
            if new_dir:
                paths = LibPaths(Path(new_dir).expanduser().resolve(), paths.lib_subdir, paths.lib_name)
        elif choice == "2":
            new_sub = prompt("New library subdirectory name: ")
            if new_sub:
                paths = LibPaths(paths.project_dir, new_sub, paths.lib_name)
        elif choice == "3":
            new_name = prompt("New library base name: ")
            if new_name:
                paths = LibPaths(paths.project_dir, paths.lib_subdir, new_name)
        elif choice == "4":
            STATE["auto_install"] = not STATE["auto_install"]
        elif choice in ("0", ""):
            return paths
        else:
            print(f"{RED}Unknown choice.{RESET}")
            pause()


def interactive_main(paths: LibPaths) -> None:
    while True:
        clear_screen()
        banner(paths)
        print(f"{BOLD}Main menu{RESET}")
        print("  1) List parts")
        print("  2) Add part(s) by LCSC number")
        print("  3) Remove part(s)")
        print("  4) Initialize / re-register library in this project")
        print("  5) Settings")
        print("  0) Quit")
        choice = prompt("\n> ")
        if choice == "1":
            menu_list(paths)
        elif choice == "2":
            menu_add(paths)
        elif choice == "3":
            menu_remove(paths)
        elif choice == "4":
            menu_init(paths)
        elif choice == "5":
            paths = menu_settings(paths)
        elif choice in ("0", "q", "Q"):
            print("Goodbye!")
            return
        else:
            print(f"{RED}Unknown choice.{RESET}")
            pause()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive KiCad library manager for parts fetched via easyeda2kicad.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project-dir", default=".", help="KiCad project directory (default: current directory)")
    parser.add_argument("--lib-subdir", default="easyeda_lib", help="Subfolder inside project dir for the library (default: easyeda_lib)")
    parser.add_argument("--lib-name", default="parts", help="Base filename for the library (default: parts)")
    parser.add_argument(
        "--no-auto-install", action="store_true",
        help="Start with the automatic 'pip install --upgrade easyeda2kicad' check turned off (toggle later in Settings)",
    )
    args = parser.parse_args()

    STATE["auto_install"] = not args.no_auto_install

    paths = LibPaths(
        project_dir=Path(args.project_dir).expanduser().resolve(),
        lib_subdir=args.lib_subdir,
        lib_name=args.lib_name,
    )

    try:
        interactive_main(paths)
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    main()
