# schemas/ — the protocol's file formats

Eight JSON Schema (draft 2020-12) files: the contract for what shed writes
and what any reader of a shed record — a person, a session, weft — can rely
on. Flat where weft's codegen is flat. Fixtures under `fixtures/` are the
worked examples; `test_shed.py` loads every one against its schema and
rejects mutated copies.

## Tense is the top-level split

| tense | artifacts | carries | mutability |
|---|---|---|---|
| **settled** (past) | `record`, `decision` | provenance — tier, basis, later a signature | immutable |
| **prospective** (future) | `intent`, `thread` (open / watch) | a lease — `expires`, `scope`, optional discharge test | replaceable, expires by silence |

"Suggest, never direct" becomes checkable: a directive is a prospective
artifact with no lease.

## The files

| file | kind | one line |
|---|---|---|
| `record.json` | noun | one session's handoff; every field carries `x-scope` and `x-tier` |
| `intent.json` | noun | what the session declared before it touched files |
| `thread.json` | noun | one open thread or watch-out; grounded, leased, carried by copy |
| `decision.json` | noun | one settled choice; grounded, never leased |
| `discharge.json` | atom | `{cmd, expect_rc, expect_re?, expect_absent?}` — appears as `done_when`, `still_true`, the proof block, the evidence gate |
| `tier.json` | rider | `authored \| derived \| interpreted` — weft's `ai.mythrl.tier` enum verbatim |
| `basis.json` | rider | one reading of one instrument: `read \| empty \| BLIND` |
| `lease.json` | rider | `expires` + `scope`; the discharge rides under the carrier's own key |

Eight is the ceiling this slice accepts (the dissipative-structure pin: every
schema is order that costs attention). A ninth needs a falsifier of its own.

## Annotations that carry behaviour

- **`x-scope: shared | private`** on every `record` property. This IS the
  private layer's allowlist: `shed.py` derives `FIELD_SCOPE` from it at import
  and `split_record` projects the draft into the shared archive and the private
  record. A key not declared here reaches neither file. Adding a field means
  declaring where it may go; there is no second table to keep equal.
- **`x-tier`** on every `record` property: who stands behind the field as a
  property of the writer — the human's sentences `authored`, what shed computes
  from git and the transcript `derived`, model prose `interpreted`. Entries in
  the gated lists carry their own `tier` because the author varies per entry.
- **`x-embedded: true`** on the riders: they ride inside a carrier, never
  stand alone.
- **`x-source`** on `tier.json`: the enum is copied from weft; the test
  compares the two files when `../weft` is checked out.

## Checking

```sh
python3 shed.py check record  handoff/sessions/<ended>__<sid8>.json
python3 shed.py check thread  schemas/fixtures/thread_open.json
```

`check_shape` in `shed.py` is a stdlib required-keys checker over the subset
of JSON Schema these files use (type, enum, required, properties, items,
pattern, minLength, maxLength, additionalProperties, `$ref` to a sibling file
or a `#/properties/…` pointer, allOf, anyOf). It is FORM only — it says a file
is the contract's shape, never that its evidence is real; the evidence gate
judges that. `finalize` runs it on the shared record before writing and fails
closed. Full validation (a real draft-2020-12 validator, codegen) is the
platform's job: weft vendors these files under its own envelope and its CI
validates there.

## What is deliberately not here

A `brief` schema (a brief is a thread that escaped into its own file). A
signature rider (`ai.mythrl.sig` is layer two; it lives in weft). The
relation-basis vocabularies (`authored / mention / inferred` on lineage edges,
`authored / authored-prose / extracted / derived` on the canon graph) — a
second axis, how a link was found, riding on computed edges only; not
consolidated in this slice.

S1 (`content_sha256` on carried entries) and S2 (`id` + `supersedes` on
threads and decisions) are DECLARED here as optional fields so the slices
that make shed write and check them change no schema — only `shed.py`.
