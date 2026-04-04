# ADR-005: Fail-Loud Testing — No Skips

**Status:** Accepted  
**Date:** 2026-04-03  

## Context

The initial test suite used `pytest.skipif` and `pytest.skip()` to handle
missing external dependencies (the copilot-language-server binary). Integration
tests were silently skipped when the binary wasn't found, and the test suite
reported green.

### Empirical evidence

**Silent skips masked a real failure on the target machine.** Integration tests
hung on the target environment. Root cause: `find_binary()` grabbed the first
`copilot-language-server` from `ps` output, which was an incompatible version
from a different JetBrains IDE. The binary existed but was wrong.

The tests used `skipif` to pass when no binary was found. But the actual
failure wasn't "missing binary" — it was "wrong binary selected from multiple
running instances." The skip condition never triggered because *a* binary was
found. The real bug (incompatible binary selection) was invisible.

**The test suite showed 100% pass rate with half its tests not running.** On
machines without the binary, integration tests were skipped. The suite looked
green. This provided false confidence — the integration layer was completely
untested, and the first real deployment failed.

## Decision

Tests must never use `skipif` or `pytest.skip()`. When a test depends on an
external resource (binary, service, credential):

1. The test fails with a clear message explaining what is missing.
2. A fixture asserts the resource exists and is usable, then returns it.
3. There is no "success path" that avoids the dependency.

A test that cannot run is asserting that the environment is misconfigured.
That assertion should surface as a **failure**, not be silently suppressed.

## Rationale

- **Skipping masks environment drift.** If the binary disappears or changes
  path, skipped tests don't notice. The first signal is a production failure.
- **Green with skips is false confidence.** A test suite that passes with half
  its tests skipped proves nothing about the skipped functionality. The suite
  is lying about coverage.
- **The painful short-term path is the correct long-term path.** Loud failures
  when the environment is wrong force environments to get fixed and stay fixed.
  Skips allow broken environments to persist indefinitely because breakage is
  invisible.
- **Specific to this project's reality.** The proxy operates at the boundary
  of an externally controlled binary. The binary version, path, and behavior
  can change without notice (IDE updates, plugin updates). Integration tests
  are the only early warning system. Silencing them defeats their purpose.

## Consequences

- **Integration tests fail on machines without the binary.** This is
  intentional. Running integration tests requires the copilot-language-server.
  Developers who want to run only unit tests can target specific test files:
  `pytest tests/test_transport.py tests/test_server.py tests/test_discovery.py`
- **CI must provision the binary.** Any CI pipeline must either have the binary
  available or explicitly scope test runs to unit tests only. There is no
  silent degradation path.
- **Binary compatibility is tested, not assumed.** The `test_integration.py`
  fixture validates that the discovered binary matches the expected IntelliJ
  2025.3 plugin path (see ADR-006). Wrong binaries cause test failure, not
  silent misbehavior.
