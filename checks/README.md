# checks/ — the documentation drift harness

<!-- LIVING. The check framework is a first-class artefact, not an afterthought.
     It ships WITH the scaffold so the docs stay true without anyone remembering to run anything. -->

This directory holds the self-enforcing anti-drift harness for bear-harness. The principle:
**only mechanisms nobody has to *remember* to run survive.** These checks are git-derived or
structural — they can't lie, and they run on a hook and in CI.

## What ships today

| Check | Kind | Blocking? | Where |
|---|---|---|---|
| **Link & anchor** — every relative markdown link / `#anchor` resolves | structural | **blocking** | hook + CI |
| **References provenance** — every `references/*.md` crib has a canonical-source header + version pin + footer refresh-trigger, and `references/00-index.md` lists every crib | structural | **blocking** on `references/` | hook + CI |
| **Git-freshness** — a LIVING doc whose watched code changed *after* the doc is *suspect* | git-derived | advisory (warns) | hook + CI |

All three live in **`check-drift.sh`** (one script, flags select the subset). Run it directly:

```sh
sh checks/check-drift.sh .            # all three checks
sh checks/check-drift.sh . --no-freshness   # blocking checks only (fast)
```

Exit `0` = clean. Exit `1` = a blocking failure (dead link/anchor, or a malformed/orphan crib).
Freshness staleness only warns; it never blocks. SNAPSHOT docs (decision-notes, plans, provenance,
deferrals, individual cribs) are exempt from freshness — they are allowed to age.

## How a LIVING doc opts into the freshness check

Add a one-line directive naming the code globs the doc describes:

```markdown
<!-- watches: src/executor src/scheduler/launch.py -->
```

`watches:` takes **git pathspecs** (a directory, or explicit files — not shell `*` globs). When the
newest commit touching any watched path is newer than the doc's last commit, the doc is flagged for
reconciliation. No hand-maintained "last-reconciled" stamp (those rot); the timestamps come from git.

## Baseline-and-ratchet (brownfield repos)

Do **not** try to fix every violation at once. On an existing repo with many dead links:
record the current violations as a known baseline (a checked-in allowlist the check reads and skips),
let the check **prevent new ones**, and burn the baseline down over time. The check rewards
improvement; it does not demand a big-bang cleanup. <!-- Implement the baseline file when you first
need it — a fresh scaffold has zero violations, so it starts empty. -->

## Wiring (run without remembering)

Raw git hooks — no framework needed:

```sh
git config core.hooksPath .githooks    # one-time, per clone
```

`.githooks/pre-commit` calls `checks/check-drift.sh` on commit. CI runs the same script. The escape
hatch is `git commit --no-verify` — use it sparingly; CI is the authoritative gate.

## How to add a check

The harness is meant to grow. When an invariant bites you a second time, encode it:

1. Add the logic to `check-drift.sh` behind a new section + a `--no-<name>` opt-out flag.
2. Decide its blast radius: **blocking** (structural breakage everyone must fix) or **advisory**
   (a warning + a reconcile task). Default to advisory until it has earned blocking.
3. Document it in the table above and reference the decision-note that motivated it.

<!-- when to expand me: split check-drift.sh into per-check scripts ONLY if one script grows past
     comprehension or a check needs a different runtime (e.g. a Python AST symbol-checker). Until
     then one POSIX-sh script is the simplest thing that works — keep it. Checks that need network
     access to a live external surface (re-diffing a crib against the current `--help`) belong in an
     on-demand / scheduled job, never in the blocking hook. -->
