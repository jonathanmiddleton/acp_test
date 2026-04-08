# ADR-006: Binary Discovery — IntelliJ IDEA 2025.3 Only

**Status:** Accepted  
**Date:** 2026-04-03  

## Context

The copilot-language-server binary is bundled with JetBrains IDE plugins, not
installed independently. Multiple JetBrains IDEs can run simultaneously on the
same machine (IntelliJ, PyCharm, WebStorm, etc.), each with its own copy of
the binary at a different version. The target environment confirmed this —
multiple `copilot-language-server` processes were running from different IDE
installations.

### Empirical evidence

**Wrong binary selection caused hangs on target.** The initial `find_binary()`
grabbed the first matching process from `ps` output. On the target machine,
this was an incompatible version from a different IDE (not IntelliJ 2025.3).
Integration tests hung because the binary's ACP behavior differed from what
the proxy expected.

**Case sensitivity mismatch.** The discovery pattern initially used
`IntellijIdea` (lowercase j) but the actual macOS filesystem directory is
`IntelliJIdea` (capital J). macOS HFS+ is case-insensitive, so glob-based
file search worked, but the regex for `ps`-based validation was
case-sensitive and rejected the correct binary.

**Binary path is user-dependent.** The path includes the OS username (SOEID on
the target environment). Hardcoding any user-specific path component would
break on every other machine.

## Decision

`discovery.py` is the single source of truth for binary resolution. It:

1. **Validates three properties for compatibility.** A binary path must:
   - Be under the current user's home directory.
   - Contain `IntelliJIdea2025.3` as a path component.
   - Have the correct binary filename (`copilot-language-server` on Unix,
     `copilot-language-server.exe` on Windows).

   No assumptions are made about the intermediate directory structure
   between the home directory and the IDE directory. Deployment layouts
   vary across environments — the Windows target uses
   `.../copilot-agent/bin/copilot-language-server` (no `native/` or
   architecture directory), while macOS uses
   `.../copilot-agent/native/darwin-arm64/copilot-language-server`.

2. **Auto-discovers from running processes.** On Unix, scans `ps` output.
   On Windows, uses PowerShell (`Get-Process`) with a wmic fallback. Each
   candidate's full path is validated against the three-property check.
   Incompatible binaries (other IDEs, other versions, other users) are
   rejected with a warning log.
3. **Falls back to filesystem lookup.** If no matching process is found,
   checks the expected JetBrains plugin directory on disk.
4. **Explicit `--binary` override.** The CLI accepts an explicit path that
   bypasses discovery, for environments where auto-discovery fails.

Both `__main__.py` and `test_integration.py` import from `discovery.py`. No
duplicated discovery logic.

## Rationale

- **Version specificity prevents silent incompatibility.** ACP behavior can
  differ between binary versions. The proxy was developed and tested against
  the IntelliJ 2025.3 plugin binary (agent version 1.457.1 on dev, 1.442.0
  on target). Accepting arbitrary versions risks silent behavioral differences.
- **Single source of truth.** Before this change, `__main__.py` and
  `test_integration.py` had separate discovery logic that diverged. Extracting
  to a shared module eliminated the inconsistency.
- **`ps`-based discovery handles the common case.** If JetBrains is running,
  the binary is in `ps` output. This is faster and more reliable than
  filesystem search (which requires knowing the exact user home path).
- **Rejecting incompatible binaries is the correct failure mode.** Finding *a*
  binary is not sufficient — it must be the *right* binary. The target
  environment proved this when the wrong binary was selected and the proxy
  hung.

## Consequences

- **Only one IDE version supported.** If the user upgrades to IntelliJ 2026.1,
  discovery will fail until the pattern is updated. This is intentional — the
  proxy should be explicitly validated against new versions before accepting
  them.
- **Filesystem fallback assumes a specific directory structure.** The
  `find_binary_from_jetbrains()` path uses the full expected layout
  (including `native/{arch}/`). If the actual layout differs (as on the
  Windows target), the filesystem fallback won't find the binary. Process
  discovery will still work since it uses the relaxed three-property check.
  The `--binary` flag is the escape hatch.
- **IDE must be running for process discovery.** If JetBrains is not running,
  process scanning finds nothing and discovery falls back to filesystem
  lookup. The user must have JetBrains installed (even if not running) for
  filesystem lookup to work.

## Revision History

| Date | Change |
|------|--------|
| 2026-04-07 | Relaxed path matching from exact full-path regex to three-property check (home dir, IDE dir component, binary name). Added Windows process discovery via PowerShell + wmic fallback. Confirmed Windows target uses a different directory layout (`bin/` instead of `native/{arch}/`). |
