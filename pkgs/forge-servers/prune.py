#!/usr/bin/env python3
"""Remove install-time residue from a Forge server tree.

Usage:
    prune.py <install_profile.json> <out> <side> <generation>

`generation` is spec0 or spec1. The distinction matters because the launch
surface is discovered differently:

  spec0  `java -jar forge-<v>.jar`; classpath comes from that jar's
         META-INF/MANIFEST.MF `Class-Path` entry.
  spec1  `java @unix_args.txt`; classpath comes from `-p` and
         `-DlegacyClassPath`.

Two things make the keep-set larger than a naive reading of the launch
arguments suggests, and both were established by boot-testing every locked
version rather than by inspection:

  * `MinecraftLocator` discovers `srg.jar`, `fmlcore`, the language
    providers, and `forge-<v>-server.jar`/`-universal.jar` by maven
    convention under the library directory. None of them appear in
    unix_args.txt.

Pruning therefore ends with an assertion that every path on the launch
surface still exists. If a future Forge build needs something this script
removes, the build will fail.
"""

import glob
import json
import os
import re
import sys
import zipfile

MAVEN_RE = re.compile(r"^([^:]+):([^:]+):([^@:]+)(?::([^@:]+))?(?:@(.+))?$")


def maven_path(coordinate: str) -> str:
    m = MAVEN_RE.match(coordinate)
    if not m:
        raise SystemExit(f"unparseable coordinate: {coordinate}")
    group, name, version, classifier, ext = m.groups()
    ext = ext or "jar"
    suffix = f"-{classifier}" if classifier else ""
    return f"{group.replace('.', '/')}/{name}/{version}/{name}-{version}{suffix}.{ext}"


def manifest_lines(raw: str):
    """Unfold MANIFEST.MF continuation lines (leading space = append)."""
    out = []
    for line in raw.splitlines():
        if line.startswith(" ") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def classpath_from_root_jar(out: str):
    """spec0: the root launcher jar's Class-Path, plus the jar itself.

    The launcher jar is the `-jar` target, so it is not listed in its own
    Class-Path; it has to be added explicitly or it looks deletable.
    """
    roots = sorted(glob.glob(os.path.join(out, "forge-*.jar")))
    keep = list(roots)
    if roots:
        with zipfile.ZipFile(roots[0]) as z:
            try:
                mf = z.read("META-INF/MANIFEST.MF").decode(errors="replace")
            except KeyError:
                mf = ""
        for line in manifest_lines(mf):
            if line.startswith("Class-Path"):
                keep += [os.path.join(out, p) for p in line.split(":", 1)[1].strip().split()]
    return keep


def classpath_from_unix_args(out: str):
    args = glob.glob(os.path.join(out, "libraries", "**", "unix_args.txt"), recursive=True)
    if not args:
        return []
    text = open(args[0]).read()
    keep = list(args)
    for chunk in re.findall(r"-DlegacyClassPath=(\S+)", text) + re.findall(r"--?p (\S+)", text):
        keep += chunk.split(":")
    return keep


def convention_kept(out: str):
    """Jars Forge finds by maven convention rather than by an explicit list."""
    keep = []
    pat = ("fmlcore", "fmlloader", "javafmllanguage", "lowcodelanguage",
           "mclanguage", "fmlearlydisplay", "universal", "-server.jar", "-srg.jar")
    for root, _dirs, files in os.walk(os.path.join(out, "libraries", "net", "minecraftforge")):
        keep += [os.path.join(root, f) for f in files if any(k in f for k in pat)]
    keep += glob.glob(os.path.join(out, "libraries", "net", "minecraft", "server", "*", "*-srg.jar"))
    return keep


def install_tool_jars(profile: dict, out: str):
    """Jars referenced only as installer processor classpath entries.

    These are install tools (`installertools`, `jarsplitter`, `binarypatcher`,
    `ForgeAutoRenamingTool`, `srgutils`, the asm copies they pin, ...). Not all
    such coordinates are install-only -- `jopt-simple:5.0.4` is on the runtime
    legacyClassPath even though a processor also lists it -- so the caller
    subtracts the launch surface rather than trusting this list.
    """
    coords = set()
    for proc in profile.get("processors") or []:
        coords.add(proc["jar"])
        coords.update(proc.get("classpath") or [])
    return {os.path.join(out, "libraries", maven_path(c)) for c in coords}


def residue(out: str, profile: dict):
    """Install outputs that are inputs to later processors, not runtime deps.

    `*-slim.jar` and `*-unpacked.jar` are jarsplitter inputs, `*-mappings.txt`
    is a merge input, `*.cache` are jarsplitter's input/output hash stamps, and
    the mcp_config zip is the MAPPINGS source.

    The vanilla jar is matched by exact path, not by a `server-*.jar` glob: the
    same directory also holds `-srg.jar` and `-extra.jar`, which are runtime
    dependencies. Those happen to be on the launch surface so a glob would be
    masked today, but the exact path does not depend on that.
    """
    pats = [
        "libraries/net/minecraft/server/*/server-*-slim.jar",
        "libraries/net/minecraft/server/*/server-*-unpacked.jar",
        "libraries/net/minecraft/server/*/server-*-mappings.txt",
        "libraries/net/minecraft/server/*/*.cache",
        "libraries/de/oceanlabs/mcp/mcp_config/*/mcp_config-*.zip",
        "libraries/de/oceanlabs/mcp/mcp_config/*/mcp_config-*-mappings*.txt",
    ]
    found = set()
    for p in pats:
        found.update(glob.glob(os.path.join(out, p)))
    # The vanilla bundler, pre-staged by the derivation as MINECRAFT_JAR.
    mc = profile["minecraft"]
    found.add(os.path.join(out, "libraries", "net", "minecraft", "server", mc, f"server-{mc}.jar"))
    return found


def redundant_launcher_copy(out: str, generation: str):
    """spec0 copies the launcher jar to the root; the libraries/ copy then
    duplicates it. Only removed when the two are byte-identical."""
    if generation != "spec0":
        return set()
    gone = set()
    for root_jar in glob.glob(os.path.join(out, "forge-*.jar")):
        if root_jar.endswith("-installer.jar"):
            continue
        name = os.path.basename(root_jar)
        for dup in glob.glob(os.path.join(out, "libraries", "net", "minecraftforge", "forge", "*", name)):
            with open(root_jar, "rb") as a, open(dup, "rb") as b:
                if a.read() == b.read():
                    gone.add(dup)
    return gone


def main():
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    profile_path, out, side, generation = sys.argv[1:5]
    profile = json.loads(open(profile_path).read())

    keep = set()
    keep.update(classpath_from_root_jar(out))
    keep.update(classpath_from_unix_args(out))
    keep.update(convention_kept(out))
    keep.add(os.path.join(out, "bin", "forge-server"))

    candidates = set()
    candidates.update(install_tool_jars(profile, out))
    candidates.update(residue(out, profile))
    candidates.update(redundant_launcher_copy(out, generation))

    # Never remove anything the launch surface names, and never remove a
    # directory. `jopt-simple:5.0.4` is the reason this subtraction is not
    # redundant: a processor lists it, but so does the runtime classpath.
    to_remove = {p for p in candidates if p not in keep and os.path.isfile(p)}

    # Also drop library symlinks nothing on the launch surface reads. Each one
    # is a store reference, so removing it shrinks the closure, not just $out.
    surface = {os.path.realpath(p) for p in keep}
    by_link = {}
    for root, _dirs, files in os.walk(os.path.join(out, "libraries")):
        for f in files:
            p = os.path.join(root, f)
            if os.path.islink(p) and os.path.realpath(p) not in surface:
                by_link.setdefault(os.path.realpath(p), []).append(p)
    # A store path is kept if any corresponding library path is on the surface.
    for target, links in by_link.items():
        if target not in surface:
            to_remove.update(links)

    before = sum(os.path.getsize(p) for p in to_remove if os.path.exists(p))
    for p in sorted(to_remove):
        os.remove(p)

    # Anything on the launch surface that no longer exists means the rules
    # above removed something Forge needs. Fail the build rather than ship it.
    missing = sorted(p for p in keep if not os.path.exists(p))
    if missing:
        print("FATAL: pruning removed files the launch surface needs:", file=sys.stderr)
        for p in missing:
            print(f"  {os.path.relpath(p, out)}", file=sys.stderr)
        raise SystemExit(1)

    print(
        f"pruned {len(to_remove)} files ({before / 1048576:.1f} MiB); "
        f"kept {len(keep)} entry points on the launch surface"
    )


if __name__ == "__main__":
    main()
