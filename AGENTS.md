# AGENTS.md

Forge server packages where **installation happens entirely at build time**: no
network during the build, no first-run install step. Everything is under
`pkgs/forge-servers/`; the flake exposes the `forge-servers` overlay and
`nix run .#update-forge`.

```
default.nix        one attribute per lock, as forge-1_20_1 and forge-1_20_1-47_4_20
derivation.nix     the build
update.py          lock generator
run-processors.py  runs install_profile.json's processors itself
prune.py           strips install residue, at the end of the build
locks/forge-<mc>-<forge>.json
```

`bin/forge-server` is the integration point (a `makeWrapper` script), so
`getExe server.package` works unchanged. `passthru` carries `installer`,
`libraries`, `runtimeJava`, `gameVersion`, `loaderVersion`, `generation`.

## Versions and Java

| Minecraft | Forge | Format | Runtime Java |
|---|---|---|---|
| 1.12.2 | 14.23.5.2860 | spec 0 | 8 |
| 1.16.5 | 36.2.34 | spec 0 | 8 |
| 1.18.2 | 40.2.4 | spec 1 | 17+ |
| 1.20.1 | 47.4.20 | spec 1 | 17+ |

**Pick the JRE per generation.** Up to 1.16.5, launchwrapper/modlauncher cast
`AppClassLoader` to `URLClassLoader`, which throws on Java 9+; 1.17.1+ uses
BootstrapLauncher and works on 17/21. `runtimeJava` encodes this.

1.20.2+ is the sibling `neoforge-servers` scope in upstream nix-minecraft, which
is why this stops at 1.20.1. Versions ≤ 1.12 are unsupported because their
libraries carry no download info and are split across two Maven repositories, so
URLs would have to be probed (`resolve_legacy_url` in `update.py` is the missing
piece).

## How the install works

`update.py` locks every artifact — `installerSha256`, `libraries` keyed by maven
coordinate, the pre-staged `mojmaps`. `derivation.nix` `fetchurl`s them into
Maven layout with `linkFarm`, then installs.

**The installer jar is never modified.** Two pre-staged inputs make that work:

- the vanilla server jar, where `MINECRAFT_JAR` resolves, so no version-manifest
  lookup happens;
- the Mojang mappings at `MOJMAPS`, so `DOWNLOAD_MOJMAPS` is skipped. Because
  they come from a content-addressed piston-meta `fetchurl`, mapping drift
  becomes a hash mismatch rather than a silent difference.

### Processors are run directly

`run-processors.py` executes the JVMs that `install_profile.json` describes,
rather than `java -jar installer.jar --installServer`. The flag that would stop
`DOWNLOAD_MOJMAPS`, `--skipIfExists`, **does not exist before installertools
1.4.1**, and Forge 1.18.x ships 1.3.0, which rejects it with
`UnrecognizedOptionException`. Running them directly needs no jar surgery and
keeps each step inspectable.

When reading that file: maven coordinates appear **directly in processor args**
(`[de.oceanlabs.mcp:mcp_config:...@zip]`), not only in the `data` table; and
skipped processors are asserted to have their output already present, so a bad
lock fails the build instead of reaching for the network. `EXTRA_ARGS` adds
flags keyed by main class, so it cannot apply to the wrong tool.

### Launch surface

- **`unix_args.txt` order matters.** Replace the no-trailing-slash form first or
  the blanket rewrite misses it, and the server dies with `Missing
  libraryDirectory system property, cannot continue`. `derivation.nix` has the
  exact `substituteInPlace`.
- **spec 0 needs the root launcher jar copied out**, because running processors
  directly bypasses `ServerInstall`. Its manifest `Class-Path` resolves relative
  to the jar, which is what lets the server start from any data directory.

## Gotchas

Each was found by boot-testing every locked version; reading the launch
arguments is not sufficient.

- **`MinecraftLocator` finds jars by maven convention**: `srg.jar`, `fmlcore`,
  the language providers, `forge-<v>-server.jar`. **None appear in
  `unix_args.txt`.** Deleting `srg.jar` gives `NoClassDefFoundError:
  net/minecraft/core/RegistryAccess`; deleting `forge-<v>-server.jar` fails
  later, inside `ItemStack`.
- **`MANIFEST.MF` wraps long values** — a leading space continues the previous
  line. Ignoring that yields truncated, nonexistent `Class-Path` entries.
- **Generation detection is not `"spec" in profile`**: 1.12.2 reports `spec: 0`
  yet has zero processors, and ≤ 1.12 omits `spec` entirely. Use `versionInfo`.
- **`builtins.match` is POSIX ERE** — no `(?:...)`.
- **`maven_path` must keep classifier and extension**: `...:mappings@txt` is not
  `...mappings.jar`.

## Determinism

Only `SpecialSource` needs help; the other tools pin timestamps themselves:

| Tool | Jar entry timestamp |
|---|---|
| `jarsplitter`, `binarypatcher` | `setTime(628041600000L)`, hardcoded |
| `ForgeAutoRenamingTool` | inherits each input's time; its only `setTime` passes `946684800L`, which clamps to the 1980 epoch |
| `SpecialSource` | **none** — `JarEntry` gets the wall clock |

`SpecialSource` produces the srg jar for the 1.16.5 generation only. Its
`--stable` flag, gated by the `SpecialSource.stable` static field (initialised
`false`), makes `JarRemapper` call `entry.setTime(0)`; Forge does not pass it, so
`EXTRA_ARGS` adds it.

That touches only metadata nothing reads. Fix the cause this way rather than
rewriting timestamps afterwards with `strip-nondeterminism`, which hides where
the difference came from. `--rebuild` is clean under any `TZ`/locale; the
build-time `jre_headless` comes from the flake's nixpkgs pin, so only the
runtime JVM varies by generation.

## Pruning install residue

`prune.py` removes what the install needed but the server does not: jarsplitter
inputs (`*-slim.jar`, `*-unpacked.jar`, `*.cache`), both mapping tables, the
installer tools and the asm versions they pin, the vanilla bundler, and every
library symlink the launch surface does not read.

Removing a dead *symlink* frees its whole store path from the closure, which is
where the real saving is.

It finishes by re-deriving the launch surface and **asserting every entry still
exists**, so a successful build is evidence nothing required was removed, and a
Forge version that needs something pruned fails the build instead of producing a
server that dies at startup. Keep that property.

## Regenerating locks

```
nix run .#update-forge                    # refresh every lock
nix run .#update-forge -- 1.20.1 47.4.20  # or one
```

Needs network. New versions in 1.13–1.20.x need no code changes, but prefer a
bounded scope: Forge has ~5000 builds across ~76 Minecraft versions, so
recommended/latest per game version is a few hundred locks, not thousands.

## Remaining work

- ≤ 1.12 unsupported by choice; `update.py` rejects rather than guessing URLs.
- Upstream `neoforge-servers` modifies the installer jar — the approach that
  proved fragile here — so it may rely on `sandbox = false` and fetch silently.
- No test suite. Validation is manual: `nix build`, `--rebuild`, boot to
  `Done (`. A NixOS VM test with a 1.12.2 leg (Java-8 and zero-processor paths)
  would be the natural addition.
