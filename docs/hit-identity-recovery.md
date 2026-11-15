# Hit-test identity correction under validation

The 1.7.2rc1 live authentication-browser trial stopped at a normal dry-run. No input was sent. The latest action record (2026-11-15T12:27:07.538Z) had `not_actionable` but no `hit_check` field. Its reported wording identifies the foreign-recipient branch: a known owner differed from the scoped app, and the result did not qualify as the exact target, a permitted passive descendant, or a keyboard host.

That record does not identify the foreign process, establish ancestry, or show which candidate point was tested. The reported hit frame was disjoint from the target. Geometry alone cannot authorize another app's control, nor prove that the hit-test snapshot was wrong. The original live cause remains unproven.

## Source correction

The runner's direct snapshot path could combine the original AX hit's process identity with attributes from a snapshot whose AX identity was never checked. Route version 6 requires both the accessibility element ID and process ID to match. A mismatched or unusable direct snapshot uses the existing exact-identity application-tree fallback. If neither read proves identity, the runner returns the fixed error `tapwright hit: identity_unproven`. Successful replies carry `identity: verified`.

The companion Python correction refuses every failed attempted version-6 proof, including timeouts while the runner remains alive, missing elements, generic errors, malformed replies, or missing proof markers. Capability discovery is also checked: malformed or transient failures cannot be cached as evidence of an old runner and silently disable hit checks. Explicit legacy endpoint absence and valid older protocol versions retain their existing behavior.

The foreign-recipient branch now records fixed diagnostics: `gate`, `owner`, `point` (center/offset), and `hit_contains_point`, plus existing relation/probe codes. No foreign labels, identifiers, bundles, raw trees, coordinates or screenshots are added to diagnostics.

## Deployment boundary

This is a paired runner/client correction. The client must be installed before a version-6 runner is activated: older clients can treat hit-route failures as unavailable optional checks. Do not install the runner alone. The release pin and exact signed-artifact verification must be completed after Apple CI succeeds.

No live system is changed by this source work. The phone lead retains exclusive ownership, and the installed 1.7.2rc1 environment remains running. Any future signing, runner installation, daemon switch or normal Cancel trial needs a separately coordinated quiet window. Existing scope, policy, wrapper and confirmation protections remain in force. The correction does not select saved credentials, operate the Passwords sheet, or grant other-app control.

Only after the paired update is validated and coordinated may the lead repeat the screenshot-free normal Cancel dry-run procedure. A successful build or healthy daemon does not establish live Cancel success; a refused dry-run still sends no input and must not be followed by force, coordinates or blind retries.
