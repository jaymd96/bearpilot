#!/bin/sh
# check-drift.sh — self-enforcing documentation drift gate for a principled project.
#
# Three checks, ship-with-the-scaffold, nobody-has-to-remember-to-run:
#
#   1. LINK / ANCHOR CHECKER (blocking).
#      Every relative markdown link in the doc tree must resolve to a real file, and every
#      in-document #anchor must match a heading or an explicit anchor. Catches the single most
#      common drift: a doc citing a renamed / moved / deleted file. Failures are BLOCKING
#      (exit 1) so it gates a hook or CI.
#      Intentionally ignores: links inside fenced code blocks, links inside HTML comments, and
#      link targets containing an unresolved <...> placeholder — a freshly scaffolded, not-yet-
#      filled repo must pass its own gate. Resolve placeholders before the first real commit.
#
#   2. REFERENCES PROVENANCE LINT (blocking on references/).
#      Every references/*.md crib (except the 00-index) must carry the mandatory provenance
#      STRUCTURE: a "Canonical source:" header, a "Version / pin:" header, and a footer
#      "Update on:" refresh-trigger. And references/00-index.md must list every crib (no orphan
#      crib, no index row pointing at a missing file). A malformed crib is BLOCKING — a crib
#      without provenance is a crib that can't be re-verified.
#
#   3. GIT-FRESHNESS / STALENESS SURFACER (advisory — warns, never blocks).
#      A LIVING doc declares the code it describes via a directive:
#          <!-- watches: src/foo src/bar/baz.py -->
#      `watches:` takes git PATHSPECS (a directory or explicit files — NOT shell * globs).
#      When the watched code's last commit is NEWER than the doc's last commit, the doc is
#      SUSPECT and a reconcile warning is emitted. Timestamps are git-derived (can't lie); no
#      hand-maintained "last-reconciled" stamp to rot. SNAPSHOT docs (under decision-notes/,
#      plans/, provenance/, archive/, or spec-deferrals.md, or tagged <!-- snapshot -->) are
#      EXEMPT — they are allowed to age.
#
# Exit status:
#   0  clean (advisory warnings may have printed)
#   1  one or more BLOCKING failures (dead link / dead anchor / malformed crib / orphan index)
#   2  usage / environment error
#
# Dependency-light: POSIX sh + git + grep + sed. No node, no python.
#
# Usage:
#   sh check-drift.sh [<repo-root>] [--no-freshness] [--no-references] [--strict] [--quiet]
#
#     <repo-root>      directory to scan (defaults to git toplevel, else $PWD)
#     --no-freshness   skip check 3 (only run the blocking checks)
#     --no-references  skip check 2 (e.g. a repo with no references/ library yet)
#     --strict         treat freshness staleness warnings as BLOCKING too (exit 1)
#     --quiet          suppress the per-file "ok" lines; show only problems + summary

set -eu

# ---------------------------------------------------------------------------
# Args.
# ---------------------------------------------------------------------------
ROOT=""
DO_FRESHNESS=1
DO_REFERENCES=1
STRICT=0
QUIET=0

while [ $# -gt 0 ]; do
  case $1 in
    --no-freshness)  DO_FRESHNESS=0 ;;
    --no-references) DO_REFERENCES=0 ;;
    --strict)        STRICT=1 ;;
    --quiet)         QUIET=1 ;;
    -h|--help)
      sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    --*)
      echo "check-drift: unknown option: $1" >&2
      exit 2 ;;
    *)
      if [ -z "$ROOT" ]; then ROOT=$1; else
        echo "check-drift: unexpected extra argument: $1" >&2; exit 2
      fi ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Resolve repo root.
# ---------------------------------------------------------------------------
if [ -z "$ROOT" ]; then
  if ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then :; else ROOT=$(pwd); fi
fi
if [ ! -d "$ROOT" ]; then
  echo "check-drift: not a directory: $ROOT" >&2
  exit 2
fi
ROOT=$(CDPATH= cd -- "$ROOT" && pwd)

HAVE_GIT=0
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  HAVE_GIT=1
fi

fail_count=0
warn_count=0

say()  { [ "$QUIET" -eq 1 ] || printf '%s\n' "$1"; }
prob() { printf '%s\n' "$1" >&2; }

# ---------------------------------------------------------------------------
# Temp scratch. Sidecar files carry failure tallies across subshell boundaries.
# ---------------------------------------------------------------------------
TMPD=$(mktemp -d 2>/dev/null || echo "/tmp/cd_$$")
[ -d "$TMPD" ] || mkdir -p "$TMPD"
MD_LIST="$TMPD/md_list"
FAILS="$TMPD/fails"          # one 'x' per blocking failure (whole run)
: > "$FAILS"
trap 'rm -rf "$TMPD" 2>/dev/null || true' EXIT INT TERM

# ---------------------------------------------------------------------------
# Collect the markdown files to scan. Skip VCS / vendor / build dirs.
# ---------------------------------------------------------------------------
find "$ROOT" \
  \( -name .git -o -name node_modules -o -name .venv -o -name venv \
     -o -name dist -o -name build -o -name target -o -name .tox \) -prune \
  -o -type f -name '*.md' -print 2>/dev/null | sort > "$MD_LIST"

if [ ! -s "$MD_LIST" ]; then
  say "check-drift: no markdown files found under $ROOT — nothing to check."
  exit 0
fi

# strip_fences <file> — emit the file's content with fenced code blocks removed (but HTML
# comments kept). Used for the freshness check, whose directive IS an HTML comment but which
# must not fire on an EXAMPLE `<!-- watches: ... -->` shown inside a ``` documentation fence.
strip_fences() {
  awk '
    BEGIN { in_fence = 0 }
    {
      if ($0 ~ /^[[:space:]]*(```|~~~)/) { in_fence = !in_fence; next }
      if (in_fence) next
      print
    }' "$1"
}

# strip_noise <file> — emit the file's content with fenced code blocks and HTML comments
# removed, so the link checker never flags an illustrative link inside ``` or <!-- -->.
# (Conservative: a ``` or ~~~ at the start of a line toggles a fence; <!-- ... --> spans lines.)
strip_noise() {
  sed -e 's/<!--/\n<!--/g' -e 's/-->/-->\n/g' "$1" | awk '
    BEGIN { in_fence = 0; in_comment = 0 }
    {
      line = $0
      # toggle fenced code block on a line that starts with ``` or ~~~
      if (line ~ /^[[:space:]]*(```|~~~)/) { in_fence = !in_fence; next }
      if (in_fence) next
      # drop HTML comment spans (already split onto their own lines above)
      if (line ~ /<!--/) { in_comment = 1 }
      if (in_comment) { if (line ~ /-->/) { in_comment = 0 } ; next }
      print line
    }'
}

# ===========================================================================
# CHECK 1 — LINK / ANCHOR CHECKER (blocking)
# ===========================================================================
say "== Link & anchor check =="

# slugify <heading-text> -> github-style anchor.
slugify() {
  printf '%s\n' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -e 's/[^a-z0-9 _-]//g' -e 's/[ _]/-/g' -e 's/-\{2,\}/-/g' \
          -e 's/^-//' -e 's/-$//'
}

# anchors_of <file> -> newline-separated set of valid anchors (heading slugs +
# explicit <a name="..."> / <a id="..."> / {#custom-id} attributes).
anchors_of() {
  af="$1"
  grep -E '^#{1,6}[[:space:]]+' "$af" 2>/dev/null | while IFS= read -r line; do
    text=$(printf '%s' "$line" | sed -E 's/^#{1,6}[[:space:]]+//; s/[[:space:]]*#+[[:space:]]*$//')
    custom=$(printf '%s' "$text" | sed -nE 's/.*\{#([A-Za-z0-9_-]+)\}.*/\1/p')
    if [ -n "$custom" ]; then
      printf '%s\n' "$custom"
      text=$(printf '%s' "$text" | sed -E 's/[[:space:]]*\{#[A-Za-z0-9_-]+\}[[:space:]]*$//')
    fi
    slugify "$text"
  done
  grep -oE '<a[[:space:]][^>]*(name|id)="[^"]+"' "$af" 2>/dev/null \
    | sed -nE 's/.*(name|id)="([^"]+)".*/\2/p'
}

# links_of <file> -> markdown link targets, one per line, with code fences + comments stripped.
links_of() {
  strip_noise "$1" \
    | grep -oE '\]\([^)[:space:]]+\)' 2>/dev/null \
    | sed -E 's/^\]\(//; s/\)$//'
}

while IFS= read -r md; do
  rel=${md#"$ROOT"/}
  dir=$(dirname "$md")
  file_fail_marker="$TMPD/file_fail"
  rm -f "$file_fail_marker"

  links_of "$md" | while IFS= read -r target; do
    [ -n "$target" ] || continue

    # Strip a trailing title:  path "Title"
    target=$(printf '%s' "$target" | sed -E 's/[[:space:]].*$//')

    case $target in
      http://*|https://*|mailto:*|tel:*|ftp://*|//*)
        continue ;;                         # external — out of scope
    esac

    # Skip unresolved placeholders: a target containing < or > is a template slot, not a real
    # path. A freshly scaffolded repo carries these until step 4 fills them in.
    case $target in
      *'<'*|*'>'*) continue ;;
    esac

    # Split off any #anchor.
    case $target in
      *\#*) path=${target%%#*}; anchor=${target#*#} ;;
      *)    path=$target;       anchor="" ;;
    esac

    # Resolve the path component (may be empty -> same file).
    if [ -z "$path" ]; then
      resolved=$md
    else
      case $path in
        /*) resolved="$ROOT$path" ;;        # leading slash = repo-root-relative
        *)  resolved="$dir/$path" ;;
      esac
    fi

    if [ ! -e "$resolved" ]; then
      prob "FAIL  $rel  ->  $target   (target not found)"
      echo x >> "$FAILS"
      echo x >> "$file_fail_marker"
      continue
    fi

    # Anchor check (only when target is a markdown file and an anchor was given).
    if [ -n "$anchor" ] && [ -f "$resolved" ]; then
      case $resolved in
        *.md|*.markdown)
          if anchors_of "$resolved" | grep -Fxq "$anchor"; then
            :
          else
            prob "FAIL  $rel  ->  $target   (anchor #$anchor not found in target)"
            echo x >> "$FAILS"
            echo x >> "$file_fail_marker"
          fi ;;
      esac
    fi
  done

  # Per-file "ok" only when THIS file contributed no failures (marker survives the subshell).
  [ -f "$file_fail_marker" ] || say "  ok   $rel"
  rm -f "$file_fail_marker"
done < "$MD_LIST"

# ===========================================================================
# CHECK 2 — REFERENCES PROVENANCE LINT (blocking on references/)
# ===========================================================================
if [ "$DO_REFERENCES" -eq 1 ] && [ -d "$ROOT/references" ]; then
  say ""
  say "== References provenance lint (blocking) =="

  index="$ROOT/references/00-index.md"
  # Every crib (a references/*.md that is not the index) must carry provenance STRUCTURE.
  for crib in "$ROOT"/references/*.md; do
    [ -e "$crib" ] || continue
    crel=${crib#"$ROOT"/}
    case $crib in
      */00-index.md) continue ;;
    esac

    missing=""
    grep -qiE 'canonical source[[:space:]]*:' "$crib" 2>/dev/null || missing="$missing canonical-source-header"
    grep -qiE 'version[[:space:]]*/?[[:space:]]*pin[[:space:]]*:|version[[:space:]]*:' "$crib" 2>/dev/null || missing="$missing version-pin"
    grep -qiE 'update on[[:space:]]*:' "$crib" 2>/dev/null || missing="$missing refresh-trigger"

    if [ -n "$missing" ]; then
      prob "FAIL  $crel   (malformed crib — missing:$missing)"
      echo x >> "$FAILS"
    else
      say "  ok   $crel"
    fi

    # The index must list this crib by filename.
    if [ -f "$index" ]; then
      base=$(basename "$crib")
      if grep -Fq "$base" "$index" 2>/dev/null; then
        :
      else
        prob "FAIL  references/00-index.md   (orphan crib not listed: $base)"
        echo x >> "$FAILS"
      fi
    fi
  done

  # Every index row that links a crib must point at a file that exists.
  if [ -f "$index" ]; then
    links_of "$index" | while IFS= read -r t; do
      case $t in
        http://*|https://*|mailto:*|*'<'*|*'>'*) continue ;;
      esac
      p=${t%%#*}
      [ -n "$p" ] || continue
      case $p in /*) r="$ROOT$p" ;; *) r="$ROOT/references/$p" ;; esac
      if [ ! -e "$r" ]; then
        prob "FAIL  references/00-index.md  ->  $t   (index row points at a missing crib)"
        echo x >> "$FAILS"
      fi
    done
  fi
fi

# Tally blocking failures recorded by all checks above.
if [ -s "$FAILS" ]; then
  fail_count=$(wc -l < "$FAILS" | tr -d ' ')
fi

# ===========================================================================
# CHECK 3 — GIT-FRESHNESS / STALENESS SURFACER (advisory)
# ===========================================================================
if [ "$DO_FRESHNESS" -eq 1 ]; then
  say ""
  say "== Freshness check (git-derived; advisory) =="

  if [ "$HAVE_GIT" -eq 0 ]; then
    say "  (not a git repo — freshness check skipped)"
  else
    last_commit_ts() {
      git -C "$ROOT" log -1 --format=%ct -- "$1" 2>/dev/null
    }

    while IFS= read -r md; do
      rel=${md#"$ROOT"/}

      # Skip SNAPSHOT genres — they are allowed to age.
      case $rel in
        */decision-notes/*|*/plans/*|*/provenance/*|*/archive/*|*spec-deferrals.md)
          continue ;;
      esac
      if grep -qiE '<!--[[:space:]]*snapshot[[:space:]]*-->' "$md" 2>/dev/null; then
        continue
      fi

      # Read the watches directive from the fence-stripped content so an EXAMPLE directive shown
      # inside a documentation code block (e.g. checks/README.md) is not mistaken for a real one.
      watch_line=$(strip_fences "$md" | grep -oiE '<!--[[:space:]]*watches:[^>]*-->' 2>/dev/null | head -n 1 || true)
      [ -n "$watch_line" ] || continue

      globs=$(printf '%s' "$watch_line" \
        | sed -E 's/<!--[[:space:]]*[Ww][Aa][Tt][Cc][Hh][Ee][Ss]:[[:space:]]*//; s/[[:space:]]*-->.*//')
      [ -n "$globs" ] || continue

      doc_ts=$(last_commit_ts "$rel")
      [ -n "$doc_ts" ] || continue          # untracked doc IS the working copy — treat as fresh

      newest_code_ts=0
      newest_code_path=""
      any_tracked=0
      for g in $globs; do
        # Skip placeholder pathspecs left in a not-yet-filled doc.
        case $g in *'<'*|*'>'*) continue ;; esac
        ct=$(last_commit_ts "$g")
        if [ -n "$ct" ]; then
          any_tracked=1
          if [ "$ct" -gt "$newest_code_ts" ]; then
            newest_code_ts=$ct
            newest_code_path=$g
          fi
        fi
      done

      # A watched pathspec that matches nothing tracked is worth surfacing (likely a typo or a
      # moved path) — but it is advisory, not blocking.
      if [ "$any_tracked" -eq 0 ]; then
        say "  note  $rel   (watched paths match nothing tracked — check the 'watches:' pathspecs)"
        continue
      fi

      if [ "$newest_code_ts" -gt "$doc_ts" ]; then
        warn_count=$((warn_count + 1))
        prob "STALE $rel   (watched code '$newest_code_path' changed after the doc — reconcile)"
        if [ "$STRICT" -eq 1 ]; then
          echo x >> "$FAILS"
        fi
      else
        say "  fresh $rel"
      fi
    done < "$MD_LIST"
  fi
fi

# Re-tally (strict mode may have added freshness failures).
if [ -s "$FAILS" ]; then
  fail_count=$(wc -l < "$FAILS" | tr -d ' ')
fi

# ===========================================================================
# Summary + exit.
# ===========================================================================
say ""
say "== Summary =="
say "  blocking failures: $fail_count"
say "  staleness warnings: $warn_count"

if [ "$fail_count" -gt 0 ]; then
  prob "check-drift: FAILED — $fail_count blocking issue(s). Fix dead links / anchors / malformed cribs before relying on the docs."
  exit 1
fi

if [ "$warn_count" -gt 0 ]; then
  say "check-drift: passed with $warn_count advisory staleness warning(s)."
else
  say "check-drift: clean."
fi
exit 0
