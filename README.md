# shed

The coordination protocol over files in git. Six verbs: **orient**,
**declare**, **handoff**, **woosh**, **sweep** and **proof** — orient and
handoff wired into Claude Code as hooks so nobody has to remember them,
declare the one sentence a session types before it touches files, woosh a
standing suggestion for a later session, sweep the decay vent that keeps the
suggestions from piling up, proof the rule that a commit claiming a number
shows how the number was read.

A *shed* is the opening in the warp that the pick passes through: the space a
session works in, made fresh each pass. `shed` holds the state of a project
*between* sessions in plain files, so any worker — a person, a Claude Code
session, another agent — can pick the work up cold without a firm's worth of
people holding that state in their heads.

The claim this is built to test:
a small group plus agents can build large software without a large
organization, if the coordination state lives in a portable protocol instead
of in people. Six verbs make up that protocol — orient, declare, handoff,
woosh, sweep, proof. The first three shipped against one test — **two fresh
sessions in a row pick up a thread from files alone** — which passed on
2026-10-04 in a project that had never seen shed. The other three are ports
of tools that ran for months in the repo shed was extracted from, cut down to
the part that is protocol.

```
shed.py        the CLI and the hook entry points — stdlib only, Python 3.9+
test_shed.py   ~280 checks, no credentials: python3 test_shed.py (the draft-seat
               checks bind 127.0.0.1; a sandbox that forbids it fails them by name)
schemas/       the protocol's file formats — eight JSON Schemas + fixtures
```

## Install into any repo

```sh
git clone https://github.com/blakechasteen/shed ~/.local/share/shed
python3 ~/.local/share/shed/shed.py install --root /path/to/your/repo
```

No dependencies beyond Python 3.9+ and git; update with
`git -C ~/.local/share/shed pull`. Run on macOS and on Omarchy (Arch
Linux) with Claude Code.

That merges three hooks into the repo's `.claude/settings.json` (existing
settings and hooks are preserved; running it twice is a no-op), creates
`handoff/sessions/`, `handoff/drafts/` and `handoff/private/`, and gitignores
the drafts, the metrics and the private layer — only the finalized shared
archive is worth sharing. When `shed.py` lives
in the same checkout as the target, the hook addresses it as
`$CLAUDE_PROJECT_DIR/<relative path>`, so it survives worktrees, clones, and
other machines. If a richer archive for the session already exists at
`SessionEnd` (a `/handoff` skill, a human), shed stands down rather than
writing a second one.

| hook | runs | effect |
|---|---|---|
| `SessionStart` | `shed.py hook session-start` | prints the orient page; Claude Code injects it as context |
| `Stop` | `shed.py hook stop` | refreshes this session's draft record from the transcript + git |
| `SessionEnd` | `shed.py hook session-end` | finalizes the draft into `handoff/sessions/<ended>__<sid8>.json` |

No CLAUDE.md text is required. Instructions get skipped; hooks do not. That
is the point. The one instruction a fresh repo needs — *how* to declare —
rides on the page itself: `install` writes the start hook as `hook
session-start --declare-hint`, which puts a single line under the notice
with the session's sid8 spelled out. A repo that wires the hook by hand and
has its own declare verb leaves the flag off. (First run on a real Omarchy
box, 2026-09-30: without that line no session ever learned the verb existed;
with it, a session asked only to add a flag declared from the page alone.)

## The one page

`shed.py orient` prints, in this order and under a ceiling of 40 lines:

- the repo, branch, sha, and how many files are uncommitted
- **the recorded notice**, always line two: the session is written to
  `handoff/` — private on this machine, shared in git. Consent by disclosure,
  not by choice; an agent that cannot decline being recorded is under
  surveillance, and the notice names that rather than resolving it
- the last finalized session: who, when, its `tldr`, its `open_threads` and
  `watch_outs`
- any *unfinalized* draft from another session (a session that ended without
  its `SessionEnd` hook firing — the hook is best-effort, so this is normal)
- commits since that session
- open briefs, if the repo keeps `handoff/brief/*.md` with `status: open` and
  a `done_when`, created or **body-edited** within 30 days and not past its own
  `expires` — the 8 newest by `created`, with the total and how many decayed,
  so a repo with hundreds still fits on the page. This is the same predicate
  `sweep` vents on: the page and the files cannot disagree about what decayed

The ceiling is enforced, not advisory: the page truncates with a marker and
`--max-lines 0` shows everything. A human reads this page, an agent gets it as
context, a voice can speak it. If all three can use the same page, the
protocol is agent-agnostic by construction.

## Declare: the one field written before the work

```sh
python3 shed.py declare "port the parser to the new tokenizer" \
    --cmd "python3 -m pytest tests/test_parser.py -q" --expires 2026-10-15
```

One sentence of what this session means to do, and — when "done" is
honestly a command — that command as a discharge test. It lands in the
session's draft as `intent` (`schemas/intent.json`), the `Stop` hook carries it,
and the page other sessions see shows it as `declared: …` while the session
is live. Re-declaring keeps the earlier statement under `superseded` with a
`--reason`; nothing is overwritten.

At finalize the declaration is **read back**:

| verdict | what finalize does |
|---|---|
| **MET** — the command holds | a `progress` line: `intent MET: <statement>` |
| **UNMET** — it fails | the statement becomes an `open_thread`, verbatim, `tier: authored`, carrying the declaration's own `done_when`, `expires` and `scope` |
| **UNARMED** — no `--cmd` | same thread; next step says to confirm by hand or re-declare armed |
| **ERROR** — rc 124 / 126 / 127 / 128 | same thread, with `blockers: instrument silent — not a verdict` |

This is forward-intent **without a model in the seat**: the first archive
shed finalized on a real session carried zero open threads, because the
mechanical draft has nothing to fill them with. The verb that was declared
is the thread that is open. A session that declares and does not finish
leaves the next session a page that names exactly what to pick up, with the
command that says when it is done. `--cmd` requires `--expires`: a predicate
with no dated backstop can never auto-close.

## Woosh: a standing suggestion

```sh
python3 shed.py woosh port-parser "Port the parser to the new tokenizer" \
    --done-when "tests/test_parser.py green on the new tokenizer" \
    [--done-check "python3 -m pytest tests/test_parser.py -q" --expires 2026-11-15] \
    [--topic parser] [--global] [--interpreted-by <model-id>] [--body FILE|-]
```

Writes `handoff/brief/<slug>.md`: frontmatter (`title`, `status: open`,
`created`, `sid8`, `done_when`, and whatever was asked for) over a body that
opens **Suggestion, never directive** — the session that picks it up accepts,
reshapes, or declines. A handoff is a snapshot of one session; a brief is a
standing piece of work that outlives any one of them.

- **`done_when` is required.** A brief with no retirement condition is a claim
  on attention with no end; orient will not show one.
- **`--done-check` needs `--expires`**, the same rule as `declare --cmd`: a
  predicate with no dated backstop can never auto-close. `expires` is the
  lease rider from `schemas/lease.json`, written flat into the frontmatter.
- **An existing slug is refused.** Two sessions independently inventing the
  same path is the collision worktrees do not stop; edit the file instead.
- **It reads back what it wrote.** Every value is parsed back through the same
  frontmatter reader orient uses; anything YAML could misread (quotes, `: `)
  goes in a folded block, wrapped so the join gives the exact string back.

No ninth schema: a brief is markdown a person reads, and `schemas/README.md`
caps the formats at eight until one earns its falsifier.

## Sweep: the decay vent

```sh
python3 shed.py sweep                      # dry run: what has decayed, what is held
python3 shed.py sweep --run-checks         # also run each open brief's done_check
python3 shed.py sweep --apply              # flip the due ones to declined
python3 shed.py sweep --flip <slug> --to open|consumed|superseded|declined [--note WHY]
```

Everything prospective decays unless someone engages with it. A brief is
**due** when it has had no **body** edit in 30 days (`--idle-days`), or when
its own `expires` has passed. The clock reads the body only: a frontmatter
pass — a topic retrofit, a date pushed out — does not reset it (in the repo
shed came from, bulk housekeeping touched 148 of 154 open briefs inside 29
days, which kept nearly everything artificially fresh). It reads HEAD and
`origin/main` both, so engagement pushed from another checkout counts, and a
brief being edited right now (uncommitted) is never due. An untracked brief
falls back to its file's mtime.

- **Dry run unless `--apply`.** Apply flips status to `declined` and appends
  a dated `expired-unclaimed` note that says **aged out, not judged**: when
  eight stale-open briefs were checked by hand, four were finished work
  nobody had flipped. It is reversible with `--flip <slug> --to open`.
- **Two holds, never auto-flipped:** `global: true` (load-bearing; a quiet one
  is a thing to read) and anything with a `done_check` (it may be `consumed`).
- **`--run-checks` is explicit.** A sweep must not run shell commands out of
  repo files as a side effect; with the flag it runs each `done_check` and
  names the ones that hold as ready to flip — and flips nothing.
- **The hooks never sweep.** Orient already hides what decayed; sweep makes the
  files say so, and that is a person's (or a session's) call to make.

## Proof: a number shows how it was read

```sh
python3 shed.py proof                      # origin/main..HEAD; rc 1 on an unbacked claim
python3 shed.py proof --range A..B --tally # class counts, always rc 0
python3 shed.py proof --message FILE       # one message — what the commit-msg hook calls
python3 shed.py install --proof-hook       # opt-in: git itself refuses the commit
```

A commit message that asserts a **quantitative** result (`13/13`, `exit 0`,
`ALL PASS`, `rc=0`, `42 passed`, `recall@k`) carries a proof block —
`checked:` / `cmd:` / `evidence:` — or one auditable trailer:

```
Proof-Block: none (<why there is no block>)
Proof-Block: <path#section>          the block lives in an artifact
```

**Form only.** It checks that a block was *shown*, never that the evidence is
real or related, and the cheapest way past it is to delete the number —
nothing measures that. `--tally` is the tripwire for the other failure: when
declarations outnumber shown blocks, the gate is collecting opt-outs and
measuring nothing. Merge commits are skipped (the branch commits carry the
claim). The matchers are carried verbatim from the gate this was ported from,
and over the same 200 commits the two produce identical class counts.

`install --proof-hook` writes `commit-msg` into the clone's hooks directory
(`core.hooksPath` when set). It is opt-in because `.git/hooks` is per clone
and not shed's to touch unasked; it refuses to replace a hook it did not
write (and prints the line to add); it fails open when `python3` is missing,
because a gate that blocks every commit for want of an interpreter gets
deleted.

## The handoff record

The `Stop` hook writes a **mechanical draft** to `handoff/drafts/<sid8>.json`
on every turn: `session_topic` is the first user turn, `tldr` is the first
sentence of the last assistant turn, `progress` is the commits since the
session started, `files_touched` is the union of committed and dirty paths.
`open_threads`, `decisions` and `watch_outs` are carried from the previous
draft and otherwise left empty — **empty means unknown, not none**. Filling
them is the job of a human or a model, and the next slice puts a small local
model in that seat. The `SessionEnd` hook stamps `ended_at` and moves the
draft to `handoff/sessions/`.

The archive shape is schema'd (`schemas/record.json`), so a repo that later
puts heavier tooling over these files keeps its history. A repo that never does
loses nothing: the files are readable as they are.

## The private layer: her words stay on her machine

The archive commits to trunk; trunk mirrors. The first time a person's
sentence shows up somewhere she did not put it, the protocol is over for her.
So the record splits at `finalize`, before any human word is kept:

- **Two records per session.** `handoff/private/<sid8>.json` (gitignored)
  holds `human_first`, `human_last`, `session_topic` (a normalized copy of her
  first sentence — the same words) and the transcript pointer. The shared
  `handoff/sessions/<ended>__<sid8>.json` holds only fields on an explicit
  **allowlist** (`FIELD_SCOPE` in `shed.py`); a key not in that table reaches
  neither file. The tests prove a private field cannot cross.
- **Orient reads private first.** On the machine that recorded it, the page
  shows her words back, above `tldr`; a clone of the repo orients from the
  shared file alone and never sees them.
- **Crossing is explicit.** `shed.py handoff --finalize --share human_last` is
  the only way a private field enters the shared record, and the record says
  so (`shared_fields`). The hooks never share. Later a signed emission goes
  here; for now the flag and the test are the contract.

- **The shared record commits to the private one.** `private_sha256` is the
  hash of the private fields, on the shared file, next to nothing else from
  them. `shed.py verify <archive>` checks this machine's private record
  against it; a `--share`d sentence is checkable by anyone holding the shared
  file. A sealed journal at level zero: hash on the shared side, contents
  off it; the git boundary is the seal.
- **A private record buys nothing.** Orient on a clone must never treat "this
  session has a private file" as signal, and nothing rewards sharing. The
  room with the lock on the inside is only real if it buys you nothing.
- **The hooks cannot share.** The hook path carries no share argument at all,
  so sharing cannot grow there by a later patch; it is a person typing
  `--share`.

The allowlist IS the schema: every field of `schemas/record.json` carries
`x-scope: shared | private`, and `shed.py` derives `FIELD_SCOPE` from that
annotation at import. There is no second table to keep equal.

## The schemas: the protocol's file formats

`schemas/` holds eight JSON Schema (2020-12) files — the contract for what
shed writes and what any reader can rely on (`schemas/README.md`):

| tense | file | carries |
|---|---|---|
| settled | `record`, `decision` | provenance — `tier`, `basis`, later a signature |
| prospective | `intent`, `thread` (open / watch) | a lease — `expires`, `scope`, an optional discharge test |
| atom | `discharge` | `{cmd, expect_rc, expect_re?, expect_absent?}` — `done_when`, `still_true`, the proof block, the evidence gate |
| riders | `tier`, `basis`, `lease` | who stands behind it / how it was read (`read \| empty \| BLIND`) / how long it may claim attention |

Every record field also declares `x-tier` — her sentences `authored`, what
shed computes from git and the transcript `derived`, model prose
`interpreted` — and every record carries a `basis` reading per instrument,
so a transcript that was never on disk is `BLIND` and never reads as a
silent session. `shed.py check <schema> <file>` is the form check
(`check_shape`, ~40 lines of stdlib over the subset of JSON Schema these
files use); `finalize` runs it on the shared record and fails closed. Full
validation is the job of whatever platform reads these files. Fixtures
under `schemas/fixtures/` are loaded by the tests.

## Grounded: drift refused where it can be, measured where it can't

The moment a model fills the record, drift is the failure mode — each
summary written from the last one smooths toward the model's prior until an
invented framing reads as fact. shed refuses that mechanically, with no
judgment call anywhere in the path:

- **Depth one from primary.** The draft is rebuilt from the transcript and
  git on every `Stop`; it never reads the previous draft's prose.
- **The human's words never regenerate.** `human_first` and `human_last` are
  the human's first and last sentences, byte-for-byte, kept in the private
  layer, and orient prints them *before* `tldr` on the machine that has them.
  The synthesis is framed by her words, not the reverse.
- **Every filled entry is tiered and grounded.** An `open_thread`, `decision`
  or `watch_out` is an object with `tier: authored | derived | interpreted`
  (`schemas/tier.json`), so an entry mirrored anywhere else keeps saying who
  stands behind it. `authored`
  (an identified party stands behind it) needs nothing. The other two must
  carry `evidence: {turn}` naming a transcript turn (index or uuid), and the
  entry's **claim text itself** — `thread` on an open thread, `text` on a
  watch-out or decision — must be a verbatim span of that turn. The
  paraphrase, the why, the model's reading go in `gloss`, which is never
  gated and always rendered as the entry's tier. `finalize` rejects anything
  else, names the entry, exits non-zero, and leaves the draft on disk. A bare
  string is unknown-author and is rejected; so is a tier-less entry — absence
  is unstated, and unstated never renders as authored. Why the claim and not
  an attached quote: a gate that checks "some quote is in some turn" binds
  nothing — a fabricated thread beside a real, unrelated four-letter quote
  passed the earlier form (adversarial pass, 2026-09-25). Grounding the
  claim makes the fabrication unwritable: the sentence has to exist in the
  source. What is *not* gated, said plainly: `next_step`, `blockers` and
  `gloss` are prose, and `tier: authored` is self-declared until identity
  keys exist — a writer that lies about who it is gets past any gate.
- **Carry by copying, never by restating.** An entry carried from an earlier
  session names `origin_sid8` and must be byte-identical to an entry in that
  archive; a restated copy is rejected. Orient marks carried and interpreted
  entries (`[interpreted, from <sid8>]`) so a reader never mistakes model
  prose for the human's.
- **Extractiveness, one number per handoff.** The share of the record's
  **ungated** prose word 3-grams — `tldr`, and each interpreted entry's
  `gloss` / `next_step` / `blockers` — found in the transcript and commit
  subjects, written into the archive and `metrics.jsonl`. The gated claim
  text is verbatim by construction and would score 1.0 whatever the model
  did, so it is left out. A mechanical draft scores 1.0 because its `tldr`
  is a copied sentence; the number starts to mean something the moment a
  model writes the prose, and falling across sessions is the drift alarm.

What this does not fix: a record faithful to its source and still wrong about
*what mattered*. That stays with the human.

## It measures itself

Every orient appends a line to `handoff/metrics.jsonl` with the page's line
and character count and a token estimate; every finalized handoff appends the
commit count, turn count, and seconds from session start to first commit.
Those are the two coordination-cost numbers the arc says to watch — tokens to
orient, and cold-start to first useful commit. They are produced wherever the
tool is installed, so the claim carries its own falsifier.

Whose commits: on a session branch, `progress`, `files_touched` and
`first_commit_seconds` are the branch's own commits (`origin/main..HEAD`,
inside the session's window) under the project root (`-- .`), so a peer's
commit on trunk in the same minute and a monorepo sibling's files never land
in a session's record. On the trunk itself there is no branch to scope by:
it stays a time window, and the record's `basis[git]` says so in words —
"commits by ANY session in it". Read the basis before reading the number.

## What is deliberately not here

Re-running proof blocks to check the evidence is sound, a queue for
judgment calls, and sweep signals that need a code forge or a model stay
out: they are not file formats plus discipline. Semantic recall, graphs,
vector indexes, presence, Matrix: those are a *platform*, opt-in
accelerators over these files, and they do not belong in the protocol. Signed records are next: whose handoff
it is becomes a question the moment a second party arrives.

## Verify

```sh
python3 test_shed.py                      # ~280 checks
python3 shed.py orient --root /path/to/your/repo   # the page, ~0.25s
python3 shed.py sweep --root /path/to/your/repo    # dry run: what decayed, what is held
python3 shed.py check record handoff/sessions/<ended>__<sid8>.json   # form check
```
