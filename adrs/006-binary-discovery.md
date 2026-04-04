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

1. **Only accepts IntelliJ IDEA 2025.3 plugin binary.** The expected path
   pattern is fully specified per platform:
   - macOS: `~/Library/Application Support/JetBrains/IntelliJIdea2025.3/plugins/github-copilot-intellij/copilot-agent/native/darwin-arm64/copilot-language-server`
   - Windows: `%APPDATA%/JetBrains/IntelliJIdea2025.3/plugins/github-copilot-intellij/copilot-agent/native/win-x64/copilot-language-server.exe` (provisional — needs verification)
2. **Auto-discovers via `ps`.** Scans running processes for
   `copilot-language-server`. Each candidate's full path is validated against
   the expected IntelliJ 2025.3 pattern. Incompatible binaries (other IDEs,
   other versions) are rejected with a warning log.
3. **Falls back to filesystem glob.** If no matching process is found, searches
   the JetBrains plugin directory for the expected path.
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
- **Windows path is provisional.** The Windows pattern is declared but not
  empirically verified. First use on Windows will require path validation.
- **IDE must be running for `ps` discovery.** If JetBrains is not running,
  `ps` finds nothing and discovery falls back to filesystem glob. The user
  must have JetBrains installed (even if not running) for glob to work.
