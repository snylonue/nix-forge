#!/usr/bin/env python3
"""Generate a build lock for a Forge server version.

The lock records everything the installer needs so that `derivation.nix` can
produce a server without any network access at build time:

  - every library jar plus the vanilla server jar, with Maven-layout paths
  - the Mojang server mappings (spec 1 installers), pre-placed where the
    DOWNLOAD_MOJMAPS processor would otherwise write them
  - Forge's own published SHA-1s of the intermediate and final jars, recorded
    for reference

Forge's installer format changed repeatedly over the years. Two shapes are
handled, distinguished by the profile contents rather than by version number:

  v1-spec0   `spec: 0`, processors, no MOJMAPS       (1.12.2 - 1.16.5)
  v1-spec1   `spec: 1`, processors, MOJMAPS present  (1.17+)

The pre-1.13 format (no `spec`, with `install`/`versionInfo` blocks) is
rejected rather than half-supported; see resolve_legacy_url for what adding it
would require.

Usage:
    ./update.py 1.20.1 47.4.20      # one lock, written to locks/
    ./update.py                     # refresh every lock in locks/

Network access is required. Proxy environment variables (http_proxy /
https_proxy) are honoured when set.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

FORGE_MAVEN = "https://maven.minecraftforge.net"
MOJANG_MAVEN = "https://libraries.minecraft.net"
VERSION_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"


def log(*args):
    print(*args, file=sys.stderr, flush=True)


_opener = None


def fetch_opener():
    """A urllib opener honouring proxy environment variables when present.

    Proxies are read explicitly rather than relying on urllib's implicit
    detection, because `nix shell` and Nix build sandboxes strip the variables
    from the environment.
    """
    global _opener
    if _opener is None:
        handlers = []
        for scheme in ("http", "https"):
            proxy = os.environ.get(f"{scheme}_proxy") or os.environ.get(f"{scheme.upper()}_PROXY")
            if proxy:
                log(f"using {scheme} proxy {proxy}")
                handlers.append(urllib.request.ProxyHandler({scheme: proxy}))
        _opener = urllib.request.build_opener(*handlers)
    return _opener


def fetch(url: str) -> bytes:
    """Fetch a URL, honouring proxy environment variables when present."""
    req = urllib.request.Request(url, headers={"User-Agent": "nix-forge-lockgen"})
    with fetch_opener().open(req, timeout=120) as resp:
        return resp.read()


def fetch_json(url: str):
    return json.loads(fetch(url))


def nix_hash(url: str) -> str:
    """SRI sha256 of a URL, computed via nix-prefetch-url."""
    env = dict(os.environ)
    out = subprocess.run(
        ["nix-prefetch-url", url],
        check=True,
        capture_output=True,
        encoding="UTF-8",
        env=env,
    ).stdout.splitlines()
    return subprocess.run(
        ["nix", "hash", "convert", "--hash-algo", "sha256", "--to", "sri", out[-1]],
        check=True,
        capture_output=True,
        encoding="UTF-8",
        env=env,
    ).stdout.strip()


def maven_path(coordinate: str, artifact: dict | None = None) -> str:
    """Maven coordinate -> repository-relative path.

    Both the classifier and the extension are part of the on-disk name, e.g.
    `net.minecraft:server:1.20.1-20230612.114412:mappings@txt` must become
    `...-mappings.txt`, not `....jar`. `@` terminates the version component so
    that `version:mappings@txt` parses correctly.
    """
    if artifact and artifact.get("path"):
        return artifact["path"]
    m = re.match(r"^([^:]+):([^:]+):([^@:]+)(?::([^@:]+))?(?:@(.+))?$", coordinate)
    if not m:
        raise ValueError(f"unparseable coordinate: {coordinate}")
    group, name, version, classifier, ext = m.groups()
    ext = ext or "jar"
    suffix = f"-{classifier}" if classifier else ""
    return f"{group.replace('.', '/')}/{name}/{version}/{name}-{version}{suffix}.{ext}"


def piston_version(mc: str) -> dict:
    manifest = fetch_json(VERSION_MANIFEST)
    entry = next((v for v in manifest["versions"] if v["id"] == mc), None)
    if entry is None:
        raise SystemExit(f"Minecraft version {mc} not found in manifest")
    return fetch_json(entry["url"])


def resolve_legacy_url(coordinate: str) -> str | None:
    """Find a URL for a library whose profile omits download info.

    Legacy profiles (<= 1.12) carry no URLs at all, and the libraries are split
    across Forge's Maven and Mojang's. Probe both rather than assuming, since a
    guessed URL that 404s would only surface as a fetch failure much later.
    """
    path = maven_path(coordinate)
    for base in (FORGE_MAVEN, MOJANG_MAVEN):
        url = f"{base}/{path}"
        try:
            req = urllib.request.Request(url, method="HEAD")
            fetch_opener().open(req, timeout=30).close()
            return url
        except Exception:
            continue
    return None


def collect_libraries(sources: list) -> dict:
    """Gather every library referenced by the installer.

    Libraries appear in both install_profile.json and version.json and the two
    lists differ, so all sources must be passed in.

    Some entries have an empty URL but a real `path` and `sha1`, because the jar
    ships inside the installer under `maven/` rather than on a repository.
    Those are recorded as `bundle:` entries for extraction.
    """
    out = {}
    for source in sources:
        for lib in source or []:
            name = lib["name"]
            artifact = (lib.get("downloads") or {}).get("artifact") or {}
            url = artifact.get("url")
            path = artifact.get("path") or maven_path(name)

            if not url:
                if artifact.get("sha1"):
                    # Present, but shipped inside the installer.
                    out[name] = {
                        "url": f"bundle:maven/{path}",
                        "sha1": artifact["sha1"],
                        "path": path,
                        "size": artifact.get("size"),
                    }
                else:
                    log(f"  skipping {name}: no artifact url")
                continue

            out[name] = {
                "url": url,
                "sha1": artifact.get("sha1"),
                "path": path,
                "size": artifact.get("size"),
            }
    return out


def vanilla_entry(mc: str) -> dict:
    vanilla = piston_version(mc)["downloads"]["server"]
    return {
        "url": vanilla["url"],
        "sha1": vanilla["sha1"],
        "path": maven_path(f"net.minecraft:server:{mc}"),
        "size": vanilla.get("size"),
    }


def upstream_sha(data: dict, key: str):
    entry = data.get(key)
    return entry["server"].strip("'") if entry and entry.get("server") else None


def build_lock(mc: str, forge: str) -> dict:
    version = f"{mc}-{forge}"
    installer_url = f"{FORGE_MAVEN}/net/minecraftforge/forge/{version}/forge-{version}-installer.jar"
    installer_hash = nix_hash(installer_url)

    with tempfile.TemporaryDirectory() as tmp:
        jar = Path(tmp) / "installer.jar"
        jar.write_bytes(fetch(installer_url))
        with zipfile.ZipFile(jar) as zf:
            names = set(zf.namelist())
            profile = json.loads(zf.read("install_profile.json"))
            # Very old installers (<= 1.12) carry the launch information in a
            # `versionInfo` block inside the profile instead of a separate
            # version.json, which may be absent entirely.
            version_json = json.loads(zf.read("version.json")) if "version.json" in names else {}

    # Discriminate on profile contents, not on the version number: the formats
    # do not line up cleanly with version boundaries.
    data = profile.get("data") or {}
    has_mojmaps = "MOJMAPS" in data

    if "versionInfo" in profile:
        raise SystemExit(
            f"{mc}-{forge} uses the pre-1.13 installer format, which is not "
            "supported: its libraries carry no download info and are split "
            "across two Mavens, so URLs cannot be resolved reliably."
        )

    lock = {
        "mc": mc,
        "forge": forge,
        "spec": profile.get("spec"),
        "installerSha256": installer_hash,
        "mojmaps": None,
        "patchedSha": None,
        "slimSha": None,
        "extraSha": None,
    }

    libraries = collect_libraries([profile.get("libraries"), version_json.get("libraries")])
    lock |= {
        "generation": "installer",
        "mainClass": version_json.get("mainClass"),
        "patchedSha": upstream_sha(data, "PATCHED_SHA"),
        "slimSha": upstream_sha(data, "MC_SLIM_SHA"),
        "extraSha": upstream_sha(data, "MC_EXTRA_SHA"),
    }

    if has_mojmaps:
        piston = piston_version(mc)
        if "server_mappings" in piston["downloads"]:
            lock["mojmaps"] = {
                "url": piston["downloads"]["server_mappings"]["url"],
                "sha1": piston["downloads"]["server_mappings"]["sha1"],
                # Full coordinate: classifier and extension are both part of
                # the file name the processor writes.
                "path": maven_path(data["MOJMAPS"]["server"].strip("[]")),
            }

    libraries[f"net.minecraft:server:{mc}"] = vanilla_entry(mc)
    lock["libraries"] = libraries
    return lock


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "version",
        nargs="?",
        help="Minecraft version, e.g. 1.20.1 (omit to refresh every existing lock)",
    )
    ap.add_argument("forge", nargs="?", help="Forge build, e.g. 47.4.20")
    ap.add_argument("-o", "--output", help="output path (default: locks/forge-<mc>-<forge>.json)")
    args = ap.parse_args()

    lockdir = Path(__file__).parent / "locks"

    if args.version is None:
        # Refresh in place. Each lock name encodes its versions, so existing
        # locks are the source of truth for what to regenerate.
        existing = sorted(lockdir.glob("forge-*.json"))
        if not existing:
            raise SystemExit("no locks found and no version given")
        for path in existing:
            m = re.match(r"forge-(.+?)-(\d.*)\.json$", path.name)
            if not m:
                log(f"skipping unparseable lock name: {path.name}")
                continue
            mc, forge = m.groups()
            log(f"==> {path.name}")
            lock = build_lock(mc, forge)
            path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
            log(f"    {lock['generation']}, {len(lock['libraries'])} libraries")
        return

    if args.forge is None:
        ap.error("forge build is required when a Minecraft version is given")

    log(f"resolving Forge {args.version}-{args.forge}")
    lock = build_lock(args.version, args.forge)
    log(f"generation: {lock['generation']}")

    out = Path(args.output) if args.output else lockdir / f"forge-{args.version}-{args.forge}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    log(f"wrote {out} ({len(lock['libraries'])} libraries)")


if __name__ == "__main__":
    main()
