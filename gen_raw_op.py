#!/usr/bin/env python3
# gen_raw_op.py
#
# Interactive flow:
#   1. Pick a netlist (.spice) file
#   2. Pick a process corner (tt/ff/ss/fs/sf)
#   3. Pick a temperature
#   4. Pick which MOSFET parameters to save (default: id gm vgs vds vth vdsat)
# Then:
#   - Strip out the original .control block entirely (removes heavy tran etc.)
#   - Auto-generate save lines for every MOSFET, and current save lines for
#     every R/L/C element
#   - Build a new, lightweight .control block that runs op only
#   - Run ngspice -b directly on the resulting netlist to produce the .raw
#
# The original netlist file is never modified.
#
# Usage:
#   python3 gen_raw_op.py
#
# Note on region (SAT/TRIODE/CUTOFF):
#   Showing text like "SAT" directly on the schematic requires placing an
#   extra probe with a formula (ngspice_get_expr.sym) on every device, which
#   is outside the scope of this script. If you just want a list in the
#   terminal, use check_mos_regions_sky130.py, which is built the same way
#   and computes the region from vgs/vth/vds/vdsat.

import os
import re
import subprocess
import sys
import tempfile

NETLIST_EXTS = (".spice", ".sp", ".cir", ".net", ".ckt")
CORNERS = ["tt", "ff", "ss", "fs", "sf"]


def ask(prompt, default=None):
    try:
        s = input(prompt).strip()
    except EOFError:
        return default if default is not None else ""
    return s if s else (default if default is not None else "")


def choose_netlist():
    files = sorted(
        f for f in os.listdir(".")
        if f.lower().endswith(NETLIST_EXTS) and os.path.isfile(f)
    )
    if not files:
        print(f"No netlist files ({', '.join(NETLIST_EXTS)}) found in this folder.")
        typed = ask("Enter a path directly (blank to quit): ")
        if not typed:
            sys.exit(0)
        if not os.path.isfile(typed):
            print(f"Not found: {typed}")
            sys.exit(1)
        return typed

    print("Netlist files in this folder:")
    for i, f in enumerate(files, 1):
        print(f"  [{i}] {f}")
    while True:
        sel = ask(f"Pick a number [1-{len(files)}] (default 1): ", default="1")
        if sel.isdigit() and 1 <= int(sel) <= len(files):
            return files[int(sel) - 1]
        if os.path.isfile(sel):
            return sel
        print("Invalid input, try again.")


def detect_corner(text):
    m = re.search(r"^\s*\.lib\s+'[^']+'\s+(\S+)\s*$", text, re.IGNORECASE | re.MULTILINE)
    return m.group(1) if m else None


def choose_corner(detected):
    print(f"\nProcess corner (detected in netlist: {detected or 'unknown'}):")
    for i, c in enumerate(CORNERS, 1):
        print(f"  [{i}] {c}")
    default_idx = str(CORNERS.index(detected) + 1) if detected in CORNERS else "1"
    sel = ask(f"Pick a number [1-{len(CORNERS)}] (default {default_idx}): ", default=default_idx)
    if sel.isdigit() and 1 <= int(sel) <= len(CORNERS):
        return CORNERS[int(sel) - 1]
    if sel in CORNERS:
        return sel
    print("Invalid input, using the default.")
    return CORNERS[int(default_idx) - 1]


def choose_temp():
    raw = ask("Temperature in Celsius (default: 27): ", default="27")
    try:
        return float(raw)
    except ValueError:
        print("Could not parse as a number, using 27.")
        return 27.0


def choose_params():
    default_params = ["id", "gm", "vgs", "vds", "vth", "vdsat"]
    raw = ask(
        f"MOSFET parameters to save, space-separated (default: {' '.join(default_params)}): ",
        default=" ".join(default_params),
    )
    return raw.split()


def set_corner(text, corner):
    def repl(m):
        return m.group(1) + corner
    new_text, n = re.subn(r"(^\s*\.lib\s+'[^']+'\s+)(\S+)\s*$", repl,
                          text, flags=re.IGNORECASE | re.MULTILINE)
    if n == 0:
        print("Warning: could not find a '.lib ... <corner>' line; corner unchanged.")
    return new_text


def strip_temp_lines(text):
    """Remove any existing .temp lines (the new value is inserted together
    with the control block by insert_before_final_end)."""
    return re.sub(r"(?im)^\s*\.temp\s+\S+\s*$", "", text)


def strip_control_block(text):
    """Remove the original .control ... .endc block entirely (drops heavy
    analyses like tran)."""
    new_text, n = re.subn(
        r"(?is)^\s*\.control\b.*?^\s*\.endc\b\s*$", "", text, flags=re.M
    )
    if n == 0:
        print("Warning: no .control block found.")
    return new_text


def find_transistors(text):
    devices = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("XM") or stripped.startswith("xm"):
            tokens = stripped.split()
            if len(tokens) < 6:
                continue
            name = tokens[0]
            model = tokens[5]
            devices.append((name, model))
    return devices


def find_passive_currents(text):
    """Collect R/L/C element names (their branch current will be saved)."""
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "RLCrlc":
            continue
        tokens = stripped.split()
        if len(tokens) < 4:
            continue
        names.append(tokens[0])
    return names


def build_new_control(transistors, passives, params, out_raw):
    lines = [".control", "save all"]
    for name, model in transistors:
        for p in params:
            lines.append(f"save @m.{name.lower()}.m{model}[{p}]")
    for name in passives:
        lines.append(f"save @{name}[i]")
    lines.append("op")
    lines.append(f"write {out_raw}")
    lines.append(".endc")
    return "\n".join(lines) + "\n"


def insert_before_final_end(text, block):
    """Insert block right before the last standalone '.end' line.
    Appends at the end if no '.end' line is found."""
    matches = list(re.finditer(r"(?im)^\s*\.end\s*$", text))
    if not matches:
        return text.rstrip() + "\n\n" + block + "\n"
    last = matches[-1]
    return text[:last.start()] + block + "\n" + text[last.start():]


def run_ngspice(spice_path):
    print(f"\nRunning: ngspice -b {spice_path}")
    try:
        result = subprocess.run(
            ["ngspice", "-b", spice_path],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        print("ERROR: ngspice not found on PATH.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("ERROR: ngspice did not finish within 120 seconds (timeout).")
        print("This should only be an op analysis, so check the generated netlist "
              "if you see this.")
        sys.exit(1)

    tail = result.stdout.splitlines()[-30:]
    print("----- ngspice stdout (tail) -----")
    print("\n".join(tail))
    if result.stderr.strip():
        print("----- ngspice stderr -----")
        print(result.stderr)
    print(f"----- exit code: {result.returncode} -----")
    return result.returncode == 0


def main():
    print("=== gen_raw_op.py (op only, with corner/temperature selection) ===")

    netlist_path = choose_netlist()
    print(f"Selected: {netlist_path}")

    with open(netlist_path, "r") as f:
        original_text = f.read()

    detected = detect_corner(original_text)
    corner = choose_corner(detected)
    temp_c = choose_temp()
    params = choose_params()

    transistors = find_transistors(original_text)
    passives = find_passive_currents(original_text)
    print(f"\nFound: {len(transistors)} transistor(s), {len(passives)} R/L/C element(s)")

    body = strip_control_block(original_text)
    body = set_corner(body, corner)
    body = strip_temp_lines(body)

    base, ext = os.path.splitext(netlist_path)
    out_raw = f"{base}_op_{corner}_{temp_c:g}C.raw"
    control_block = build_new_control(transistors, passives, params, out_raw)
    # .temp must come before the .control block and before the final .end
    extra_block = f".temp {temp_c}\n\n" + control_block

    new_text = insert_before_final_end(body, extra_block)

    # Write the intermediate netlist to a temp file and delete it after
    # ngspice runs, so the only thing left in the folder is the .raw file.
    tmp_dir = os.path.dirname(os.path.abspath(netlist_path))
    fd, tmp_netlist_path = tempfile.mkstemp(
        suffix=ext, prefix="xschem_op_tmp_", dir=tmp_dir
    )
    os.close(fd)
    with open(tmp_netlist_path, "w") as f:
        f.write(new_text)

    try:
        ok = run_ngspice(tmp_netlist_path)
    finally:
        os.remove(tmp_netlist_path)
        print(f"(removed intermediate file {os.path.basename(tmp_netlist_path)})")

    if ok:
        print(f"\nDone. raw file: {out_raw}")
        print("Back in Xschem, run [Simulation -> Annotate Operating Point into schematic].")
        print("If it can't auto-detect this filename, point it there directly from the console:")
        print(f"  xschem annotate_op {os.path.abspath(out_raw)}")
        print()
        print("Note: any node with no voltage shown just has no net label attached to it")
        print("(the value itself is already saved, since 'save all' covers every node).")
    else:
        print("\nngspice exited with an error. Check the log above.")


if __name__ == "__main__":
    main()
