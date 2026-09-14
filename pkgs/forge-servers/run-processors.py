#!/usr/bin/env python3
"""Run Forge installer processors directly, bypassing SimpleInstaller entirely.

Delegating to the installer binary means the only way to stop the
DOWNLOAD_MOJMAPS processor from hitting launchermeta.mojang.com is to modify
install_profile.json inside the jar, which is fragile: the obvious
`--skipIfExists` flag exists in installertools >= 1.4.1 but not 1.3.0 (Forge
1.18.x rejects it with UnrecognizedOptionException).

install_profile.json already describes each processor as a plain JVM
invocation with data-driven arguments, so it can be executed directly. That
removes the need to patch the jar at all, and makes each step individually
inspectable.

Usage:
    run-processors.py <install_profile.json> <libdir> <installer.jar> <root> <side>

The JVM is taken from $JAVA if set, otherwise `java` from $PATH.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

JAVA = os.environ.get("JAVA") or shutil.which("java")
if not JAVA:
    raise SystemExit("no java found: set $JAVA or put `java` on $PATH")

MAVEN_RE = re.compile(r"^([^:]+):([^:]+):([^@:]+)(?::([^@:]+))?(?:@(.+))?$")

# Processors whose whole purpose is fetching a file we already have from a
# content-addressed source. Their output is pre-staged, so they are skipped
# rather than run. Matching on the task name keeps this independent of
# installertools' CLI surface.
SKIP_TASKS = {"DOWNLOAD_MOJMAPS"}


def maven_path(coordinate: str, artifact: dict | None = None) -> str:
    if artifact and artifact.get("path"):
        return artifact["path"]
    m = MAVEN_RE.match(coordinate)
    if not m:
        raise SystemExit(f"unparseable coordinate: {coordinate}")
    group, name, version, classifier, ext = m.groups()
    ext = ext or "jar"
    suffix = f"-{classifier}" if classifier else ""
    return f"{group.replace('.', '/')}/{name}/{version}/{name}-{version}{suffix}.{ext}"


def main_class(jar: Path) -> str:
    with zipfile.ZipFile(jar) as z:
        for line in z.read("META-INF/MANIFEST.MF").decode(errors="replace").splitlines():
            if line.startswith("Main-Class:"):
                return line.split(":", 1)[1].strip()
    raise SystemExit(f"no Main-Class in {jar}")


def main():
    if len(sys.argv) != 6:
        raise SystemExit(__doc__)
    profile_path, libdir_s, installer_s, root_s, side = sys.argv[1:6]
    profile = json.loads(Path(profile_path).read_text())
    # Resolve to absolute paths: processors run from a scratch cwd, so relative
    # classpath entries would otherwise fail to resolve.
    libdir = Path(libdir_s).resolve()
    installer = Path(installer_s).resolve()
    root = Path(root_s).resolve()

    mc = profile["minecraft"]

    # MINECRAFT_JAR is where the installer would have put the vanilla jar.
    server_jar_path = profile.get("serverJarPath") or "{ROOT}/minecraft_server.{MINECRAFT_VERSION}.jar"
    mcjar = Path(
        server_jar_path
        .replace("{ROOT}", str(root))
        .replace("{LIBRARY_DIR}", str(libdir))
        .replace("{MINECRAFT_VERSION}", mc)
    )

    special = {
        "ROOT": str(root),
        "LIBRARY_DIR": str(libdir),
        "INSTALLER": str(installer),
        "MINECRAFT_JAR": str(mcjar),
        "MINECRAFT_VERSION": mc,
        "SIDE": side,
    }

    work = Path(tempfile.mkdtemp())
    extracted: dict[str, str] = {}
    library_by_name = {lib["name"]: lib for lib in (profile.get("libraries") or [])}

    def artifact_path(name: str) -> Path:
        lib = library_by_name.get(name)
        artifact = (lib or {}).get("downloads", {}).get("artifact") or {}
        return libdir / maven_path(name, artifact)

    def data_value(key: str):
        entry = (profile.get("data") or {}).get(key)
        if not entry:
            return None
        raw = entry.get(side) or entry.get("server")
        if raw is None:
            return None
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
            return raw[1:-1]
        if raw.startswith("["):
            return str(libdir / maven_path(raw[1:-1]))
        if raw.startswith("/"):
            # A file carried inside the installer jar.
            if raw not in extracted:
                target = work / Path(raw).name
                with zipfile.ZipFile(installer) as z:
                    target.write_bytes(z.read(raw.lstrip("/")))
                extracted[raw] = str(target)
            return extracted[raw]
        return raw

    def subst(arg: str) -> str:
        """Expand one processor argument.

        Two forms occur:

        - `{NAME}` placeholders, resolved from the `data` table.
        - `[group:artifact:version(:classifier)(@ext)]` maven coordinates,
          which appear directly in processor args and must become filesystem
          paths. Note these are not in `data` at all, e.g. the MCP_DATA
          processor passes `[...:mcp_config:...@zip]` as `--input`.
        """
        if arg.startswith("[") and arg.endswith("]") and ":" in arg:
            return str(libdir / maven_path(arg[1:-1]))

        def rep(m):
            name = m.group(1)
            if name in special:
                return special[name]
            value = data_value(name)
            if value is None:
                raise SystemExit(f"unknown placeholder {{{name}}} in arg: {arg}")
            return value

        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", rep, arg)

    processors = profile.get("processors") or []
    ran = skipped = 0

    for i, proc in enumerate(processors):
        sides = proc.get("sides")
        if sides and side not in sides:
            continue

        args = [subst(a) for a in (proc.get("args") or [])]
        task = args[args.index("--task") + 1] if "--task" in args else None

        if task in SKIP_TASKS:
            out = args[args.index("--output") + 1]
            if not Path(out).exists():
                raise SystemExit(
                    f"processor {i} ({task}) skipped but its output is not pre-staged: {out}"
                )
            print(f"[{i}] {task}: satisfied from pre-staged input")
            skipped += 1
            continue

        classpath = [str(artifact_path(proc["jar"]))]
        classpath += [str(artifact_path(n)) for n in (proc.get("classpath") or [])]

        for path in classpath:
            if not Path(path).exists():
                raise SystemExit(f"processor {i}: missing classpath entry {path}")

        name = main_class(Path(classpath[0]))
        print(f"[{i}] {task or name}: {name}")
        result = subprocess.run(
            [JAVA, "-cp", ":".join(classpath), name, *args],
            cwd=str(work),
        )
        if result.returncode != 0:
            raise SystemExit(f"processor {i} ({task or name}) failed: exit {result.returncode}")
        ran += 1

    print(f"processors run: {ran}, satisfied from pre-staged inputs: {skipped}")


if __name__ == "__main__":
    main()
