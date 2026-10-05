#!/usr/bin/env python3
"""shed — the coordination protocol over files in git: orient, declare, handoff, woosh, sweep, proof.

A *shed* is the opening in the warp that the pick passes through: the space a
session works in, made fresh each pass. This tool holds the state of a project
between sessions in plain files, so that any worker — a person, a Claude Code
session, another agent — can pick the work up cold.

Six verbs make up the protocol: orient, declare, handoff, woosh, sweep, proof.

  orient    read the last handoff + git state (+ open briefs if any) -> one page
  declare   one sentence of what this session means to do, before it touches
            files, with an optional discharge test. Read back at finalize:
            MET is a progress line; UNMET / UNARMED / ERROR make the statement
            an open thread, verbatim — forward-intent without a model in the seat.
  handoff   draft the session record from the transcript + git; finalize at end.
            Finalize is GROUNDED: every model-written entry's claim text IS a
            verbatim span of the transcript turn it cites (paraphrase goes in
            `gloss`, never gated), carried entries are byte-identical to their
            origin, the human's own first and last sentences sit above the
            synthesis, and one extractiveness number over the ungated prose
            makes drift a trend instead of a feeling.

  woosh     write handoff/brief/<slug>.md: a standing suggestion with a required
            done_when. Suggestion, never directive.
  sweep     the decay vent. A brief with no BODY edit in 30d (or past its own
            `expires`) has already left the page; sweep lists it, and --apply
            flips it declined, AGED OUT, never judged. Holds global/done_check.
  proof     a commit that claims a number shows a proof block (checked / cmd /
            evidence) or a Proof-Block: trailer. Form only. Opt-in commit-msg hook.

  draft     the draft seat (optional): a local model proposes open_threads /
            decisions / watch_outs; only entries the evidence gate passes are
            written. The gate judges, the model is the variable. An optional
            typed judge (SHED_JUDGE: Kev / Mica) may then drop kept entries
            that are status narration — drop only, never add; silence keeps.

Orient and handoff are wired into Claude Code as hooks so no prompt has to ask for them:
SessionStart runs orient and injects the page; Stop refreshes the draft;
SessionEnd runs the draft seat once (when SHED_MODEL is set) and finalizes.
Instructions get skipped; hooks do not.

Standard library only. Python 3.9+. No network, except the draft seat's one
opt-in call to a model endpoint you name (SHED_MODEL). State lives under
<root>/handoff/ (sessions/, drafts/, metrics.jsonl) and the archive format is
schema'd under schemas/, so a repo that later puts heavier tooling over
these files keeps its history.

usage:
  shed.py orient  [--root DIR] [--max-lines N] [--sid8 X]
  shed.py declare "<statement>" [--root DIR] [--sid8 X] [--cmd CMD [--expect-rc N] [--expect-re RE]
                  [--expect-absent] --expires YYYY-MM-DD [--scope repo|host]] [--reason WHY]
  shed.py handoff [--root DIR] [--transcript FILE] [--sid8 X] [--finalize] [--share FIELD]...
  shed.py draft   [--root DIR] [--transcript FILE] [--sid8 X] [--model NAME[@URL]] [--budget CHARS] [--timeout S]
                  [--judge NAME[@URL][,...]] [--judge-tau P] [--judge-budget S]
  shed.py woosh   <slug> "<title>" --done-when TEXT [--done-check CMD --expires YYYY-MM-DD] [--topic T]
                  [--global] [--interpreted-by MODEL] [--body FILE|-] [--root DIR] [--sid8 X]
  shed.py sweep   [--apply] [--run-checks] [--idle-days N] [--root DIR]
  shed.py sweep   --flip SLUG --to open|consumed|superseded|declined [--note WHY]
  shed.py proof   [--range A..B | --message FILE] [--tally] [--root DIR]
  shed.py verify  <shared archive> [--root DIR]
  shed.py check   SCHEMA FILE                          # form only, against schemas/<SCHEMA>.json
  shed.py install [--root DIR] [--proof-hook]
  shed.py hook (session-start|stop|session-end)      # JSON on stdin

The protocol's file formats are JSON Schema under schemas/ (record, intent, thread, decision,
discharge, tier, basis, lease). Every record field declares its emission scope there
(`x-scope: shared | private`); the private layer's allowlist is DERIVED from that annotation.
"""
import argparse
import glob
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone

MAX_LINES_DEFAULT = 40

# The protocol's schemas ship beside this file (schemas/*.json, JSON Schema 2020-12; the contract
# hololoom_mcp/handoff/brief/shed_protocol_schemas.md S3 names). shed validates with the small
# required-keys checker below — stdlib stays the rule — and the platform (weft CI) does the rest.
SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")
_SCHEMAS = {}


def schema(name):
    """Load schemas/<name>.json once. Missing or unparseable = a broken install: raise, never guess."""
    name = name[:-5] if name.endswith(".json") else name
    if name not in _SCHEMAS:
        with open(os.path.join(SCHEMA_DIR, name + ".json"), encoding="utf-8") as f:
            _SCHEMAS[name] = json.load(f)
    return _SCHEMAS[name]


# The private layer (hololoom_mcp/handoff/brief/shed_private_layer.md). Every field of the record
# declares its emission scope IN THE SCHEMA — `x-scope: shared | private` on schemas/record.json —
# and this table is derived from it, never hand-kept. Nothing crosses the git boundary by
# replication, only by a scope the schema declares or a `--share <field>` on finalize. Anything a
# person SAID is private by default: `session_topic` is a whitespace-normalized copy of
# `human_first`, so it is private too — a shared topic that is her sentence is the leak the layer
# exists to close. The shared archive is an allowlist: a key not in the schema never reaches
# handoff/sessions/.
FIELD_SCOPE = {k: v["x-scope"] for k, v in schema("record")["properties"].items()}
SHARED_FIELDS = tuple(k for k, v in FIELD_SCOPE.items() if v == "shared")
PRIVATE_FIELDS = tuple(k for k, v in FIELD_SCOPE.items() if v == "private")
RECORDED_NOTICE = ("Recorded: this session is written to handoff/ "
                   "(private/ stays on this machine; sessions/ is shared in git).")
DECAY_DAYS = 30
MAX_BRIEFS = 8
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


# ---------------------------------------------------------------- helpers

def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s):
    """3.9-safe: fromisoformat rejects a trailing Z. Return None on garbage."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def stamp(dt):
    """Filename-safe timestamp, matching hololoom_mcp's archive naming."""
    return iso(dt).replace(":", "-")


def git(root, *args):
    try:
        out = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


def sid8_from(explicit=None, payload=None):
    for cand in (explicit, (payload or {}).get("session_id"), os.environ.get("CLAUDE_CODE_SESSION_ID")):
        if cand:
            return str(cand)[:8]
    return "nosid"


def paths(root):
    h = os.path.join(root, "handoff")
    return {
        "handoff": h,
        "sessions": os.path.join(h, "sessions"),
        "drafts": os.path.join(h, "drafts"),
        "private": os.path.join(h, "private"),
        "briefs": os.path.join(h, "brief"),
        "metrics": os.path.join(h, "metrics.jsonl"),
    }


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def metric(root, event, **fields):
    p = paths(root)["metrics"]
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        rec = {"ts": iso(utcnow()), "event": event}
        rec.update(fields)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- schema check (the forty lines)

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}


def _resolve_ref(ref):
    """`file.json` or `file.json#/properties/x` — sibling schema files only; no remote refs."""
    path, _, ptr = ref.partition("#")
    node = schema(path)
    for part in [p for p in ptr.split("/") if p]:
        node = node[part]
    return node


def check_shape(obj, sch, at="$"):
    """Required-keys / type / enum / pattern checker over the subset of JSON Schema these files use
    (type, enum, required, properties, items, pattern, minLength, maxLength, additionalProperties,
    $ref, allOf, anyOf). Returns a list of problem strings; [] = the shape holds. FORM only: it says a
    record is the contract's shape, never that its evidence is real — that is the evidence gate's job."""
    out = []
    if "$ref" in sch:
        out += check_shape(obj, _resolve_ref(sch["$ref"]), at)
    for sub in sch.get("allOf", []):
        out += check_shape(obj, sub, at)
    if "anyOf" in sch and all(check_shape(obj, sub, at) for sub in sch["anyOf"]):
        out.append("{}: matches none of {}".format(at, [sorted(s.get("required", [])) for s in sch["anyOf"]]))
    types = sch.get("type")
    if types is not None:
        want = tuple(_TYPES[t] for t in ([types] if isinstance(types, str) else types))
        if not isinstance(obj, want) or (isinstance(obj, bool) and bool not in want):
            return out + ["{}: expected {}, got {}".format(at, types, type(obj).__name__)]
    if "enum" in sch and obj not in sch["enum"]:
        out.append("{}: {!r} not one of {}".format(at, obj, sch["enum"]))
    if isinstance(obj, str):
        if "pattern" in sch and not re.search(sch["pattern"], obj):
            out.append("{}: {!r} does not match {}".format(at, obj[:40], sch["pattern"]))
        if len(obj) < sch.get("minLength", 0) or len(obj) > sch.get("maxLength", len(obj)):
            out.append("{}: length {} outside [{}, {}]".format(at, len(obj), sch.get("minLength", 0), sch.get("maxLength", "")))
    if isinstance(obj, dict):
        for k in sch.get("required", []):
            if k not in obj:
                out.append("{}: missing required {!r}".format(at, k))
        props = sch.get("properties", {})
        for k, v in obj.items():
            if k in props:
                out += check_shape(v, props[k], "{}.{}".format(at, k))
            elif sch.get("additionalProperties") is False:
                out.append("{}: unknown key {!r}".format(at, k))
    if isinstance(obj, list) and "items" in sch:
        for i, v in enumerate(obj):
            out += check_shape(v, sch["items"], "{}[{}]".format(at, i))
    return out


def last_archive(root):
    """Newest finalized archive. Only `<ended>__<sid8>.json` counts: a live scratchpad
    (`SESSION.<sid8>.json`) in the same directory sorts AFTER every dated name and would
    otherwise be read as the last session (seen 2026-09-25, sid8 a45f7b63)."""
    files = sorted(f for f in glob.glob(os.path.join(paths(root)["sessions"], "*.json"))
                   if "__" in os.path.basename(f))
    for f in reversed(files):
        d = load_json(f)
        if isinstance(d, dict):
            d["_path"] = f
            if not d.get("sid8"):
                d["sid8"] = os.path.basename(f)[:-5].rsplit("__", 1)[-1]
            # The private layer: the human's words live beside the archive on this machine only.
            # Read them back here so *she* sees her words; a clone of the repo never has this file.
            priv = load_json(os.path.join(paths(root)["private"], d["sid8"] + ".json"))
            if isinstance(priv, dict) and priv.get("archive") == os.path.basename(f):
                for k in PRIVATE_FIELDS:
                    if priv.get(k) and not d.get(k):
                        d[k] = priv[k]
            return d
    return None


def drafts(root, exclude_sid8=None):
    out = []
    for f in sorted(glob.glob(os.path.join(paths(root)["drafts"], "*.json"))):
        d = load_json(f)
        if isinstance(d, dict) and d.get("sid8") != exclude_sid8:
            d["_path"] = f
            out.append(d)
    return out


def superseding_archive(root, sid8, started):
    """A richer archive (a /handoff skill, a human) that ended at or after this draft started.
    finalize stands down on it, so a draft it covers will never be finalized — orient must not
    advertise that draft as unfinalized either. One predicate, both callers.
    Disk first, then trunk: a shared checkout lags what was published by pushing a sha.
    Returns a label naming the archive (a root-relative path, or <trunk>:<path>), else None."""
    if not started:
        return None
    sessions = paths(root)["sessions"]
    on_disk = ((os.path.relpath(f, root), load_json(f) or {})
               for f in glob.glob(os.path.join(sessions, "*__{}.json".format(sid8))))
    for label, d in itertools.chain(on_disk, trunk_archives(root, sessions, sid8)):
        ended = parse_iso(d.get("ended_at"))
        if ended and ended >= started and d.get("source") != "shed":
            return label
    return None


# ---------------------------------------------------------------- briefs (read-only here)

def parse_frontmatter(text):
    m = FM_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    key = None
    for line in m.group(1).splitlines():
        if line.startswith((" ", "\t")) and key:
            fm[key] = (fm[key] + " " + line.strip()).strip()
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            fm[key] = val.strip().strip("\"'").lstrip(">").strip()
    return fm, text[m.end():]


# The decay clock reads BODY edits only. A frontmatter pass (a topic retrofit, a pushed-out date) must
# not reset it: hololoom_mcp measured 2026-08-23 that bulk housekeeping had touched 148 of 154 open
# briefs inside 29 days, so an any-commit clock vented 6 where the body clock vents 60. A diff counts
# as frontmatter-only when every changed line is frontmatter-SHAPED and every hunk ENDS on or before
# the file's own closing fence; anything ambiguous reads as engagement and keeps the brief open — a
# wrong keep costs a line on a page, a wrong decline buries live work. Shape alone is not enough
# (`Update: half built` and an indented sub-bullet are prose that looks like YAML), and neither is
# brief_sweep's fixed 60-line bound (most briefs are shorter than 60 lines, so it bounded nothing —
# adversarial pass 2026-10-04). The fence is read from the file as it is now; a frontmatter that has
# since grown can only move the bound later, and the shape test still has to pass.
FM_DIFF_LINE = re.compile(r"^[+-](\s{2,}\S|[A-Za-z][A-Za-z0-9_]*\s*:|---\s*$|\s*$)")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BRIEF_STATUSES = ("open", "consumed", "superseded", "declined")


def brief_files(root):
    bdir = paths(root)["briefs"]
    return [f for f in sorted(glob.glob(os.path.join(bdir, "*.md"))) if not os.path.basename(f).startswith("_")]


def fence_line(path):
    """1-based line number of the closing `---` fence, or 0 when the file has no frontmatter."""
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
    except OSError:
        return 0
    m = FM_RE.match(text)
    return m.group(0)[:m.end(1) - m.start(0)].count("\n") + 2 if m else 0


def zsplit(out):
    return [x for x in (out or "").split("\0") if x]


def body_clock(root, days=DECAY_DAYS):
    """(edited, tracked, dirty): `edited` maps each REAL brief path to the UTC date of its newest
    BODY-changing commit (the scan reaches a little past `days`; brief_age judges by the date), and
    `tracked`/`dirty` are the paths under version control and uncommitted right now. Real, not normalized: git names the
    toplevel by its resolved path (macOS /private/var/…) while the glob keeps the caller's
    (/var/…), and a path compared across the two never matches — every edit reads as silence. One `git log -p` for the directory,
    over HEAD and trunk both — a checkout can lag trunk, and engagement pushed from elsewhere must
    count (brief_sweep measured the miss 2026-09-09). Empty sets when git cannot read."""
    bdir = paths(root)["briefs"]
    top = git(root, "rev-parse", "--show-toplevel")
    trunk = trunk_ref(root)
    revs = ["HEAD"] + ([trunk] if trunk else [])
    # quotePath off: a non-ASCII name is otherwise octal-escaped in quotes and never matches its file
    # +2: `--since` is a rolling instant and the verdict is whole UTC days — the margin keeps every
    # commit on the boundary date in the scan; anything older that slips in is judged by its date
    log = git(root, "-c", "core.quotePath=false", "log", *revs, "--since={}.days.ago".format(days + 2),
              "--format=@%H %ct", "-p", "--unified=0", "--", bdir)
    edited, fences = {}, {}
    cur, lines, max_line, day = None, [], 0, None

    def flush():
        if cur and day:
            if cur not in fences:
                fences[cur] = fence_line(cur)
            real = [l for l in lines if l[1:].strip() not in ("", "---")]
            if not (real and all(FM_DIFF_LINE.match(l) for l in real) and max_line <= fences[cur]):
                edited[cur] = max(edited.get(cur, day), day)

    for line in (log or "").splitlines() if top else ():
        if line.startswith("@@"):
            m = HUNK_RE.match(line)
            if m:  # the LAST line each side touches, not where the hunk starts
                o_start, o_len, n_start, n_len = (int(g) if g is not None else 1 for g in m.groups())
                max_line = max(max_line, o_start + max(o_len, 1) - 1, n_start + max(n_len, 1) - 1)
            else:
                max_line = 10 ** 9  # unreadable hunk header: engagement
        elif line.startswith("@") and not line.startswith("@@"):
            flush()
            cur, lines, max_line = None, [], 0
            try:  # committer time, the clock `--since` reads; its DATE taken in UTC, as `created:` is written
                day = datetime.fromtimestamp(int(line.split()[1]), timezone.utc).date()
            except (IndexError, ValueError):
                day = None  # unreadable stamp: the commit cannot vouch for engagement on any date
        elif line.startswith("+++ "):
            flush()
            p = line[4:].strip()
            cur = os.path.realpath(os.path.join(top, p[2:])) if p.startswith("b/") else None
            lines, max_line = [], 0
        elif line.startswith(("--- ", "diff ", "index ", "new file", "deleted file", "similarity", "rename ")):
            continue
        elif cur and line[:1] in "+-":
            lines.append(line)
    flush()
    # -z: NUL-separated and never quoted, so a space or a non-ASCII name survives (porcelain quotes both)
    tracked = {os.path.realpath(os.path.join(root, l)) for l in zsplit(git(root, "ls-files", "-z", "--", bdir))}
    dirty, entries = set(), zsplit(git(root, "status", "--porcelain", "-z", "--", bdir))
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if e[:1] in "RC":
            i += 1  # a rename/copy carries its source path as the next entry
        if len(e) > 3 and e[:2] != "??":
            dirty.add(os.path.realpath(os.path.join(top or root, e[3:])))
    return edited, tracked, dirty


def brief_age(path, fm, clock, now, days=DECAY_DAYS):
    """None when the brief is live — being edited (dirty), or created or body-edited fewer than
    `days` whole UTC days ago — else a short reason it is not. Whole UTC days because that is the
    calendar `created:` and `expires:` are written in (woosh stamps utcnow().date()): a rolling
    30x24h window flips a verdict mid-day, and local dates read a `created: 09-05` stamped at 21:53
    EDT as a day younger than its commit — sweep and brief_sweep disagreed on three briefs a day
    that way (2026-10-05). An untracked brief, or one in a repo with no history, falls back to the
    filesystem clock: the only one there is."""
    edited, tracked, dirty = clock
    p = os.path.realpath(path)
    if p in dirty:
        return None
    created = parse_iso((fm.get("created") or "")[:10])
    marks = [created.date()] if created else []
    if p in tracked:
        marks += [edited[p]] if p in edited else []
        why = "no body edit in {}d".format(days)
    else:
        try:
            marks.append(datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).date())
        except OSError:
            return "unreadable"
        why = "untouched on disk {}d"
    if not marks:
        return why
    idle = (now.astimezone(timezone.utc).date() - max(marks)).days
    return None if idle < days else why.format(idle)


def brief_expired(fm, now):
    """The author's own dated backstop (`expires: YYYY-MM-DD`, the lease rider) has passed."""
    exp = parse_iso((fm.get("expires") or "")[:10])
    return bool(exp and now.date() > exp.date())


def read_briefs(root):
    """[(path, frontmatter, text)] for every brief file that reads."""
    out = []
    for f in brief_files(root):
        try:
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        fm, _ = parse_frontmatter(text)
        out.append((f, fm, text))
    return out


def open_briefs(root, now=None):
    """(shown, live_total, decayed_total). Open briefs with a done_when that are live by the body
    clock and inside their own `expires`, newest `created` first. The SAME predicate `sweep`
    vents on, so the page and the files can never disagree about what decayed."""
    now = now or utcnow()
    briefs = [(f, fm) for f, fm, _ in read_briefs(root) if fm.get("status", "open") == "open" and fm.get("done_when")]
    if not briefs:
        return [], 0, 0
    clock = body_clock(root)
    out, decayed = [], 0
    for f, fm in briefs:
        if brief_age(f, fm, clock, now) or brief_expired(fm, now):
            decayed += 1
            continue
        out.append({"title": fm.get("title", os.path.basename(f)), "created": fm.get("created", "?"),
                    "path": os.path.relpath(f, root)})
    out.sort(key=lambda b: b["created"], reverse=True)
    return out[:MAX_BRIEFS], len(out), decayed


# ---------------------------------------------------------------- orient

def tier_mark(entry):
    """Render the trust tier so a reader never mistakes model prose for the human's words.
    Authored is the default reading and gets no mark; carried entries name where they came from."""
    if not isinstance(entry, dict):
        return ""
    bits = []
    if entry.get("tier") in ("derived", "interpreted"):
        bits.append(entry["tier"])
    if entry.get("origin_sid8"):
        bits.append("from " + entry["origin_sid8"])
    if entry.get("intent_verdict"):
        bits.append("intent " + str(entry["intent_verdict"]))
    return "  [{}]".format(", ".join(bits)) if bits else ""


def orient_lines(root, sid8):
    now = utcnow()
    name = os.path.basename(os.path.abspath(root))
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD") or "?"
    sha = git(root, "rev-parse", "--short", "HEAD") or "?"
    status = git(root, "status", "--porcelain")
    dirty = len([l for l in (status or "").splitlines() if l.strip()])
    L = ["# orient — {} @ {} {} ({} uncommitted)".format(name, branch, sha, dirty), RECORDED_NOTICE]

    last = last_archive(root)
    since = None
    if last:
        ended = parse_iso(last.get("ended_at"))
        age = "{}d ago".format((now - ended).days) if ended else "date unknown"
        L.append("Last session: {} ended {} ({})".format(last.get("sid8", "?"), last.get("ended_at", "?"), age))
        # The human's own words frame the synthesis, never the other way round: verbatim, before tldr.
        if last.get("human_first"):
            L.append("  human first: " + last["human_first"])
        if last.get("human_last") and last["human_last"] != last.get("human_first"):
            L.append("  human last: " + last["human_last"])
        if last.get("tldr"):
            L.append("  tldr: " + last["tldr"])
        if isinstance(last.get("intent"), dict) and last["intent"].get("statement"):
            L.append("  declared: " + last["intent"]["statement"])
        ots = last.get("open_threads") or []
        if ots:
            L.append("  open threads:")
            for t in ots:
                if isinstance(t, dict):
                    L.append("    - {} -> {}{}".format(t.get("thread") or t.get("text", "?"), t.get("next_step", "?"), tier_mark(t)))
                else:
                    L.append("    - {}".format(t))
        wos = last.get("watch_outs") or []
        if wos:
            L.append("  watch outs:")
            for w in wos:
                L.append("    - {}{}".format(w.get("text", "?") if isinstance(w, dict) else w, tier_mark(w)))
        since = ended
    else:
        L.append("No handoff yet — this is the first recorded session in this repo.")

    superseded = 0
    for d in drafts(root, exclude_sid8=sid8):
        if superseding_archive(root, d.get("sid8", "?"), parse_iso(d.get("started_at"))):
            superseded += 1  # its SessionEnd never ran to delete it; the archive is the record
            continue
        L.append("Unfinalized draft from {} (started {}): {}".format(
            d.get("sid8", "?"), d.get("started_at", "?"), d.get("tldr") or d.get("session_topic") or "(empty)"))
        # A live declaration is the one thing another session most needs to know: what is being worked.
        if isinstance(d.get("intent"), dict) and d["intent"].get("statement"):
            L.append("  declared: " + d["intent"]["statement"])
    if superseded:
        L.append("({} stale draft{} hidden: already archived by /handoff)".format(superseded, "" if superseded == 1 else "s"))

    log_args = ["log", "--format=%h %s", "-n", "8"]
    if since:
        log_args.append("--since=" + iso(since))
    commits = (git(root, *log_args) or "").splitlines()
    if commits:
        L.append("Commits since then:" if since else "Recent commits:")
        for c in commits:
            L.append("  " + c)

    briefs, total, decayed = open_briefs(root, now)
    if briefs:
        vent = "; {} decayed, not shown — `shed.py sweep` lists them".format(decayed) if decayed else ""
        L.append("Open briefs (status open, body edited within {}d; {} of {}, newest first{}):".format(
            DECAY_DAYS, len(briefs), total, vent))
        for b in briefs:
            L.append("  - {} ({}) {}".format(b["title"][:90], b["created"], b["path"]))
    return L


def declare_hint(root, sid8):
    me = os.path.abspath(__file__)
    rel = os.path.relpath(me, root)
    ref = me if rel.startswith("..") else rel
    return ('Before editing, declare what this session will do: python3 {} declare "<one sentence>" --sid8 {} '
            '[--cmd "<exits 0 when done>" --expires YYYY-MM-DD] — the handoff reads it back.').format(ref, sid8)


def cmd_orient(args, payload=None):
    root = args.root
    sid8 = sid8_from(args.sid8, payload)
    lines = orient_lines(root, sid8)
    if getattr(args, "declare_hint", False):
        # The page is the only instruction a fresh repo carries (no CLAUDE.md prose), so it has to say HOW to
        # declare, or no session ever does (first run on athena, 2026-09-30). Under the notice, so the ceiling
        # (which cuts from the tail) never reaches it; the sid8 is spelled out because the env var is not a guarantee.
        lines.insert(2, declare_hint(root, sid8))
    cap = args.max_lines
    if cap and len(lines) > cap:
        hidden = len(lines) - (cap - 1)
        lines = lines[: cap - 1] + ["… +{} lines hidden by the ceiling (shed.py orient --max-lines 0 for all)".format(hidden)]
    page = "\n".join(lines)
    metric(root, "orient", sid8=sid8, lines=len(lines), chars=len(page), est_tokens=len(page) // 4)
    print(page)
    return 0


# ---------------------------------------------------------------- transcript

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def read_transcript(path):
    """Claude Code transcript (.jsonl). Returns {first_user, last_user, last_assistant, turns, first_ts, last_ts,
    turn_list}. `turn_list` keeps every text turn as {index, uuid, role, text} — the evidence gate
    resolves `evidence.turn` (an index or a uuid) against it. Turns whose text starts with '<' are
    harness-injected (system-reminder, command output), never the human's words."""
    out = {"first_user": "", "last_user": "", "last_assistant": "", "turns": 0, "first_ts": None, "last_ts": None, "turn_list": []}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                kind = e.get("type")
                msg = e.get("message") or {}
                if kind not in ("user", "assistant") or not isinstance(msg, dict):
                    continue
                text = _text_of(msg.get("content")).strip()
                if not text:
                    continue
                if out["first_ts"] is None:
                    out["first_ts"] = e.get("timestamp")
                out["last_ts"] = e.get("timestamp") or out["last_ts"]
                out["turn_list"].append({"index": out["turns"], "uuid": e.get("uuid"), "role": kind, "text": text})
                out["turns"] += 1
                if kind == "user" and not text.startswith("<"):
                    if not out["first_user"]:
                        out["first_user"] = text
                    out["last_user"] = text
                if kind == "assistant":
                    out["last_assistant"] = text
    except OSError:
        pass
    return out


def first_sentence(text, limit=200):
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s[:limit]


def verbatim_sentence(text, limit=300):
    """First sentence of `text` as a byte-for-byte span of it (no whitespace normalization) —
    the human's words go in the record exactly as typed. Capped, never rewritten."""
    text = text.strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text, re.S)
    s = m.group(1) if m else text
    return s[:limit]


# ---------------------------------------------------------------- grounding

TIERS = tuple(schema("tier")["enum"])  # weft's ai.mythrl.tier enum, verbatim (schemas/tier.json)
GATED_FIELDS = ("open_threads", "decisions", "watch_outs")


def _entry_text(entry):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return " ".join(str(entry.get(k, "")) for k in ("thread", "next_step", "text", "decision", "blockers") if entry.get(k))
    return ""


def _claim_text(entry):
    """The load-bearing sentence of an entry — the part that asserts something happened or is true:
    `thread` on an open thread, `text` on a watch-out or a decision. This is what the gate grounds.
    `next_step`, `blockers` and `gloss` are prospective or explanatory prose, not claims, and are
    never gated; orient marks the whole entry's tier so a reader knows what they are."""
    if not isinstance(entry, dict):
        return ""
    for k in ("thread", "text", "decision"):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _find_turn(turn_list, ref):
    if isinstance(ref, bool) or ref is None:
        return None
    if isinstance(ref, int):
        return next((t for t in turn_list if t["index"] == ref), None)
    if isinstance(ref, str):
        if ref.isdigit():
            return next((t for t in turn_list if t["index"] == int(ref)), None)
        return next((t for t in turn_list if t.get("uuid") == ref), None)
    return None


def _strip_origin(entry):
    return {k: v for k, v in entry.items() if k != "origin_sid8"} if isinstance(entry, dict) else entry


def evidence_gate(rec, turn_list, root):
    """Every model-written entry in open_threads / decisions / watch_outs must be grounded.
    Returns a list of rejection strings (empty = pass). No judgment: substring and byte-equality.

    The enum is weft's tier rider (weft/schemas/ai.mythrl.tier.json) verbatim — accountability
    structure, not species. Whether an entry is a verbatim span or a paraphrase is not a tier:
    it is whether `evidence` is present and survives the substring test.

      tier: authored    an identified party stands behind it — never gated, never decays
      tier: derived     machine-computed, reproducible — evidence {turn} required; the entry's CLAIM
                        TEXT (thread / text) must be a verbatim span of that turn
      tier: interpreted stateless synthesis (model prose) — same rule. The paraphrase goes in `gloss`,
                        which is never gated and always rendered as interpreted
      origin_sid8       carried from an earlier archive — must be byte-identical to an entry in it
      (no tier)         rejected: a record that cannot say who stands behind a line does not carry it
                        (schemas/tier.json — absence is unstated, and unstated never renders as authored)
    Strings (no dict) are unknown-author and cannot carry evidence, so they are rejected.

    Why the claim itself and not an attached quote: a gate that checks "some quote is in some turn"
    binds nothing — a fabricated thread glued to a real, unrelated four-letter quote passed it
    (adversarial pass, 2026-09-25). Grounding the claim text makes the fabrication impossible to
    write: the sentence has to exist in the source. `evidence.quote` is still accepted and, when
    present, must be a substring too; it is no longer what the gate is about."""
    rejects = []
    origins = {}
    for field in GATED_FIELDS:
        for i, entry in enumerate(rec.get(field) or []):
            label = "{}[{}]".format(field, i)
            if isinstance(entry, str):
                rejects.append("{}: bare string {!r} — wrap as {{text, tier, evidence}}".format(label, entry[:60]))
                continue
            if not isinstance(entry, dict):
                rejects.append("{}: not an object".format(label))
                continue
            tier = entry.get("tier")
            if tier is None:
                rejects.append("{}: no tier (one of {})".format(label, "/".join(TIERS)))
                continue
            if tier not in TIERS:
                rejects.append("{}: unknown tier {!r} (one of {})".format(label, tier, "/".join(TIERS)))
                continue
            osid = entry.get("origin_sid8")
            if osid:
                if osid not in origins:
                    origins[osid] = archive_for(root, osid)
                src = origins[osid]
                if not src:
                    rejects.append("{}: origin_sid8 {} has no archive under handoff/sessions/".format(label, osid))
                    continue
                if _strip_origin(entry) not in [_strip_origin(e) for e in (src.get(field) or [])]:
                    rejects.append("{}: carried from {} but not byte-identical to any {} entry there".format(label, osid, field))
                continue
            if tier == "authored":
                continue
            ev = entry.get("evidence")
            if not isinstance(ev, dict) or "turn" not in ev:
                rejects.append("{}: no evidence {{turn}} on a {} entry".format(label, tier))
                continue
            turn = _find_turn(turn_list, ev["turn"])
            if turn is None:
                rejects.append("{}: evidence.turn {!r} is not a turn in the transcript".format(label, ev["turn"]))
                continue
            claim = _claim_text(entry)
            if not claim:
                rejects.append("{}: no claim text (thread / text) to ground".format(label))
                continue
            if claim not in turn["text"]:
                rejects.append("{}: claim is not a verbatim span of turn {} — the span goes in thread/text, the paraphrase in gloss".format(label, ev["turn"]))
                continue
            q = ev.get("quote")
            if isinstance(q, str) and q.strip() and q not in turn["text"]:
                rejects.append("{}: quote is not a substring of turn {}".format(label, ev["turn"]))
    return rejects


# ---------------------------------------------------------------- declare

DISCHARGE_TIMEOUT = 20


def run_discharge(test, cwd, timeout=DISCHARGE_TIMEOUT):
    """Run one discharge test (schemas/discharge.json) and read it back: (verdict, rc), verdict one of
    MET / UNMET / ERROR. rc 124 (timeout), 126 / 127 (unrunnable) and git's fatal 128 are ERROR —
    instrument silent, never a verdict — the same rule hololoom_mcp's tools/session_intent.py applies."""
    try:
        r = subprocess.run(test["cmd"], shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        rc, out = r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return "ERROR", 124
    except OSError:
        return "ERROR", 127
    if rc in (124, 126, 127, 128):
        return "ERROR", rc
    holds = rc == int(test.get("expect_rc", 0))
    if holds and test.get("expect_re"):
        holds = re.search(test["expect_re"], out or "") is not None
    if test.get("expect_absent"):
        holds = not holds
    return ("MET" if holds else "UNMET"), rc


def discharge_intent(rec, root):
    """At finalize, read the declaration back. MET becomes a progress line. UNMET, UNARMED and ERROR
    make the statement an open thread — verbatim, tier authored, carrying the declaration's own
    done_when / expires / scope — so the next session's page names what this one meant to do and did
    not finish. Forward-intent without a model in the seat: the verb that was declared is the thread
    that is open. Returns the verdict; ABSENT when nothing was declared."""
    intent = rec.get("intent")
    if not isinstance(intent, dict) or not intent.get("statement"):
        return "ABSENT"
    test = intent.get("done_when")
    if not isinstance(test, dict) or not test.get("cmd"):
        verdict, rc = "UNARMED", None
    else:
        verdict, rc = run_discharge(test, root)
    stmt = intent["statement"]
    if verdict == "MET":
        rec.setdefault("progress", []).append("intent MET: " + stmt)
        return verdict
    thread = {"thread": stmt, "tier": "authored", "intent_verdict": verdict}
    if verdict == "UNARMED":
        thread["next_step"] = "declared, never armed: confirm by hand, or re-declare with --cmd so a handoff can read it back"
    elif verdict == "ERROR":
        thread["next_step"] = "done_when could not run (rc {}): fix the command or its scope, then re-run it".format(rc)
        thread["blockers"] = "instrument silent — not a verdict"
    else:
        thread["next_step"] = "finish what was declared; done_when says when (last rc {})".format(rc)
    for k in ("done_when", "expires", "scope"):
        if k in intent:
            thread[k] = intent[k]
    rec.setdefault("open_threads", []).append(thread)
    return verdict


def cmd_declare(args, payload=None):
    """`shed.py declare "<statement>" [--cmd … --expires …]` — the one field written BEFORE the work.
    Lands in this session's draft as `intent` (schemas/intent.json); the Stop hook carries it; finalize
    reads it back (discharge_intent). Re-declaring keeps the old statement under `superseded`."""
    root = args.root
    sid8 = sid8_from(args.sid8, payload)
    draft_path = os.path.join(paths(root)["drafts"], sid8 + ".json")
    now = utcnow()
    statement = " ".join((args.statement or "").split())
    if not statement:
        print("declare: empty statement")
        return 2
    intent = {"statement": statement, "declared_at": iso(now), "sid8": sid8}
    if args.cmd:
        if not args.expires:
            print("declare: --cmd needs --expires YYYY-MM-DD — a predicate with no dated backstop can never auto-close")
            return 2
        test = {"cmd": args.cmd, "expect_rc": args.expect_rc}
        if args.expect_re:
            test["expect_re"] = args.expect_re
        if args.expect_absent:
            test["expect_absent"] = True
        intent["done_when"] = test
    if args.expires:
        intent["expires"] = args.expires
        intent["scope"] = args.scope
    prev = load_json(draft_path) or {}
    old = prev.get("intent")
    if isinstance(old, dict) and old.get("statement"):
        if old["statement"] == statement and old.get("done_when") == intent.get("done_when"):
            print("already declared: " + statement)
            return 0
        superseded = list(old.get("superseded") or [])
        gone = {"statement": old["statement"], "declared_at": old.get("declared_at", "?")}
        if args.reason:
            gone["reason"] = args.reason
        superseded.append(gone)
        intent["superseded"] = superseded
    problems = check_shape(intent, schema("intent"))
    if problems:
        print("declare REFUSED — not schemas/intent.json's shape:")
        for pr in problems:
            print("  - " + pr)
        return 1
    prev["intent"] = intent
    prev.setdefault("sid8", sid8)
    prev.setdefault("source", "shed")
    prev.setdefault("draft", True)
    write_json(draft_path, prev)
    metric(root, "declare", sid8=sid8, armed="done_when" in intent, superseded=len(intent.get("superseded") or []))
    print("declared -> {}".format(os.path.relpath(draft_path, root)))
    print("  intent : " + statement)
    if "done_when" in intent:
        print("  done_when: {}  (expires {}, scope {})".format(intent["done_when"]["cmd"], intent["expires"], intent["scope"]))
    else:
        print("  unarmed: finalize will carry the statement as an open thread unless it is re-declared with --cmd")
    return 0


def archive_for(root, sid8):
    files = sorted(glob.glob(os.path.join(paths(root)["sessions"], "*__{}.json".format(sid8))))
    for f in reversed(files):
        d = load_json(f)
        if isinstance(d, dict):
            return d
    return None


def _ngrams(text, n=3):
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def extractiveness(rec, turn_list, commits, n=3):
    """Share of the record's UNGATED model prose word n-grams — `tldr`, and each interpreted entry's
    `gloss` / `next_step` / `blockers` — that occur in the sources (transcript turns + commit
    subjects). The gated claim text is a verbatim span by construction (the gate rejects anything
    else) and would score 1.0 whatever the model did, so it is left out; the number is over the
    prose the gate does not touch. 1.0 = every phrase is in the source; falling across sessions =
    drift. None when there is too little such prose to form an n-gram. A mechanical draft's tldr is
    a copied sentence, so a mechanical record scores 1.0 — that is true of it, not a flaw in the
    number; the number starts meaning something when a model writes the prose."""
    synth = [rec.get("tldr") or ""]
    for field in GATED_FIELDS:
        for e in rec.get(field) or []:
            if not isinstance(e, dict) or e.get("tier") in ("authored", "derived"):
                continue
            synth.extend(str(e[k]) for k in ("gloss", "next_step", "blockers") if e.get(k))
    # n-grams per field, never across a field boundary: a phrase that straddles two fields is not a
    # phrase anyone wrote (joining them counted "gadget started on" as a miss).
    grams = set()
    for s in synth:
        grams |= _ngrams(s, n)
    if not grams:
        return None
    source = set()
    for s in [t["text"] for t in turn_list] + list(commits):
        source |= _ngrams(s, n)
    return round(len(grams & source) / len(grams), 3)


# ---------------------------------------------------------------- private layer

def split_record(rec, share=()):
    """One draft -> (shared, private). The shared record is an ALLOWLIST projection of the draft:
    only SHARED_FIELDS, plus any PRIVATE_FIELDS named in `share` — the contracted emission. The
    private record carries every private field and a pointer back to the archive it belongs to.
    A key in neither table is dropped from both, on purpose: unknown fields do not cross."""
    unknown = [f for f in share if f not in PRIVATE_FIELDS]
    if unknown:
        raise ValueError("--share names a field that is not private: {} (private fields: {})".format(
            ", ".join(unknown), ", ".join(PRIVATE_FIELDS)))
    shared = {k: rec[k] for k in SHARED_FIELDS if k in rec}
    for k in share:
        if k in rec:
            shared[k] = rec[k]
    shared["shared_fields"] = sorted(set(share))
    private = {k: rec.get(k) for k in PRIVATE_FIELDS}
    private["sid8"] = rec.get("sid8")
    # Commit-and-reveal (docs/design/sealed_introspection_handoff.md §4 primitive 3, Blake's item-17
    # rider): the shared record carries the HASH of the private one, never its contents. A later
    # `--share` is checkable by anyone holding the shared file — text that hashes to the recorded
    # value is what was recorded; text that does not is not. The private file itself stays sealed
    # by the git boundary, not by cryptography; that is level zero.
    shared["private_sha256"] = private_digest(private)
    return shared, private


def private_digest(private):
    """SHA-256 over the canonical JSON of the private fields only (sorted keys, no pointer back to
    the archive, no timestamps) so the digest is a function of the words alone."""
    body = {k: private.get(k) for k in PRIVATE_FIELDS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def verify_share(shared, private):
    """Does this private record match the commitment in this shared one? Pure, no I/O."""
    return bool(shared.get("private_sha256")) and shared["private_sha256"] == private_digest(private)


# ---------------------------------------------------------------- handoff

def trunk_ref(root):
    """The published trunk ref (origin/main, else origin/master), or None when neither resolves."""
    for cand in ("origin/main", "origin/master"):
        if git(root, "rev-parse", "--verify", "-q", cand + "^{commit}") is not None:
            return cand
    return None


def trunk_archives(root, sessions_dir, sid8):
    """Archives for sid8 that trunk holds but this checkout may not. A /handoff record published by
    pushing a sha never lands in a shared checkout's tree until someone pulls, so a finalize run from
    that checkout cannot see it on disk (measured 2026-09-28: 4 of 18 backlog finalizes duplicated
    a richer archive already on origin/main). Yields (label, record); empty when git cannot read."""
    trunk = trunk_ref(root)
    rel = os.path.relpath(sessions_dir, root)
    if not trunk or rel.startswith(".."):
        return
    names = git(root, "ls-tree", "--name-only", trunk, "./" + rel + "/")
    for name in (names or "").splitlines():
        if not name.endswith("__{}.json".format(sid8)):
            continue
        body = git(root, "show", "{}:./{}".format(trunk, name))
        try:
            d = json.loads(body) if body else None
        except ValueError:
            d = None
        if isinstance(d, dict):
            yield "{}:{}".format(trunk, name), d


def git_scope(root):
    """Which commits count as this session's. On a session branch (anything but the trunk) they
    are the branch's own commits, `origin/<trunk>..HEAD` — a peer's commits on trunk in the same
    time window are not ours, however many sessions share the clock. On the trunk itself (a shared
    checkout) there is no branch to scope by, so it stays a time window and the basis SAYS so:
    commits by any session in the window. Paths are always scoped to the project root (`-- .`),
    so a monorepo sibling's files never land in files_touched. (Both leaks were measured on
    hololoom_mcp's shared checkout, 2026-09-25: a peer's commit became a session's first commit,
    and `garage/` from the monorepo root sat in its files_touched.)"""
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    trunk = trunk_ref(root)
    if branch and branch != "HEAD" and trunk and branch != trunk.split("/", 1)[1]:
        return {"range": [trunk + "..HEAD"],
                "detail": "branch {}: commits in {}..HEAD under the project root".format(branch, trunk)}
    return {"range": [],
            "detail": "trunk checkout ({}): time window only — commits by ANY session in it, under the project root".format(branch or "?")}


def cmd_handoff(args, payload=None):
    root = args.root
    sid8 = sid8_from(args.sid8, payload)
    transcript = args.transcript or (payload or {}).get("transcript_path")
    p = paths(root)
    draft_path = os.path.join(p["drafts"], sid8 + ".json")
    now = utcnow()

    prev = load_json(draft_path) or {}
    tr = read_transcript(transcript)
    started = parse_iso(prev.get("started_at")) or parse_iso(tr["first_ts"]) or now

    # basis (schemas/basis.json): one reading per instrument, so a transcript that was never on disk
    # is BLIND and never reads as a silent session, and a repo git cannot read is BLIND, not empty.
    basis = [{"instrument": "transcript", "observed_at": iso(now),
              "state": "BLIND" if not (transcript and os.path.exists(transcript)) else ("read" if tr["turns"] else "empty")},
             {"instrument": "git", "observed_at": iso(now),
              "state": "BLIND" if git(root, "rev-parse", "--git-dir") is None else "read"}]
    if basis[0]["state"] == "BLIND":
        basis[0]["detail"] = "no transcript at {!r}".format(transcript)

    # Which commits are THIS session's: the branch's own, under the project root (git_scope).
    scope = git_scope(root)
    if basis[1]["state"] == "read":
        basis[1]["detail"] = scope["detail"]
    since = ["--since=" + iso(started)] + scope["range"]
    log = git(root, "log", "--format=%h %s", *(since + ["--", "."])) or ""
    commits = [l for l in log.splitlines() if l.strip()]
    touched = set()
    files = git(root, "log", "--name-only", "--format=", *(since + ["--", "."])) or ""
    touched.update(l.strip() for l in files.splitlines() if l.strip())
    status = git(root, "status", "--porcelain", "--", ".") or ""
    touched.update(l[3:].strip() for l in status.splitlines() if l.strip())

    first_commit_s = None
    ts = git(root, "log", "--reverse", "--format=%cI", "-n", "1", *(since + ["--", "."]))
    fc = parse_iso(ts) if ts else None
    if fc:
        first_commit_s = int((fc - started).total_seconds())

    rec = {
        "sid8": sid8,
        "source": "shed",
        "draft": True,
        "session_topic": first_sentence(tr["first_user"], 160) or prev.get("session_topic", ""),
        "human_first": verbatim_sentence(tr["first_user"]) or prev.get("human_first", ""),
        "human_last": verbatim_sentence(tr["last_user"]) or prev.get("human_last", ""),
        "tldr": first_sentence(tr["last_assistant"]) or prev.get("tldr", ""),
        "summary": "Mechanical draft by shed: topic = first user turn, human_first/human_last = the human's "
                   "own first and last sentences verbatim (never regenerated), tldr = last assistant turn, "
                   "progress = this session's commits (branch-scoped where there is a branch; basis[git] says "
                   "which). open_threads / watch_outs / decisions need a human or a model to fill, except "
                   "the declared intent, which finalize reads back: MET -> progress, otherwise -> an authored "
                   "open thread carrying its own done_when. Empty here is 'unknown', not 'none'. Each filled "
                   "entry is an object {thread|text (a VERBATIM span of the cited turn), gloss (free prose, "
                   "never gated), tier: authored|derived|interpreted, evidence: {turn}} or a byte-identical "
                   "copy of an earlier archive's entry naming origin_sid8; finalize rejects anything else.",
        "started_at": iso(started),
        "ended_at": None,
        "turns": tr["turns"],
        "progress": list(commits),  # a copy: finalize appends "intent MET" here, and commits is the metric's count
        "open_threads": prev.get("open_threads", []),
        "decisions": prev.get("decisions", []),
        "watch_outs": prev.get("watch_outs", []),
        "files_touched": sorted(touched),
        "first_commit_seconds": first_commit_s,
        "basis": basis,
        "transcript_path": transcript,
    }
    if isinstance(prev.get("intent"), dict):
        rec["intent"] = prev["intent"]  # written by `declare` before the work; carried, never rewritten

    if args.finalize:
        # A richer archive for this session may already exist (a /handoff skill, a human).
        # shed is the backstop, never the overwrite: if one landed after we started, stand down.
        label = superseding_archive(root, sid8, started)
        if label:
            try:
                os.remove(draft_path)
            except OSError:
                pass
            print("archive already present for {}: {} (shed stood down)".format(sid8, label))
            return 0
        if getattr(args, "seat", False):
            # The draft seat, once, at the end — after the stand-down (no model call for a record
            # that will not be written), before the gate (which judges what the seat kept like
            # anything else). A seat failure costs nothing: the lists stand as they were.
            write_json(draft_path, rec)
            run_seat(args, payload)
            seated = load_json(draft_path) or {}
            for field in GATED_FIELDS:
                if isinstance(seated.get(field), list):
                    rec[field] = seated[field]
        verdict = discharge_intent(rec, root)
        if verdict != "ABSENT":
            metric(root, "intent", sid8=sid8, verdict=verdict)
        rejects = evidence_gate(rec, tr["turn_list"], root)
        if rejects:
            # The draft stays on disk untouched so nothing is lost; the next orient shows it unfinalized.
            write_json(draft_path, rec)
            print("finalize REJECTED for {} — {} ungrounded entr{} (draft kept at {}):".format(
                sid8, len(rejects), "y" if len(rejects) == 1 else "ies", os.path.relpath(draft_path, root)))
            for r in rejects:
                print("  - " + r)
            metric(root, "handoff_rejected", sid8=sid8, rejects=len(rejects))
            return 1
        rec["draft"] = False
        # The session ended at its last turn, not when finalize ran: a backlog finalized days later
        # (measured 2026-09-28, 18 archives stamped the same second) would otherwise read as the
        # newest work and bury the real last session. Fall back to now when the turn is unreadable.
        last = parse_iso(tr.get("last_ts"))
        ended_at = last if last and started <= last <= now else now
        rec["ended_at"] = iso(ended_at)
        rec["extractiveness"] = extractiveness(rec, tr["turn_list"], commits)
        try:
            shared, private = split_record(rec, getattr(args, "share", None) or ())
        except ValueError as e:
            write_json(draft_path, rec)
            print("finalize REJECTED for {}: {} (draft kept)".format(sid8, e))
            return 1
        # The shared record must be the contract's shape (schemas/record.json) before it is written:
        # the gate above judged the evidence, this judges the form. Fail closed, draft kept.
        problems = check_shape(shared, schema("record"))
        if problems:
            write_json(draft_path, rec)
            print("finalize REJECTED for {} — shared record is not schemas/record.json's shape (draft kept):".format(sid8))
            for pr in problems:
                print("  - " + pr)
            metric(root, "handoff_rejected", sid8=sid8, rejects=len(problems), why="schema")
            return 1
        final = os.path.join(p["sessions"], "{}__{}.json".format(stamp(ended_at), sid8))
        private["archive"] = os.path.basename(final)
        private["ended_at"] = rec["ended_at"]
        write_json(os.path.join(p["private"], sid8 + ".json"), private)
        write_json(final, shared)
        try:
            os.remove(draft_path)
        except OSError:
            pass
        metric(root, "handoff", sid8=sid8, commits=len(commits), turns=tr["turns"],
               first_commit_seconds=first_commit_s, extractiveness=rec["extractiveness"],
               path=os.path.relpath(final, root))
        print(os.path.relpath(final, root))
    else:
        write_json(draft_path, rec)
        print(os.path.relpath(draft_path, root))
    return 0


# ---------------------------------------------------------------- draft seat

SEAT_ENDPOINT = "http://127.0.0.1:8080/v1"  # llama-server's default; ollama is :11434/v1, mlx_lm.server :8080/v1
SEAT_BUDGET = 12000   # transcript chars sent, taken from the end — recent turns carry the open threads
SEAT_TURN_CAP = 3000  # one long turn must not eat the whole budget; the gate still reads the full turn
SEAT_TIMEOUT = 20     # the SessionEnd hook has 30s; the draft refresh and finalize share it
SEAT_KEYS = {"open_threads": ("thread", "next_step", "gloss"),
             "decisions": ("text", "gloss"),
             "watch_outs": ("text", "gloss")}
SEAT_SCHEMA = {"open_threads": "thread", "decisions": "decision", "watch_outs": "thread"}
SEAT_SYSTEM = """You read a coding-session transcript and extract the notes the next session needs.
Reply with ONE JSON object and nothing else:
{"open_threads": [...], "decisions": [...], "watch_outs": [...]}

open_threads: work left unfinished, or promised next.  {"thread": "<copied span>", "turn": N, "next_step": "<optional>"}
decisions:    choices that were settled.               {"text": "<copied span>", "turn": N}
watch_outs:   hazards, gotchas, things that broke.     {"text": "<copied span>", "turn": N}

"thread" and "text" MUST be copied character for character from the single turn numbered N.
Do not reword them. Anything not found verbatim in turn N is discarded.
Optional on any entry: "gloss", one short sentence in your own words.
Use empty lists when nothing fits. At most 5 entries per list."""


def seat_spec(spec):
    """`NAME` or `NAME@URL` -> (name, base url). SHED_ENDPOINT overrides the default URL."""
    name, _, url = (spec or "").partition("@")
    return name.strip(), (url.strip() or os.environ.get("SHED_ENDPOINT") or SEAT_ENDPOINT).rstrip("/")


def seat_transcript(turn_list, budget=SEAT_BUDGET):
    """Numbered turns, newest kept first until the budget is spent, rendered oldest-first."""
    lines, used = [], 0
    for t in reversed(turn_list):
        text = t["text"] if len(t["text"]) <= SEAT_TURN_CAP else t["text"][:SEAT_TURN_CAP] + " [...]"
        line = "[{}] {}: {}".format(t["index"], t["role"], text)
        if lines and used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    return "\n\n".join(reversed(lines))


def ask_seat(url, model, system, user, timeout=SEAT_TIMEOUT):
    """One POST to an OpenAI-compatible /chat/completions. Returns the reply text, or (None, why).
    Any failure — down, timeout, HTTP error, odd envelope — is silence, never a verdict."""
    import urllib.request
    import urllib.error
    body = json.dumps({"model": model, "temperature": 0, "stream": False,
                       "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode("utf-8")
    req = urllib.request.Request(url + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            env = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, "endpoint: {}".format(e)
    try:
        choice = env["choices"][0]
        if choice.get("finish_reason") == "length":
            return None, "reply truncated (finish_reason=length)"
        return choice["message"]["content"], None
    except (KeyError, IndexError, TypeError, AttributeError):
        return None, "reply is not a chat completion"


def parse_seat(text):
    """The reply must be one JSON object. A single ```json fence around it is tolerated (small
    models add it by habit); prose, several objects, or a truncated object is silence."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```$", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def seat_entries(obj):
    """Proposals -> entries in the gated shape. Only the listed keys survive, and the tier is set
    here, not by the model: the seat writes `interpreted` and nothing else — it cannot claim
    `authored`, and it cannot carry (origin_sid8 is never the model's to write)."""
    out = {f: [] for f in GATED_FIELDS}
    for field in GATED_FIELDS:
        items = obj.get(field)
        if not isinstance(items, list):
            continue
        for p in items:
            if not isinstance(p, dict):
                out[field].append(p)  # counted as proposed, rejected by the gate as it stands
                continue
            e = {k: p[k] for k in SEAT_KEYS[field] if isinstance(p.get(k), str) and p[k].strip()}
            turn = p.get("turn")
            if turn is None and isinstance(p.get("evidence"), dict):
                turn = p["evidence"].get("turn")
            e["tier"] = "interpreted"
            if isinstance(turn, (int, str)) and not isinstance(turn, bool):
                e["evidence"] = {"turn": turn}
            out[field].append(e)
    return out


# ---------------------------------------------------------------- relevance judge (optional, after the gate)
#
# The gate checks grounding, not relevance: a model that copies the assistant's own status chatter
# ("The server is up. Now the real run…") passes it. A typed judge — one prefill, a softmax over
# option labels, no decode (Kev / Mica on TypeSafe's System One wire) — reads each kept entry and
# says whether it is anything a later session needs. It may only DROP a seat-written entry, which
# moves the record back toward empty, the baseline with no seat at all; it never adds, rewrites,
# or touches an authored or carried entry. Every failure keeps the entry.

JUDGE_ENDPOINT = "http://127.0.0.1:18008"  # Kev's serve port here; Mica is conventionally :18010
JUDGE_TAU = 0.8       # drop only at this averaged narration probability or above. First Mica smoke
                      # (2026-10-01): real handoff entries topped out at 0.72, so 0.8 leaves margin
                      # while still dropping about half of real status narration
JUDGE_BUDGET = 6.0    # seconds for all judge calls; seat (20) + judge (6) stay inside SessionEnd's 30
JUDGE_STATE_CAP = 1200  # chars of claim text sent; the judges were trained on short states
JUDGE_QUESTION = ("This line was copied from a coding session's transcript to hand to the NEXT session, "
                  "which starts cold. What kind of line is it?")
JUDGE_OPTIONS = {
    "forward": "Unfinished work, or something promised or planned for later",
    "settled": "A decision or ruling the next session should keep to",
    "hazard": "A warning, gotcha, failure, or constraint worth knowing",
    "narration": "In-the-moment status chatter or an offer to the user (\"Now running X\", "
                 "\"Want me to…?\"); nothing a later session needs",
}


def judge_specs(spec):
    """`NAME[@URL][,NAME[@URL]...]` -> [(name, base url)]. Several judges are averaged."""
    out = []
    for part in (spec or "").split(","):
        name, _, url = part.strip().partition("@")
        if name.strip():
            out.append((name.strip(), (url.strip() or JUDGE_ENDPOINT).rstrip("/")))
    return out


def ask_judge(url, model, state, timeout):
    """One System One choice question. Returns ({option: prob}, None) or (None, why)."""
    import urllib.request
    import urllib.error
    body = json.dumps({"model": model, "state": state,
                       "questions": {"q": {"type": "choice", "instructions": JUDGE_QUESTION,
                                           "criteria": JUDGE_OPTIONS}}}).encode("utf-8")
    req = urllib.request.Request(url + "/v1/systemone", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            env = json.loads(r.read().decode("utf-8"))
        probs = env["answers"]["q"]["probabilities"]
        p = {k: float(probs[k]) for k in JUDGE_OPTIONS}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return None, "judge {}: {}".format(model, e)
    total = sum(p.values())
    if total <= 0:
        return None, "judge {}: no probability mass on the options".format(model)
    return {k: v / total for k, v in p.items()}, None


def judge_kept(kept, judges, tau=JUDGE_TAU, budget=JUDGE_BUDGET, clock=time.monotonic):
    """Drop seat entries every judge answered on and whose averaged narration probability is >= tau.
    Returns (kept, stats). An entry a judge could not answer on, or one the budget ran out
    before, stays — silence is never a verdict."""
    deadline = clock() + budget
    stats = {"judged": 0, "dropped": 0, "unjudged": 0, "judge_silent": 0, "rows": []}
    out = {f: [] for f in kept}
    for field, entries in kept.items():
        for e in entries:
            state = (_claim_text(e) or "")[:JUDGE_STATE_CAP]
            answers = []
            for name, url in judges:
                left = deadline - clock()
                if left <= 0:
                    break
                p, _why = ask_judge(url, name, state, min(left, 5.0))
                if p is None:
                    break
                answers.append(p)
            if len(answers) < len(judges):
                stats["unjudged" if deadline - clock() <= 0 else "judge_silent"] += 1
                out[field].append(e)
                continue
            avg = {k: sum(a[k] for a in answers) / len(answers) for k in JUDGE_OPTIONS}
            choice = max(avg, key=avg.get)
            drop = choice == "narration" and avg["narration"] >= tau
            stats["judged"] += 1
            stats["dropped"] += int(drop)
            stats["rows"].append({"field": field, "choice": choice, "p_narration": round(avg["narration"], 3),
                                  "dropped": drop})
            if not drop:
                out[field].append(e)
    return out, stats


def cmd_draft(args, payload=None):
    """`shed.py draft`: fill the mechanical draft's three empty lists from a local model, keeping
    only what the evidence gate passes. Always exits 0 — the seat is optional, the record is not:
    no model, no draft, a down endpoint or a non-JSON reply all leave the draft byte-identical."""
    root = args.root
    sid8 = sid8_from(args.sid8, payload)
    transcript = args.transcript or (payload or {}).get("transcript_path")
    spec = args.model or os.environ.get("SHED_MODEL")
    if not spec:
        return 0
    model, url = seat_spec(spec)
    draft_path = os.path.join(paths(root)["drafts"], sid8 + ".json")
    rec = load_json(draft_path)
    if not isinstance(rec, dict):
        print("draft seat: no draft for {} — run `shed.py handoff` first".format(sid8))
        return 0
    tr = read_transcript(transcript or rec.get("transcript_path"))
    if not tr["turn_list"]:
        print("draft seat: no transcript turns for {} — seat silent".format(sid8))
        metric(root, "draft_seat", sid8=sid8, model=model, proposed=None, kept=None, rejected=None,
               silent="no transcript")
        return 0

    user = "Session topic: {}\nCommits this session:\n{}\n\nTranscript (turn number in brackets):\n\n{}".format(
        rec.get("session_topic") or "(none)",
        "\n".join("- " + c for c in rec.get("progress") or []) or "(none)",
        seat_transcript(tr["turn_list"], args.budget))
    reply, why = ask_seat(url, model, SEAT_SYSTEM, user, args.timeout)
    obj = parse_seat(reply) if why is None else None
    if obj is None:
        why = why or "reply is not one JSON object"
        print("draft seat: {} — seat silent, draft untouched".format(why))
        metric(root, "draft_seat", sid8=sid8, model=model, proposed=None, kept=None, rejected=None, silent=why)
        return 0

    proposals = seat_entries(obj)
    kept = {f: [] for f in GATED_FIELDS}
    proposed = rejected = 0
    reasons = []
    for field in GATED_FIELDS:
        seen = set()
        for e in proposals[field]:
            proposed += 1
            problems = evidence_gate({field: [e]}, tr["turn_list"], root)
            if not problems:
                problems = check_shape(e, schema(SEAT_SCHEMA[field]))
            claim = _claim_text(e)
            if problems or claim in seen:
                rejected += 1
                reasons.extend(problems or ["{}: duplicate claim".format(field)])
                continue
            seen.add(claim)
            kept[field].append(e)

    judges = judge_specs(getattr(args, "judge", None) or os.environ.get("SHED_JUDGE"))
    jstats = None
    if judges:
        kept, jstats = judge_kept(kept, judges, getattr(args, "judge_tau", JUDGE_TAU),
                                  getattr(args, "judge_budget", JUDGE_BUDGET))

    # Replace the model's lists (depth one from primary: the seat reads the transcript, never the
    # last seat's prose); what a person wrote and what was carried from an earlier archive stay.
    for field in GATED_FIELDS:
        standing = [e for e in rec.get(field) or []
                    if isinstance(e, dict) and (e.get("tier") == "authored" or e.get("origin_sid8"))]
        rec[field] = standing + kept[field]
    write_json(draft_path, rec)
    n_kept = sum(len(v) for v in kept.values())
    extra = {}
    if jstats is not None:
        # `kept` stays the gate's count (what the seat could ground); `dropped` is the judge's cut.
        n_kept += jstats["dropped"]
        extra = dict(judge=[n for n, _ in judges], **jstats)
    metric(root, "draft_seat", sid8=sid8, model=model, proposed=proposed, kept=n_kept, rejected=rejected, **extra)
    print("draft seat: {} proposed, {} kept, {} rejected ({})".format(proposed, n_kept, rejected, model))
    if jstats is not None:
        print("relevance judge ({}): {} judged, {} dropped as narration, {} unjudged, {} silent".format(
            "+".join(n for n, _ in judges), jstats["judged"], jstats["dropped"], jstats["unjudged"],
            jstats["judge_silent"]))
    for r in reasons:
        print("  - " + r)
    return 0


def run_seat(args, payload=None):
    """The hook's way in: SHED_MODEL names the model, SHED_MODEL_TIMEOUT bounds it, and nothing
    the seat does — including raising — can fail the handoff it runs inside."""
    args.model = None
    args.judge = None  # SHED_JUDGE, read in cmd_draft
    args.budget = SEAT_BUDGET
    args.judge_tau = JUDGE_TAU
    try:
        args.judge_budget = float(os.environ.get("SHED_JUDGE_BUDGET") or JUDGE_BUDGET)
    except ValueError:
        args.judge_budget = JUDGE_BUDGET
    try:
        args.timeout = float(os.environ.get("SHED_MODEL_TIMEOUT") or SEAT_TIMEOUT)
    except ValueError:
        args.timeout = SEAT_TIMEOUT
    try:
        cmd_draft(args, payload)
    except Exception as e:  # noqa: BLE001 — the seat is optional, the record is not
        metric(args.root, "draft_seat", sid8=sid8_from(args.sid8, payload), model=os.environ.get("SHED_MODEL"),
               proposed=None, kept=None, rejected=None, silent="error: {}".format(e))


def cmd_verify(args, payload=None):
    """`shed.py verify <shared archive>`: check the private record on this machine against the
    commitment in the shared one. Exit 0 = matches, 1 = mismatch, 2 = no private record here
    (a clone, or a session recorded before the private layer) — silence, not a verdict."""
    root = args.root
    shared = load_json(os.path.join(root, args.archive) if not os.path.isabs(args.archive) else args.archive)
    if not isinstance(shared, dict):
        print("not a shed archive: {}".format(args.archive))
        return 2
    sid8 = shared.get("sid8") or os.path.basename(args.archive)[:-5].rsplit("__", 1)[-1]
    private = load_json(os.path.join(paths(root)["private"], sid8 + ".json"))
    if not isinstance(private, dict):
        print("no private record for {} on this machine (clone, or pre-private-layer) — cannot verify".format(sid8))
        return 2
    if not shared.get("private_sha256"):
        print("shared archive for {} carries no commitment (pre-private-layer)".format(sid8))
        return 2
    if verify_share(shared, private):
        print("ok   {}: private record matches commitment {}".format(sid8, shared["private_sha256"][:12]))
        return 0
    print("MISMATCH {}: private record does not hash to the shared commitment ({} != {})".format(
        sid8, private_digest(private)[:12], shared["private_sha256"][:12]))
    return 1


# ---------------------------------------------------------------- woosh

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def fold(value, indent="  "):
    """Wrap for a folded block so parse_frontmatter's space-join gives the value back exactly: never
    split a word or break at a hyphen ("foo-" + "bar" would rejoin as "foo- bar")."""
    return textwrap.fill(value, 96, initial_indent=indent, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False)


YAML_RETYPED = re.compile(r"(?i)^(~|null|true|false|yes|no|on|off|y|n|[-+.]?\d.*|\.inf|\.nan)$")


def fm_scalar(key, value, wrap=True):
    """One frontmatter line. A value YAML would misread (a quote, a colon-space, a leading marker)
    goes in a folded block, which needs no escaping and which parse_frontmatter joins back. wrap=False
    keeps a shell command on one line with its spacing intact."""
    value = " ".join(str(value).split()) if wrap else str(value).strip()
    if not re.search(r"[\"'#\\]|: |:$|^[-?:,\[\]{}&*!|>%@`]", value):
        if YAML_RETYPED.match(value):  # shed's reader strips the quotes; a YAML reader keeps it a string
            return '{}: "{}"\n'.format(key, value)
        return "{}: {}\n".format(key, value)
    return "{}: >\n{}\n".format(key, fold(value) if wrap else "  " + value)


def cmd_woosh(args, payload=None):
    """`shed.py woosh <slug> "<title>" --done-when "<text>"` — write handoff/brief/<slug>.md: a
    standing suggestion for a future session. Suggestion, never directive: the receiver accepts,
    reshapes or declines. `done_when` is required — a brief with no retirement condition is a
    claim on attention with no end, and orient will not show one. Refuses a slug that exists: two
    sessions inventing the same path is the collision worktrees do not stop."""
    root = args.root
    slug = args.slug[:-3] if args.slug.endswith(".md") else args.slug
    if not SLUG_RE.match(slug):
        print("woosh: slug must be lowercase letters, digits, '-' or '_' (and not start with '_'): {!r}".format(slug))
        return 2
    title, done_when = " ".join(args.title.split()), " ".join(args.done_when.split())
    if not title or not done_when:
        print("woosh: title and --done-when must both be non-empty")
        return 2
    for flag, val in (("--expires", args.expires),):
        if val and not DATE_RE.match(val):
            print("woosh: {} must be YYYY-MM-DD, got {!r}".format(flag, val))
            return 2
    if args.done_check and len(args.done_check.strip().splitlines()) != 1:
        print("woosh: --done-check is one shell command on one line")
        return 2
    if args.done_check and not args.expires:
        print("woosh: --done-check needs --expires YYYY-MM-DD — a predicate with no dated backstop can never auto-close")
        return 2
    path = os.path.join(paths(root)["briefs"], slug + ".md")
    if os.path.exists(path):
        print("woosh: {} already exists — edit it, or pick another slug".format(os.path.relpath(path, root)))
        return 1
    if args.body == "-":
        body = sys.stdin.read()
    elif args.body:
        try:
            with open(args.body, encoding="utf-8") as f:
                body = f.read()
        except OSError as e:
            print("woosh: cannot read --body {}: {}".format(args.body, e))
            return 2
    else:
        body = ("**Suggestion, never directive.** A session that picks this up may accept it, reshape it, "
                "or decline it.\n\n## Why\n\n\n## Shape\n\n")
    now = utcnow()
    fm = "---\n" + fm_scalar("title", title) + "status: open\n" + "created: {}\n".format(now.date().isoformat())
    sid8 = sid8_from(args.sid8, payload)
    if sid8 != "nosid":
        fm += "sid8: {}\n".format(sid8)
    if args.interpreted_by:
        fm += fm_scalar("interpreted_by", args.interpreted_by)
    if args.topic:
        fm += fm_scalar("topic", args.topic)
    if args.global_:
        fm += "global: true\n"
    fm += "done_when: >\n{}\n".format(fold(done_when))
    if args.done_check:
        fm += fm_scalar("done_check", args.done_check, wrap=False)
    if args.expires:
        fm += "expires: {}\n".format(args.expires)
    text = fm + "---\n\n# {}\n\n{}".format(title, body if body.endswith("\n") else body + "\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)
    # parse-verify: what orient will read back is what was asked for
    back, _ = parse_frontmatter(open(path, encoding="utf-8").read())
    want = {"title": title, "status": "open", "done_when": done_when}
    if args.done_check:
        want["done_check"] = args.done_check.strip()
    bad = [k for k, v in want.items() if back.get(k) != v]
    if bad:
        os.remove(path)  # ours, made a moment ago: a brief that misreads must not reach orient or --run-checks
        print("woosh: refused — {} would not read back as written ({}); nothing was kept".format(
            os.path.relpath(path, root), ", ".join(bad)))
        return 1
    metric(root, "woosh", sid8=sid8, slug=slug, armed=bool(args.done_check), dated=bool(args.expires))
    print("wooshed -> {}".format(os.path.relpath(path, root)))
    print("  done_when: " + done_when)
    if args.done_check:
        print("  done_check: {}  (expires {})".format(back["done_check"], args.expires))
    print("  decays {}d after its last body edit{}".format(DECAY_DAYS, ", or on {}".format(args.expires) if args.expires else ""))
    return 0


# ---------------------------------------------------------------- sweep

def flip_brief(path, to, note, sid8=None):
    """Rewrite ONLY the top-level status line inside the first fence and append a dated lifecycle note
    to the body. Everything else stays byte-for-byte — line endings, the blank line after the fence,
    an indented `status:` inside a folded block. The new text is parse-verified BEFORE it is written,
    so a failed flip leaves the file as it was. Returns (old_status, error) — error None on success."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    m = FM_RE.match(text)
    if not m:
        return None, "no frontmatter fence"
    before = parse_frontmatter(text)[0]
    old = before.get("status", "open")
    if old == to:
        return old, None
    nl = "\r\n" if "\r\n" in text else "\n"
    a, b = m.span(1)
    head, n = re.subn(r"(?m)^status[ \t]*:[^\r\n]*", "status: " + to, text[a:b], count=1)
    if not n:
        head = "status: " + to + nl + head
    out = text[:a] + head + text[b:]
    if not out.endswith(("\n", "\r")):
        out += nl
    out += "{nl}_Lifecycle ({}{}): status {} → {}.{}_{nl}".format(
        utcnow().date().isoformat(), ", sid8 " + sid8 if sid8 and sid8 != "nosid" else "", old, to,
        " " + note if note else "", nl=nl)
    after = parse_frontmatter(out)[0]
    if after.get("status") != to or {k: v for k, v in after.items() if k != "status"} != {k: v for k, v in before.items() if k != "status"}:
        return old, "the flip would change more than status — file left as it was"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    os.replace(tmp, path)
    return old, None


def cmd_sweep(args, payload=None):
    """`shed.py sweep [--apply] [--run-checks] | --flip <slug> --to <status>` — the decay vent.

    Lists every open brief that orient has stopped showing: no body edit in DECAY_DAYS, or past the
    author's own `expires`. DRY-RUN unless --apply, which flips those to `declined` with an
    `expired-unclaimed` note. `declined` here means AGED OUT, never judged: hololoom_mcp walked 8
    stale-open briefs (2026-07-05) and 4 were finished work nobody flipped. Two holds are surfaced and
    never auto-flipped: `global` (load-bearing; a quiet one is a thing to read) and `done_check`
    (its disposition may be `consumed`). --run-checks runs each open brief's done_check — explicitly,
    because a sweep must not run shell from repo files as a side effect — and names the ones that
    hold as ready to flip; it flips nothing. The hooks never sweep."""
    root = args.root
    sid8 = sid8_from(args.sid8, payload)
    if args.flip:
        if not args.to:
            print("sweep: --flip needs --to {}".format("|".join(BRIEF_STATUSES)))
            return 2
        slug = os.path.basename(args.flip)
        path = os.path.join(paths(root)["briefs"], slug if slug.endswith(".md") else slug + ".md")
        if not os.path.isfile(path):
            print("sweep: no brief {}".format(os.path.relpath(path, root)))
            return 2
        old, err = flip_brief(path, args.to, args.note, sid8)
        if err:
            print("sweep: {}: {}".format(os.path.relpath(path, root), err))
            return 1
        if old == args.to:
            print("{} already {} — no-op".format(os.path.relpath(path, root), args.to))
            return 0
        metric(root, "sweep_flip", sid8=sid8, slug=slug[:-3] if slug.endswith(".md") else slug, to=args.to)
        print("flipped {}: {} → {}".format(os.path.relpath(path, root), old, args.to))
        return 0

    now = utcnow()
    briefs = read_briefs(root)
    clock = body_clock(root, args.idle_days)
    due, held, live, no_done = [], [], 0, []
    for f, fm, _ in briefs:
        if fm.get("status", "open") != "open":
            continue
        rel = os.path.relpath(f, root)
        if not fm.get("done_when"):
            no_done.append(rel)
            continue
        why = brief_age(f, fm, clock, now, args.idle_days)
        if brief_expired(fm, now):
            why = "expires {} passed".format(fm["expires"][:10])
        if not why:
            live += 1
            continue
        hold = "global" if str(fm.get("global", "")).lower() in ("true", "yes", "1") else (
            "done_check" if fm.get("done_check") else "")
        (held if hold else due).append((rel, fm, why, hold))
    print("# sweep — {} open: {} live, {} due to decay, {} held{}".format(
        live + len(due) + len(held), live, len(due), len(held), "" if args.apply else " (dry run)"))
    for rel, fm, why, _ in due:
        print("  due   {}  — {}".format(rel, why))
    for rel, fm, why, hold in held:
        print("  held  {}  — {} [{}: dispose by hand]".format(rel, why, hold))
    if no_done:
        print("  {} open brief{} without done_when (orient never shows these): {}".format(
            len(no_done), "" if len(no_done) == 1 else "s", ", ".join(no_done)))

    ready = []
    if args.run_checks:
        for f, fm, _ in briefs:
            if fm.get("status", "open") != "open" or not fm.get("done_check"):
                continue
            verdict, rc = run_discharge({"cmd": fm["done_check"]}, root)
            rel = os.path.relpath(f, root)
            print("  check {}  — {} (rc {})".format(rel, verdict, rc))
            if verdict == "MET":
                ready.append(rel)
        for rel in ready:
            print("  ready: shed.py sweep --flip {} --to consumed".format(os.path.basename(rel)[:-3]))

    if not args.apply:
        if due:
            print("Nothing written. `shed.py sweep --apply` declines the {} due; reversible with --flip <slug> --to open.".format(len(due)))
        metric(root, "sweep", sid8=sid8, live=live, due=len(due), held=len(held), applied=0, ready=len(ready))
        return 0
    failed = 0
    for rel, fm, why, _ in due:
        note = ("expired-unclaimed — {} (shed sweep, {}d vent). AGED OUT, not judged: it may be finished work "
                "nobody flipped. Reversible: shed.py sweep --flip {} --to open.").format(
                    why, args.idle_days, os.path.basename(rel)[:-3])
        _, err = flip_brief(os.path.join(root, rel), "declined", note, sid8)
        if err:
            failed += 1
            print("  FAILED {}: {}".format(rel, err))
    print("vent: {} declined, {} failed, {} held".format(len(due) - failed, failed, len(held)))
    metric(root, "sweep", sid8=sid8, live=live, due=len(due), held=len(held), applied=len(due) - failed, ready=len(ready))
    return 1 if failed else 0


# ---------------------------------------------------------------- proof

# A commit message that asserts a QUANTITATIVE result must show a proof block (checked: / cmd: /
# evidence:) or say, auditably, why not. Form only — it never judges whether the evidence is real,
# and the cheapest way past it is to stop claiming, which nothing measures. Ported from
# hololoom_mcp/tools/proofblock_gate.py + proof_block_sample.py; the regexes are theirs verbatim,
# and their comments carry the measurements (the quantitative subset runs ~5% false positives; the
# broad claim matcher ran 43% and is deliberately not used).
QUANT_RE = re.compile(r"(\b\d+/\d+\b|\bexits? \d|\bALL PASS\b|recall@|\brc=\d|\b\d+ passed\b)", re.I)
QUANT_FALSE_FRIEND_RE = re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|/\d+/|\bv?\d+\.\d+/|\b\d+/\d+(?:/\d+)+\b)")
_K = r"[`*_\"']*(?:\s*\([^)]{0,120}\))?[`*_\"']*\s*[:=]"
CHECKED_RE = re.compile(r"\bchecked" + _K, re.I)
CMD_RE = re.compile(r"\bcmd" + _K + r"|\bcommand" + _K + r"|^\s*\$ ", re.I | re.M)
EVIDENCE_RE = re.compile(r"\bevidence" + _K + r"|\boutput" + _K, re.I)
# `Proof-Block: none (<reason>)` — the reason may wrap, but its closing paren ends its line, so prose
# that merely quotes the form does not match. `Proof-Block: <ref>` — the block lives in an artifact.
EXEMPT_RE = re.compile(r"^[^\S\n]*Proof-Block:[^\S\n]*none[^\S\n]*\([^)]+\)[^\S\n]*$", re.I | re.M)
POINTER_RE = re.compile(r"^[^\S\n]*Proof-Block:[^\S\n]*(?!none\b)\S+.*$", re.I | re.M)
PROOF_CLASSES = ("merge", "no-claim", "with-block", "exempt", "pointer", "blocked")


def quant_claims(body):
    hits = []
    for raw in re.split(r"\n|(?<=[.!?])\s+", body):
        s = raw.strip()
        if len(s) >= 8 and QUANT_RE.search(QUANT_FALSE_FRIEND_RE.sub(" ", s)):
            hits.append(s[:160])
    return hits


def classify_message(body):
    """(class, ok, reason, first_claim). A SHOWN block outranks every declaration: a commit that
    did the work is never filed as an opt-out (hololoom_mcp ruling, 2026-08-30)."""
    claims = quant_claims(body)
    if not claims:
        return "no-claim", True, "no quantitative claim", ""
    missing = [n for n, rx in (("checked:", CHECKED_RE), ("cmd:", CMD_RE), ("evidence:", EVIDENCE_RE)) if not rx.search(body)]
    if not missing:
        return "with-block", True, "proof block present", ""
    if EXEMPT_RE.search(body):
        return "exempt", True, "Proof-Block: none (...)", ""
    if POINTER_RE.search(body):
        return "pointer", True, "Proof-Block: <ref>", ""
    return "blocked", False, "missing " + ", ".join(missing), claims[0]


def kept_message(raw, comment_char=None):
    """The part of a commit-msg hook's file git will store. The hook runs BEFORE git's cleanup, and
    the cleanup depends on how the commit was made: an editor session strips comment lines and
    everything under the scissors line; `-m` / `-F` keep comment lines. The editor template is the
    tell — strip only when it is there, so `#41: 12/12 green` from `-m` is judged as git will store
    it, the same way `proof --range` will judge it later. The comment char is git's own
    (core.commentChar; `auto` or unset reads it off the scissors line, else `#`)."""
    cc = comment_char if comment_char and comment_char != "auto" else None
    m = re.search(r"(?m)^(\S+) -{24} >8 -{24}[ \t]*$", raw)
    if m:
        cc = cc or m.group(1)
        raw = raw[:m.start()]
    cc = cc or "#"
    template = m or re.search(r"(?m)^" + re.escape(cc) + r" (Please enter the commit message|Lines starting with)", raw)
    if template:
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith(cc))
    return raw


def cmd_proof(args, payload=None):
    """`shed.py proof [--range A..B] [--message FILE] [--tally]` — rc 1 when a commit (or the message
    a commit-msg hook hands over) claims a quantitative result with no proof block and no
    `Proof-Block:` trailer. --tally counts the classes and always exits 0: the tripwire is the RATIO
    of declarations to shown blocks — a gate that collects `none (...)` lines is measuring nothing."""
    root = args.root
    if args.message:
        try:
            with open(args.message, encoding="utf-8") as f:
                body = f.read()
        except OSError as e:
            print("proof: cannot read {}: {}".format(args.message, e))
            return 2
        body = kept_message(body, git(root, "config", "core.commentChar"))
        cls, ok, reason, claim = classify_message(body)
        if not ok:
            print("proof: this message claims a quantitative result with no proof block ({}).".format(reason))
            print("  claim: " + claim)
            print("  add `checked: … / cmd: <exact command> / evidence: <output you read>`, or a trailer:")
            print("    Proof-Block: none (<why no block>)      or      Proof-Block: <path#section>")
            return 1
        return 0
    note = ""
    if args.range:
        spec = args.range.split()
    else:
        trunk = trunk_ref(root)
        if trunk:
            spec = [trunk + "..HEAD"]
        else:
            spec, note = ["-1", "HEAD"], "note: no origin/main — checking HEAD only"
    out = git(root, "log", "--format=%H", *spec)
    if out is None:
        print("proof: git log failed for {} — instrument silent, not a verdict".format(" ".join(spec)))
        return 2
    shas = [s for s in out.splitlines() if s.strip()]
    if note:
        print(note)
    counts = dict((c, 0) for c in PROOF_CLASSES)
    blocked = []
    for sha in shas:
        meta = git(root, "show", "-s", "--format=%P%x00%B", sha)
        if meta is None:
            print("proof: cannot read {} — instrument silent".format(sha[:12]))
            return 2
        parents, _, body = meta.partition("\x00")
        if len(parents.split()) > 1:
            cls, ok, reason, claim = "merge", True, "", ""  # the branch commits carry the claim; gate those
        else:
            cls, ok, reason, claim = classify_message(body)
        counts[cls] += 1
        if not ok:
            blocked.append((sha, body.splitlines()[0] if body else "", reason, claim))
    rng = " ".join(spec)
    if args.tally:
        shown = counts["with-block"]
        declared = counts["exempt"] + counts["pointer"]
        print("proof --tally {}: {} commits — {}".format(rng, len(shas), ", ".join("{} {}".format(v, k) for k, v in counts.items() if v)))
        if not shas:
            head = git(root, "rev-parse", "--short", "HEAD")
            print("  CONTROL: HEAD={} (an empty range is not a clean one)".format(head or "<git silent too>"))
        elif declared and declared >= shown:
            print("  TRIPWIRE: declarations ({}) >= shown blocks ({}) — the gate is collecting opt-outs; hand-read them".format(declared, shown))
        return 0
    metric(root, "proof", range=rng, commits=len(shas), blocked=len(blocked))
    for sha, subj, reason, claim in blocked:
        print("BLOCKED {} {}\n  {}; claim: {}".format(sha[:12], subj[:80], reason, claim))
    if blocked:
        print("{} of {} commits in {} claim a number with no proof block".format(len(blocked), len(shas), rng))
        return 1
    print("proof: {} commits in {}, none blocked".format(len(shas), rng))
    return 0


PROOF_HOOK_MARK = "# shed: proof"


def install_proof_hook(root, me_rel):
    """Write a git commit-msg hook that runs `proof --message`. Opt-in (`install --proof-hook`):
    .git/hooks is per clone and not ours to touch unasked. Refuses to replace a hook it did not
    write. Fails OPEN when python3 is missing — a gate that blocks every commit for want of an
    interpreter teaches people to delete it — and when shed.py is absent from this checkout (a branch
    from before it was committed), for the same reason."""
    # --git-path already honours core.hooksPath, `~` included; resolving it by hand wrote hooks git never ran
    rel = git(root, "rev-parse", "--git-path", "hooks")
    if rel is None:
        return "not a git repository — no commit-msg hook written"
    hooks_dir = rel if os.path.isabs(rel) else os.path.join(root, rel)
    path = os.path.join(hooks_dir, "commit-msg")
    if os.path.exists(path):
        if PROOF_HOOK_MARK in open(path, encoding="utf-8", errors="replace").read():
            return "commit-msg hook already installed"
        return "commit-msg hook exists and is not shed's — left alone; add this line to it: {}".format(
            'python3 {} proof --message "$1" || exit 1'.format(me_rel))
    os.makedirs(hooks_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n{} — a commit that claims a number shows how it was read (shed.py proof)\n"
                'command -v python3 >/dev/null 2>&1 || {{ echo "shed proof: no python3, not checked" >&2; exit 0; }}\n'
                '[ -f {ref} ] || {{ echo "shed proof: "{ref}" not in this checkout, not checked" >&2; exit 0; }}\n'
                'exec python3 {ref} proof --message "$1"\n'.format(PROOF_HOOK_MARK, ref=me_rel))
    os.chmod(path, 0o755)
    return "commit-msg hook: {}".format(os.path.relpath(path, root))


# ---------------------------------------------------------------- install

HOOK_EVENTS ={"SessionStart": "session-start", "Stop": "stop", "SessionEnd": "session-end"}


def cmd_install(args, payload=None):
    root = os.path.abspath(args.root)
    me = os.path.abspath(__file__)
    # If shed.py lives in the same git checkout as the target, address it relative to
    # $CLAUDE_PROJECT_DIR so the hook survives worktrees, clones, and other machines.
    top_root = git(root, "rev-parse", "--show-toplevel")
    top_me = git(os.path.dirname(me), "rev-parse", "--show-toplevel")
    if top_root and top_me and os.path.realpath(top_root) == os.path.realpath(top_me):
        me_ref = '"$CLAUDE_PROJECT_DIR/{}"'.format(os.path.relpath(me, root))
    else:
        me_ref = me
    settings_path = os.path.join(root, ".claude", "settings.json")
    settings = load_json(settings_path) or {}
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, verb in HOOK_EVENTS.items():
        # A repo shed installs into carries no protocol prose, so its page must teach declare itself; a repo
        # that wires the hook by hand (hololoom_mcp has its own declare verb) leaves the flag off.
        cmd = "python3 {} hook {}{}".format(me_ref, verb, " --declare-hint" if verb == "session-start" else "")
        groups = hooks.setdefault(event, [])
        # The same-checkout form quotes the path (`shed.py" hook stop`), so match around the quote —
        # a substring test on `shed.py hook` duplicated all three hooks on every re-run (2026-09-25).
        present = any(re.search(r'shed\.py"?\s+hook\s+' + re.escape(verb) + r'\b', h.get("command", ""))
                      for g in groups if isinstance(g, dict) for h in g.get("hooks", []) if isinstance(h, dict))
        if present:
            continue
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 30}]})
        added.append(event)
    write_json(settings_path, settings)
    for d in ("sessions", "drafts", "private"):
        os.makedirs(paths(root)[d], exist_ok=True)
    # Drafts and metrics are per-machine noise; the private layer is the human's own words and
    # never leaves this machine; only the finalized shared archive is worth sharing.
    gi = os.path.join(root, ".gitignore")
    try:
        existing = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
        wanted = [l for l in ("handoff/drafts/", "handoff/metrics.jsonl", "handoff/private/")
                  if l not in existing.splitlines()]
        if wanted:
            with open(gi, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("# shed: drafts, local metrics and the private layer never merge; the shared archive does\n")
                f.write("\n".join(wanted) + "\n")
    except OSError:
        pass
    print("hooks: {} (settings: {})".format(", ".join(added) if added else "already installed",
                                            os.path.relpath(settings_path, root)))
    if getattr(args, "proof_hook", False):
        # git runs a hook from the worktree root, so a same-checkout shed.py is addressed relative to it
        # (every worktree of the clone shares the one hooks dir); otherwise by absolute path.
        if top_root and top_me and os.path.realpath(top_root) == os.path.realpath(top_me):
            hook_ref = '"{}"'.format(os.path.relpath(os.path.realpath(me), os.path.realpath(top_root)))
        else:
            hook_ref = '"{}"'.format(me)
        print(install_proof_hook(root, hook_ref))
    return 0


# ---------------------------------------------------------------- check

def cmd_check(args, payload=None):
    """`shed.py check <schema> <file>` — is this file the named schema's shape? rc 0 = yes, 1 = no
    (problems listed), 2 = could not read. Form only; a platform validator (weft CI) does the rest."""
    obj = load_json(args.file)
    if obj is None:
        print("cannot read {} as JSON".format(args.file))
        return 2
    try:
        sch = schema(args.schema)
    except (OSError, ValueError):
        print("no schema {!r} under {}".format(args.schema, SCHEMA_DIR))
        return 2
    problems = check_shape(obj, sch)
    if problems:
        print("{}: NOT {} ({} problem{}):".format(args.file, args.schema, len(problems), "" if len(problems) == 1 else "s"))
        for pr in problems:
            print("  - " + pr)
        return 1
    print("{}: {} ok".format(args.file, args.schema))
    return 0


# ---------------------------------------------------------------- hook entry

def cmd_hook(args):
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    args.root = os.path.abspath(args.root or payload.get("cwd") or os.getcwd())
    args.sid8 = None
    args.transcript = None
    if args.event == "session-start":
        args.max_lines = MAX_LINES_DEFAULT
        return cmd_orient(args, payload)
    if args.event == "stop":
        if payload.get("stop_hook_active"):
            return 0
        args.finalize = False
        return cmd_handoff(args, payload)
    if args.event == "session-end":
        args.finalize = True
        # The draft seat runs here, once, not on every Stop: a small local model takes 15–20 s a call
        # (witness 2026-09-29), and Stop fires every turn (blake-directed 2026-09-29).
        args.seat = bool(os.environ.get("SHED_MODEL"))
        # No `share` attribute on the hook path, deliberately: split_record reads it with getattr,
        # so the hooks cannot grow a way to share by accident — sharing is a person typing --share.
        return cmd_handoff(args, payload)
    return 2


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="shed.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("orient", help="print the one page")
    o.add_argument("--root", default=".")
    o.add_argument("--max-lines", type=int, default=MAX_LINES_DEFAULT, help="0 = no ceiling")
    o.add_argument("--sid8")
    o.set_defaults(fn=cmd_orient)

    d = sub.add_parser("declare", help="one sentence of what this session means to do, before it touches files")
    d.add_argument("statement")
    d.add_argument("--root", default=".")
    d.add_argument("--sid8")
    d.add_argument("--cmd", help="discharge test: exact shell command whose exit status says the intent was MET")
    d.add_argument("--expect-rc", type=int, default=0)
    d.add_argument("--expect-re", help="regex the command's stdout must also match")
    d.add_argument("--expect-absent", action="store_true", help="invert: the intent holds when the command does NOT match")
    d.add_argument("--expires", help="YYYY-MM-DD dated backstop; required with --cmd")
    d.add_argument("--scope", choices=("repo", "host"), default="repo")
    d.add_argument("--reason", help="why the previous declaration is being replaced (kept under superseded)")
    d.set_defaults(fn=cmd_declare)

    h = sub.add_parser("handoff", help="draft (or finalize) this session's record")
    h.add_argument("--root", default=".")
    h.add_argument("--transcript")
    h.add_argument("--sid8")
    h.add_argument("--finalize", action="store_true")
    h.add_argument("--share", action="append", default=[], metavar="FIELD",
                   help="on finalize, let this private field into the shared archive (repeatable); "
                        "the only way a person's words cross into git")
    h.set_defaults(fn=cmd_handoff)

    s = sub.add_parser("draft", help="the draft seat: a local model fills the three lists; the evidence gate keeps what it can ground")
    s.add_argument("--root", default=".")
    s.add_argument("--transcript")
    s.add_argument("--sid8")
    s.add_argument("--model", help="NAME or NAME@URL of an OpenAI-compatible endpoint (default: $SHED_MODEL; "
                                   "URL default $SHED_ENDPOINT or " + SEAT_ENDPOINT + ")")
    s.add_argument("--budget", type=int, default=SEAT_BUDGET, help="transcript chars sent, from the end")
    s.add_argument("--timeout", type=float, default=SEAT_TIMEOUT)
    s.add_argument("--judge", help="NAME[@URL][,NAME[@URL]] System One judge(s) that may drop narration the gate "
                   "kept; several are averaged (default: $SHED_JUDGE; URL default {})".format(JUDGE_ENDPOINT))
    s.add_argument("--judge-tau", type=float, default=JUDGE_TAU)
    s.add_argument("--judge-budget", type=float, default=JUDGE_BUDGET, help="seconds for all judge calls")
    s.set_defaults(fn=cmd_draft)

    v = sub.add_parser("verify", help="check this machine's private record against a shared archive's commitment")
    v.add_argument("archive", help="path to handoff/sessions/<ended>__<sid8>.json")
    v.add_argument("--root", default=".")
    v.set_defaults(fn=cmd_verify)

    w = sub.add_parser("woosh", help="write handoff/brief/<slug>.md — a standing suggestion for a future session")
    w.add_argument("slug", help="file name under handoff/brief/, lowercase, no .md needed")
    w.add_argument("title", help="one line: what the work is")
    w.add_argument("--done-when", required=True, help="the retirement condition, in words (required: orient hides a brief without one)")
    w.add_argument("--done-check", help="one shell command that exits 0 when the work is already done; needs --expires")
    w.add_argument("--expires", help="YYYY-MM-DD: the author's dated backstop; sweep declines it after this")
    w.add_argument("--topic")
    w.add_argument("--global", dest="global_", action="store_true", help="show on every page, not only topic-matched ones (sparingly)")
    w.add_argument("--interpreted-by", help="model id when a model wrote the brief; omit when a person did")
    w.add_argument("--body", help="FILE with the brief's prose, or - for stdin (default: a short stub)")
    w.add_argument("--root", default=".")
    w.add_argument("--sid8")
    w.set_defaults(fn=cmd_woosh)

    sw = sub.add_parser("sweep", help="the decay vent: list (or --apply) briefs past 30d without a body edit or past expires")
    sw.add_argument("--apply", action="store_true", help="flip the due briefs to declined (dry run otherwise)")
    sw.add_argument("--run-checks", action="store_true", help="run each open brief's done_check and name the ones ready to flip")
    sw.add_argument("--idle-days", type=int, default=DECAY_DAYS)
    sw.add_argument("--flip", metavar="SLUG", help="flip one brief's status by hand (with --to)")
    sw.add_argument("--to", choices=BRIEF_STATUSES)
    sw.add_argument("--note", help="with --flip: why, appended to the brief's lifecycle line")
    sw.add_argument("--root", default=".")
    sw.add_argument("--sid8")
    sw.set_defaults(fn=cmd_sweep)

    pf = sub.add_parser("proof", help="fail on a commit that claims a number with no proof block (form only)")
    pf.add_argument("--range", help="revision range (default: origin/main..HEAD)")
    pf.add_argument("--message", metavar="FILE", help="check one commit message file (the commit-msg hook's $1)")
    pf.add_argument("--tally", action="store_true", help="count the classes over the range; always exit 0")
    pf.add_argument("--root", default=".")
    pf.set_defaults(fn=cmd_proof)

    i = sub.add_parser("install", help="wire Claude Code hooks into <root>/.claude/settings.json")
    i.add_argument("--root", default=".")
    i.add_argument("--proof-hook", action="store_true", help="also write a commit-msg hook that runs `proof --message`")
    i.set_defaults(fn=cmd_install)

    c = sub.add_parser("check", help="is <file> the shape of schemas/<schema>.json? (form only)")
    c.add_argument("schema", help="record | intent | thread | decision | discharge | tier | basis | lease")
    c.add_argument("file")
    c.set_defaults(fn=cmd_check)

    k = sub.add_parser("hook", help="hook entry point; JSON payload on stdin")
    k.add_argument("event", choices=sorted(HOOK_EVENTS.values()))
    k.add_argument("--root")
    k.add_argument("--declare-hint", action="store_true",
                   help="session-start: put the one-line declare instruction on the page (install writes this)")
    k.set_defaults(fn=cmd_hook)

    args = ap.parse_args(argv)
    if args.cmd == "hook":
        return cmd_hook(args)
    if args.cmd == "check":
        return cmd_check(args)
    args.root = os.path.abspath(args.root)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
