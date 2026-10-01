#!/usr/bin/env python3
"""shed tests — no network, no credentials, stdlib only. Run: python3 test_shed.py"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SHED = os.path.join(HERE, "shed.py")
PY = sys.executable
FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        FAILS.append(name)


def sh(args, cwd, stdin=None):
    r = subprocess.run([PY, SHED] + args, cwd=cwd, capture_output=True, text=True, input=stdin)
    return r.returncode, r.stdout, r.stderr


def git(cwd, *a):
    subprocess.run(["git", "-C", cwd] + list(a), check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                            GIT_COMMITTER_EMAIL="t@t"))


def fresh_repo(tmp):
    # Name the trunk explicitly: stock Arch git still inits `master`, and the branch-scope test below checks out
    # `main` by name (first run on athena, Omarchy, 2026-09-30 — the Mac's init.defaultBranch=main hid it).
    git(tmp, "init", "-q", "-b", "main")
    with open(os.path.join(tmp, "a.txt"), "w") as f:
        f.write("one\n")
    git(tmp, "add", "a.txt")
    git(tmp, "commit", "-q", "-m", "first commit")


CLOCK = [0]


def transcript(tmp, user, assistant, minute=None):
    """Two turns on 2026-09-23 from 20:00. Each call is two minutes later than the last unless
    `minute` pins it: finalize dates an archive by its last turn, so a later session must end later."""
    m = CLOCK[0] if minute is None else minute
    CLOCK[0] = m + 2
    p = os.path.join(tmp, "t.jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"type": "user", "timestamp": "2026-09-23T{:02d}:{:02d}:00Z".format(20 + m // 60, m % 60),
                            "message": {"content": user}}) + "\n")
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-09-23T{:02d}:{:02d}:00Z".format(20 + (m + 1) // 60, (m + 1) % 60),
                            "message": {"content": [{"type": "text", "text": assistant}]}}) + "\n")
    return p


with tempfile.TemporaryDirectory() as tmp:
    fresh_repo(tmp)

    # 1. orient on a repo with no handoff says so and shows git state
    rc, out, err = sh(["orient", "--root", tmp], tmp)
    check("orient rc0 on fresh repo", rc == 0, err)
    check("orient says first session", "first recorded session" in out, out)
    check("orient shows commit", "first commit" in out, out)

    # 2. draft handoff from a transcript, then finalize
    t = transcript(tmp, "fix the login bug please", "Done. The login bug was a null check; tests pass. Next: ship it.")
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", t, "--sid8", "aaaa1111"], tmp)
    check("draft written", rc == 0 and out.strip().startswith("handoff/drafts/aaaa1111.json"), out)
    d = json.load(open(os.path.join(tmp, out.strip())))
    check("draft topic = first user turn", d["session_topic"] == "fix the login bug please", d["session_topic"])
    check("draft tldr = first sentence of last assistant", d["tldr"] == "Done.", d["tldr"])
    check("draft flagged", d["draft"] is True and d["ended_at"] is None)

    # a second draft refresh keeps started_at (idempotent per sid8)
    time.sleep(0.01)
    sh(["handoff", "--root", tmp, "--transcript", t, "--sid8", "aaaa1111"], tmp)
    d2 = json.load(open(os.path.join(tmp, "handoff/drafts/aaaa1111.json")))
    check("refresh keeps started_at", d2["started_at"] == d["started_at"])

    # a different session's draft shows in orient as unfinalized
    rc, out, _ = sh(["orient", "--root", tmp, "--sid8", "bbbb2222"], tmp)
    check("orient shows other session's draft", "Unfinalized draft from aaaa1111" in out, out)
    rc, out, _ = sh(["orient", "--root", tmp, "--sid8", "aaaa1111"], tmp)
    check("orient hides own draft", "Unfinalized draft" not in out, out)

    # a draft a /handoff archive already covers is not "unfinalized": finalize would stand down on it
    os.makedirs(os.path.join(tmp, "handoff/sessions"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "handoff/drafts"), exist_ok=True)
    json.dump({"sid8": "stal0001", "draft": True, "started_at": "2026-01-01T00:00:00Z", "tldr": "Archived."},
              open(os.path.join(tmp, "handoff/drafts/stal0001.json"), "w"))
    stale_arch = os.path.join(tmp, "handoff/sessions/2026-01-02T00-00-00Z__stal0001.json")
    json.dump({"sid8": "stal0001", "source": "skill", "ended_at": "2026-01-02T00:00:00Z"}, open(stale_arch, "w"))
    rc, out, _ = sh(["orient", "--root", tmp, "--sid8", "bbbb2222", "--max-lines", "0"], tmp)
    check("orient hides a draft its /handoff archive supersedes",
          "Unfinalized draft from stal0001" not in out and "1 stale draft hidden" in out
          and "Unfinalized draft from aaaa1111" in out, out)
    # an archive that ended BEFORE the draft started is an earlier session under the same sid8: keep showing it
    json.dump({"sid8": "stal0001", "source": "skill", "ended_at": "2025-12-31T00:00:00Z"}, open(stale_arch, "w"))
    rc, out, _ = sh(["orient", "--root", tmp, "--sid8", "bbbb2222", "--max-lines", "0"], tmp)
    check("orient keeps a draft newer than the sid8's archive", "Unfinalized draft from stal0001" in out, out)
    os.remove(stale_arch)
    os.remove(os.path.join(tmp, "handoff/drafts/stal0001.json"))

    # finalize -> archive, draft removed
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", t, "--sid8", "aaaa1111", "--finalize"], tmp)
    check("finalize rc0", rc == 0, out)
    final = out.strip()
    check("archive name shape", final.startswith("handoff/sessions/") and final.endswith("__aaaa1111.json"), final)
    check("draft removed", not os.path.exists(os.path.join(tmp, "handoff/drafts/aaaa1111.json")))
    a = json.load(open(os.path.join(tmp, final)))
    check("archive ended_at set", bool(a["ended_at"]) and a["draft"] is False)

    # 3. a human/model fills open_threads; next orient reads them
    a["open_threads"] = [{"thread": "ship it", "next_step": "tag v1"}]
    a["watch_outs"] = ["CI is flaky on mondays"]
    json.dump(a, open(os.path.join(tmp, final), "w"))
    rc, out, _ = sh(["orient", "--root", tmp, "--sid8", "cccc3333"], tmp)
    check("orient names last session", "Last session: aaaa1111" in out, out)
    check("orient shows open thread", "ship it -> tag v1" in out, out)
    check("orient shows watch out", "CI is flaky" in out, out)

    # 4. line ceiling truncates with a marker
    for i in range(50):
        a["watch_outs"].append("w%d" % i)
    json.dump(a, open(os.path.join(tmp, final), "w"))
    rc, out, _ = sh(["orient", "--root", tmp, "--max-lines", "10"], tmp)
    check("ceiling holds", len(out.rstrip("\n").splitlines()) == 10, len(out.splitlines()))
    check("ceiling marker", "hidden by the ceiling" in out)
    rc, out, _ = sh(["orient", "--root", tmp, "--max-lines", "0"], tmp)
    check("ceiling off", "w49" in out)

    # 5. open briefs: status open + done_when + touched within 30d shows; declined does not
    bdir = os.path.join(tmp, "handoff", "brief")
    os.makedirs(bdir)
    open(os.path.join(bdir, "live.md"), "w").write("---\ntitle: Live brief\nstatus: open\ncreated: 2026-09-01\ndone_when: >\n  x\n---\nbody\n")
    open(os.path.join(bdir, "dead.md"), "w").write("---\ntitle: Dead brief\nstatus: declined\ncreated: 2026-09-01\ndone_when: x\n---\nbody\n")
    open(os.path.join(bdir, "nodone.md"), "w").write("---\ntitle: No done\nstatus: open\ncreated: 2026-09-01\n---\nbody\n")
    rc, out, _ = sh(["orient", "--root", tmp, "--max-lines", "0"], tmp)
    check("open brief listed", "Live brief" in out, out)
    check("declined brief hidden", "Dead brief" not in out)
    check("brief without done_when hidden", "No done" not in out)

    # 6. hook entry points take JSON on stdin; session-start prints the page
    payload = json.dumps({"session_id": "dddd4444-rest", "cwd": tmp, "transcript_path": t, "hook_event_name": "SessionStart"})
    rc, out, _ = sh(["hook", "session-start"], tmp, stdin=payload)
    check("hook session-start prints page", rc == 0 and out.startswith("# orient"), out[:80])
    rc, out, _ = sh(["hook", "stop"], tmp, stdin=payload)
    check("hook stop drafts", os.path.exists(os.path.join(tmp, "handoff/drafts/dddd4444.json")))
    rc, out, _ = sh(["hook", "stop"], tmp, stdin=json.dumps({"session_id": "eeee5555", "cwd": tmp, "stop_hook_active": True}))
    check("stop_hook_active is a no-op", not os.path.exists(os.path.join(tmp, "handoff/drafts/eeee5555.json")))
    rc, out, _ = sh(["hook", "session-end"], tmp, stdin=payload)
    check("hook session-end finalizes", any(f.endswith("__dddd4444.json") for f in os.listdir(os.path.join(tmp, "handoff/sessions"))))

    # 7. install merges hooks idempotently and preserves other settings
    os.makedirs(os.path.join(tmp, ".claude"))
    json.dump({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}},
              open(os.path.join(tmp, ".claude", "settings.json"), "w"))
    rc, out, _ = sh(["install", "--root", tmp], tmp)
    s = json.load(open(os.path.join(tmp, ".claude", "settings.json")))
    check("install rc0", rc == 0, out)
    check("install preserved permissions", s["permissions"]["allow"] == ["Bash(ls)"])
    check("install preserved existing Stop hook", s["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo hi")
    check("install added three events", all(e in s["hooks"] for e in ("SessionStart", "Stop", "SessionEnd")))
    rc, out, _ = sh(["install", "--root", tmp], tmp)
    s2 = json.load(open(os.path.join(tmp, ".claude", "settings.json")))
    check("install idempotent", s2 == s and "already installed" in out, out)
    # 7a. the same-checkout form quotes the path; install must still see it as present (it duplicated
    # all three hooks in hololoom_mcp on 2026-09-25 before this check existed)
    quoted = {"hooks": {e: [{"hooks": [{"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/../shed/shed.py" hook ' + v, "timeout": 30}]}]
                        for e, v in (("SessionStart", "session-start"), ("Stop", "stop"), ("SessionEnd", "session-end"))}}
    json.dump(quoted, open(os.path.join(tmp, ".claude", "settings.json"), "w"))
    rc, out, _ = sh(["install", "--root", tmp], tmp)
    s3 = json.load(open(os.path.join(tmp, ".claude", "settings.json")))
    check("install sees the quoted same-checkout hook as present", "already installed" in out and all(len(s3["hooks"][e]) == 1 for e in s3["hooks"]), out)
    json.dump(s, open(os.path.join(tmp, ".claude", "settings.json"), "w"))

    # 7b. install: shed.py is in a different checkout than tmp -> absolute path; gitignore appended once
    cmd = s["hooks"]["SessionStart"][-1]["hooks"][0]["command"]
    check("install uses absolute path across checkouts", SHED in cmd and "$CLAUDE_PROJECT_DIR" not in cmd, cmd)
    gi = open(os.path.join(tmp, ".gitignore")).read()
    check("install gitignores drafts + metrics", "handoff/drafts/" in gi and "handoff/metrics.jsonl" in gi, gi)
    check("install gitignores the private layer", "handoff/private/" in gi and os.path.isdir(os.path.join(tmp, "handoff/private")), gi)
    check("gitignore appended once", gi.count("handoff/drafts/") == 1)

    # 7b'. install teaches declare on the page; a hand-wired hook (no flag) does not (first run on athena, 2026-09-30:
    # a fresh repo carries no CLAUDE.md prose, so without this line no session there ever declared)
    check("install writes --declare-hint on session-start only",
          cmd.endswith("hook session-start --declare-hint")
          and not s["hooks"]["Stop"][-1]["hooks"][0]["command"].endswith("--declare-hint"), cmd)
    hp = json.dumps({"session_id": "abcd1234-rest", "cwd": tmp, "hook_event_name": "SessionStart"})
    rc, out, _ = sh(["hook", "session-start", "--declare-hint"], tmp, stdin=hp)
    hint = [l for l in out.splitlines() if "declare" in l and "Before editing" in l]
    check("declare hint on line three, sid8 spelled out, absolute path across checkouts",
          rc == 0 and out.splitlines()[2:3] == hint and "--sid8 abcd1234" in hint[0] and SHED in hint[0], out[:400])
    rc, out, _ = sh(["hook", "session-start"], tmp, stdin=hp)
    check("no declare hint without the flag (hand-wired hook, e.g. hololoom_mcp)", "Before editing" not in out, out[:400])

    # 7c. finalize stands down when a richer archive for the sid8 already landed after start
    t2 = transcript(tmp, "second session", "All done here.")
    sh(["handoff", "--root", tmp, "--transcript", t2, "--sid8", "ffff6666"], tmp)
    d3 = json.load(open(os.path.join(tmp, "handoff/drafts/ffff6666.json")))
    rich = os.path.join(tmp, "handoff/sessions/2099-01-01T00-00-00Z__ffff6666.json")
    json.dump({"ended_at": "2099-01-01T00:00:00Z", "tldr": "the skill wrote this"}, open(rich, "w"))
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", t2, "--sid8", "ffff6666", "--finalize"], tmp)
    check("stand-down rc0", rc == 0, out)
    check("stand-down message", "stood down" in out, out)
    check("stand-down leaves the rich archive alone", json.load(open(rich))["tldr"] == "the skill wrote this")
    check("stand-down wrote no shed archive", not [f for f in os.listdir(os.path.join(tmp, "handoff/sessions"))
                                                   if f.endswith("__ffff6666.json") and not f.startswith("2099")])
    check("stand-down removed the draft", not os.path.exists(os.path.join(tmp, "handoff/drafts/ffff6666.json")))
    os.remove(rich)  # dated 2099: would sort as "last session" for every later orient

    # 8. metrics recorded
    m = [json.loads(l) for l in open(os.path.join(tmp, "handoff", "metrics.jsonl"))]
    check("orient metric has est_tokens", any(r["event"] == "orient" and "est_tokens" in r for r in m))
    check("handoff metric has first_commit_seconds key", any(r["event"] == "handoff" and "first_commit_seconds" in r for r in m))

    # 9. grounded handoff — drift refused mechanically (handoff/brief/shed_grounded_handoff_no_drift.md)
    def draft_with(sid, entries, user="please retag the release and fix the flaky monday CI job. thanks",
                   assistant="Retagged v1.1. The monday CI flake is a timezone bug in the cron matcher; left it open."):
        tp = transcript(tmp, user, assistant)
        sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", sid], tmp)
        dp = os.path.join(tmp, "handoff/drafts/%s.json" % sid)
        dd = json.load(open(dp))
        dd.update(entries)
        json.dump(dd, open(dp, "w"))
        return tp

    # (b) human_first / human_last are the human's verbatim sentences, printed by orient before tldr
    tp = draft_with("aaaa0001", {})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0001", "--finalize"], tmp)
    check("human_first finalize rc0", rc == 0, out)
    a1 = json.load(open(os.path.join(tmp, out.strip())))
    p1 = json.load(open(os.path.join(tmp, "handoff/private/aaaa0001.json")))
    check("human_first verbatim (private record)", p1["human_first"] == "please retag the release and fix the flaky monday CI job.", p1.get("human_first"))
    check("human_last verbatim (single user turn = same sentence)", p1["human_last"] == p1["human_first"])
    check("private record points at its archive", p1["archive"] == os.path.basename(out.strip()), p1.get("archive"))
    rc, out, _ = sh(["orient", "--root", tmp, "--max-lines", "0", "--sid8", "zzzz0000"], tmp)
    lines = out.splitlines()
    hf = next((i for i, l in enumerate(lines) if "human first:" in l), None)
    td = next((i for i, l in enumerate(lines) if "tldr:" in l), None)
    check("orient prints human_first before tldr", hf is not None and td is not None and hf < td, (hf, td))

    # (a) evidence gate: an interpreted entry without evidence is REJECTED, entry named, draft kept, rc != 0
    tp = draft_with("aaaa0002", {"open_threads": [{"thread": "monday CI flake", "next_step": "fix the tz bug", "tier": "interpreted"}]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0002", "--finalize"], tmp)
    check("evidence gate rejects (rc non-zero)", rc != 0, out)
    check("evidence gate names the entry", "open_threads[0]" in out, out)
    check("evidence gate keeps the draft", os.path.exists(os.path.join(tmp, "handoff/drafts/aaaa0002.json")))
    check("evidence gate wrote no archive", not [f for f in os.listdir(os.path.join(tmp, "handoff/sessions")) if f.endswith("__aaaa0002.json")])
    # the retired sub-tiers are unreadable, not a fourth tier (weft's rule, adopted verbatim)
    for old in ("extracted", "synthesized"):
        tp = draft_with("aaaa0009", {"watch_outs": [{"text": "CI flaky on mondays", "tier": old,
                                                     "evidence": {"turn": 1, "quote": "timezone"}}]})
        rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0009", "--finalize"], tmp)
        check("retired tier {!r} rejected as unknown".format(old), rc != 0 and "unknown tier" in out, out)
    # the CLAIM must be a verbatim span of the named turn — a paraphrase is rejected even with evidence
    tp = draft_with("aaaa0003", {"decisions": [{"text": "leave the flake open", "tier": "interpreted",
                                                "evidence": {"turn": 1}}]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0003", "--finalize"], tmp)
    check("evidence gate rejects a paraphrased claim", rc != 0 and "not a verbatim span" in out, out)
    # the 2026-09-25 adversarial finding: a fabricated claim glued to a REAL but unrelated quote must not pass
    tp = draft_with("aaaa0010", {"open_threads": [{"thread": "the production database was migrated to Iowa", "next_step": "notify legal",
                                                   "tier": "interpreted", "evidence": {"turn": 1, "quote": "cron"}}]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0010", "--finalize"], tmp)
    check("evidence gate rejects a fabricated claim beside a real quote", rc != 0 and "not a verbatim span" in out, out)
    # a verbatim claim with a stale attached quote is still rejected on the quote
    tp = draft_with("aaaa0011", {"decisions": [{"text": "left it open", "tier": "interpreted",
                                                "evidence": {"turn": 1, "quote": "the monday flake is a DST bug"}}]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0011", "--finalize"], tmp)
    check("evidence gate rejects a non-substring quote", rc != 0 and "quote is not a substring" in out, out)
    # a bare string is unknown-author and rejected; an authored entry passes without evidence
    tp = draft_with("aaaa0004", {"watch_outs": ["CI flaky on mondays"]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0004", "--finalize"], tmp)
    check("evidence gate rejects a bare string", rc != 0 and "bare string" in out, out)
    tp = draft_with("aaaa0005", {"watch_outs": [{"text": "CI flaky on mondays", "tier": "authored"}],
                                 "open_threads": [{"thread": "timezone bug in the cron matcher", "gloss": "the tz bug behind the monday flake",
                                                   "next_step": "fix cron matcher", "tier": "interpreted", "evidence": {"turn": 1}}]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0005", "--finalize"], tmp)
    check("evidence gate passes authored + verbatim-claim interpreted (gloss ungated)", rc == 0, out)
    a5 = json.load(open(os.path.join(tmp, out.strip())))
    check("gloss survives finalize unchanged", a5["open_threads"][0].get("gloss") == "the tz bug behind the monday flake")

    # (c) extractiveness recorded per finalized handoff, in the archive and in metrics.jsonl
    check("extractiveness in archive", isinstance(a5.get("extractiveness"), float) and 0.0 <= a5["extractiveness"] <= 1.0, a5.get("extractiveness"))
    m = [json.loads(l) for l in open(os.path.join(tmp, "handoff", "metrics.jsonl"))]
    check("extractiveness in metrics", any(r["event"] == "handoff" and r.get("sid8") == "aaaa0005" and "extractiveness" in r for r in m))
    check("handoff_rejected metric", any(r["event"] == "handoff_rejected" and r.get("sid8") == "aaaa0002" for r in m))

    # (d) carry forward: byte-identical + origin_sid8 passes; a restated copy is rejected
    carried = dict(a5["open_threads"][0], origin_sid8="aaaa0005")
    tp = draft_with("aaaa0006", {"open_threads": [carried]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0006", "--finalize"], tmp)
    check("origin_sid8 byte-identical carry passes", rc == 0, out)
    restated = dict(carried, next_step="fix the cron matcher's timezone handling")
    tp = draft_with("aaaa0007", {"open_threads": [restated]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0007", "--finalize"], tmp)
    check("origin_sid8 restated carry rejected", rc != 0 and "not byte-identical" in out, out)
    tp = draft_with("aaaa0008", {"open_threads": [dict(carried, origin_sid8="nope0000")]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "aaaa0008", "--finalize"], tmp)
    check("origin_sid8 unknown archive rejected", rc != 0 and "no archive" in out, out)
    rc, out, _ = sh(["orient", "--root", tmp, "--max-lines", "0", "--sid8", "zzzz0000"], tmp)
    check("orient marks tier + origin", "[interpreted, from aaaa0005]" in out, out)

    # 10. private layer (handoff/brief/shed_private_layer.md): the record splits BEFORE any human
    # word is kept in the shared archive; the shared file is an allowlist; crossing is --share only.
    sys.path.insert(0, HERE)
    import shed as shedmod
    tp = draft_with("bbbb0001", {"session_topic": "the sloop should remember where I moored it", "human_first": "the sloop should remember where I moored it.",
                                 "human_last": "ok thanks.", "some_new_field": "leak?"},
                    user="the sloop should remember where I moored it. can you retag the release? ok thanks.")
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "bbbb0001", "--finalize"], tmp)
    check("private layer: finalize rc0", rc == 0, out)
    shared = json.load(open(os.path.join(tmp, out.strip())))
    private = json.load(open(os.path.join(tmp, "handoff/private/bbbb0001.json")))
    check("private layer: no private field reaches the shared archive",
          not any(k in shared for k in shedmod.PRIVATE_FIELDS), [k for k in shedmod.PRIVATE_FIELDS if k in shared])
    check("private layer: her words are in the private record", private["human_first"] == "the sloop should remember where I moored it.", private)
    check("private layer: transcript pointer is private", private.get("transcript_path") == tp and "transcript_path" not in shared)
    check("allowlist: every shared key is declared shared",
          all(shedmod.FIELD_SCOPE.get(k) == "shared" for k in shared), [k for k in shared if shedmod.FIELD_SCOPE.get(k) != "shared"])
    check("allowlist: an undeclared field crosses into neither record",
          "some_new_field" not in shared and "some_new_field" not in private)
    check("allowlist: session_topic (her sentence, normalized) is private", "session_topic" not in shared and private["session_topic"])
    # the shared archive text as a whole never contains her sentence — the byte-level version of the claim
    raw = open(os.path.join(tmp, out.strip())).read()
    check("private layer: shared archive bytes never carry her sentence", "remember where I moored" not in raw)
    # a private field cannot reach the shared record by any path but --share: split_record is the only writer
    sh_, pr_ = shedmod.split_record({"sid8": "x", "tldr": "t", "human_last": "secret"}, share=())
    check("allowlist: split_record drops private fields with no share", "human_last" not in sh_ and pr_["human_last"] == "secret")
    # orient reads the private record back on THIS machine (her words, before tldr) and states the recording
    rc, out, _ = sh(["orient", "--root", tmp, "--max-lines", "0", "--sid8", "zzzz0000"], tmp)
    lines = out.splitlines()
    check("recorded notice is line 2 of the page", len(lines) > 1 and lines[1].startswith("Recorded: ") and "private/" in lines[1] and "shared in git" in lines[1], lines[:2])
    check("private layer: orient reads her words back from private/", "human first: the sloop should remember where I moored it." in out, out)
    # a clone has no private/ — orient still works and never shows her words
    os.rename(os.path.join(tmp, "handoff/private"), os.path.join(tmp, "handoff/private.away"))
    rc, out, _ = sh(["orient", "--root", tmp, "--max-lines", "0", "--sid8", "zzzz0000"], tmp)
    check("private layer: a clone orients without her words", rc == 0 and "Last session: bbbb0001" in out and "where I moored" not in out, out)
    os.rename(os.path.join(tmp, "handoff/private.away"), os.path.join(tmp, "handoff/private"))
    # --share <field> is the only crossing; it is recorded on the shared record; an unknown/shared name is refused
    tp = draft_with("bbbb0002", {}, user="ship it when green.", assistant="Shipped.")
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "bbbb0002", "--finalize", "--share", "human_last"], tmp)
    shared2 = json.load(open(os.path.join(tmp, out.strip())))
    check("--share human_last crosses that one field", rc == 0 and shared2.get("human_last") == "ship it when green." and "human_first" not in shared2, shared2)
    check("--share is recorded on the shared record", shared2.get("shared_fields") == ["human_last"], shared2.get("shared_fields"))
    tp = draft_with("bbbb0003", {}, user="again.", assistant="Again.")
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "bbbb0003", "--finalize", "--share", "tldr"], tmp)
    check("--share refuses a field that is not private", rc != 0 and "not private" in out and os.path.exists(os.path.join(tmp, "handoff/drafts/bbbb0003.json")), out)
    # the session-end hook path shares nothing
    tp = draft_with("bbbb0004", {}, user="hook path.", assistant="Done.")
    rc, out, _ = sh(["hook", "session-end"], tmp, stdin=json.dumps({"session_id": "bbbb0004-rest", "cwd": tmp, "transcript_path": tp}))
    hs = [f for f in os.listdir(os.path.join(tmp, "handoff/sessions")) if f.endswith("__bbbb0004.json")]
    check("hook session-end shares nothing", rc == 0 and hs and "human_first" not in json.load(open(os.path.join(tmp, "handoff/sessions", hs[0]))), (rc, hs))
    # commit-and-reveal: the shared record carries the hash of the private one; --share is checkable
    check("private layer: shared carries private_sha256, not contents",
          len(shared.get("private_sha256", "")) == 64 and "where I moored" not in json.dumps(shared), shared.get("private_sha256"))
    check("private layer: commitment matches the private record", shedmod.verify_share(shared, private))
    rc, out, _ = sh(["verify", os.path.relpath(os.path.join(tmp, "handoff/sessions", [f for f in os.listdir(os.path.join(tmp, "handoff/sessions")) if f.endswith("__bbbb0001.json")][0]), tmp), "--root", tmp], tmp)
    check("verify rc0 on a matching record", rc == 0 and out.startswith("ok"), out)
    tampered = dict(private, human_last="ok thanks, I feel great.")
    check("a rewritten private record no longer matches", not shedmod.verify_share(shared, tampered))
    json.dump(tampered, open(os.path.join(tmp, "handoff/private/bbbb0001.json"), "w"))
    rc, out, _ = sh(["verify", "handoff/sessions/" + [f for f in os.listdir(os.path.join(tmp, "handoff/sessions")) if f.endswith("__bbbb0001.json")][0], "--root", tmp], tmp)
    check("verify rc1 + MISMATCH on a rewritten record", rc == 1 and "MISMATCH" in out, out)
    json.dump(private, open(os.path.join(tmp, "handoff/private/bbbb0001.json"), "w"))
    os.rename(os.path.join(tmp, "handoff/private"), os.path.join(tmp, "handoff/private.away"))
    rc, out, _ = sh(["verify", "handoff/sessions/" + [f for f in os.listdir(os.path.join(tmp, "handoff/sessions")) if f.endswith("__bbbb0001.json")][0], "--root", tmp], tmp)
    check("verify rc2 (silent, not a verdict) on a clone", rc == 2 and "cannot verify" in out, out)
    os.rename(os.path.join(tmp, "handoff/private.away"), os.path.join(tmp, "handoff/private"))
    # the --share'd field hashes into the same commitment: shared text that matches IS what was recorded
    p2 = json.load(open(os.path.join(tmp, "handoff/private/bbbb0002.json")))
    check("--share: the shared text is the committed text", shedmod.verify_share(shared2, p2) and p2["human_last"] == shared2["human_last"])
    # the hook path has no share attribute at all — sharing cannot grow there by accident
    check("hook path has no share attribute", "args.share = []" not in open(SHED).read())

    # a live scratchpad in handoff/sessions/ must not be read as the last session (2026-09-25 main-checkout page)
    json.dump({"tldr": "I am a scratchpad"}, open(os.path.join(tmp, "handoff/sessions/SESSION.zzzz9999.json"), "w"))
    rc, out, _ = sh(["orient", "--root", tmp, "--max-lines", "0", "--sid8", "zzzz0000"], tmp)
    check("orient ignores SESSION.*.json scratchpads", "scratchpad" not in out and "Last session: bbbb0004" in out, out)

    # 11. schemas/ (handoff/brief/shed_protocol_schemas.md S3): eight files, fixtures load against them,
    # mutated fixtures are refused, the private-layer allowlist is DERIVED from x-scope, the tier enum
    # is weft's verbatim, every record carries a basis reading per instrument, and `check` is a verb.
    SCHEMAS = os.path.join(HERE, "schemas")
    names = sorted(f[:-5] for f in os.listdir(SCHEMAS) if f.endswith(".json"))
    check("schemas: eight files", names == ["basis", "decision", "discharge", "intent", "lease", "record", "thread", "tier"], names)
    for n in names:
        s = json.load(open(os.path.join(SCHEMAS, n + ".json")))
        check("schemas: {} is draft 2020-12 with $id".format(n), s.get("$schema", "").endswith("2020-12/schema") and s.get("$id") == "shed." + n, s.get("$id"))
    fixture_of = {"record": "record", "intent": "intent", "thread_open": "thread", "thread_watch": "thread", "decision": "decision",
                  "discharge": "discharge", "tier": "tier", "basis": "basis", "lease": "lease"}
    fixtures = {}
    for fx, sn in fixture_of.items():
        fixtures[fx] = json.load(open(os.path.join(SCHEMAS, "fixtures", fx + ".json")))
        probs = shedmod.check_shape(fixtures[fx], shedmod.schema(sn))
        check("fixture {} is {}'s shape".format(fx, sn), probs == [], probs)
    # mutations: a missing required key, a bad enum, a bad pattern, an unknown key on a rider, a wrong type
    bad = dict(fixtures["thread_open"]); del bad["tier"]
    check("schemas: thread without tier refused", any("missing required 'tier'" in p for p in shedmod.check_shape(bad, shedmod.schema("thread"))))
    bad = dict(fixtures["thread_open"], tier="extracted")
    check("schemas: retired tier refused by the schema too", any("'extracted' not one of" in p for p in shedmod.check_shape(bad, shedmod.schema("thread"))))
    bad = dict(fixtures["thread_open"], expires="next tuesday")
    check("schemas: lease.expires must be a date", any("expires" in p and "does not match" in p for p in shedmod.check_shape(bad, shedmod.schema("thread"))))
    bad = {k: v for k, v in fixtures["thread_watch"].items() if k != "text"}
    check("schemas: a thread needs `thread` or `text`", any("matches none of" in p for p in shedmod.check_shape(bad, shedmod.schema("thread"))))
    bad = dict(fixtures["basis"], reason="x")
    check("schemas: rider refuses an unknown key", any("unknown key 'reason'" in p for p in shedmod.check_shape(bad, shedmod.schema("basis"))))
    bad = dict(fixtures["discharge"], expect_rc="0")
    check("schemas: expect_rc must be an integer", any("expect_rc" in p and "expected integer" in p for p in shedmod.check_shape(bad, shedmod.schema("discharge"))))
    bad = dict(fixtures["record"], open_threads=[dict(fixtures["thread_open"], id="not-an-id")])
    check("schemas: nested $ref reaches into the record's lists", any("open_threads[0].id" in p for p in shedmod.check_shape(bad, shedmod.schema("record"))))
    check("schemas: True is not an integer", shedmod.check_shape(True, {"type": "integer"}) != [] and shedmod.check_shape(True, {"type": "boolean"}) == [])
    # the allowlist is the schema: every record property declares x-scope AND x-tier; FIELD_SCOPE is that table
    rec_props = shedmod.schema("record")["properties"]
    check("schemas: every record field declares x-scope", all(p.get("x-scope") in ("shared", "private") for p in rec_props.values()),
          [k for k, p in rec_props.items() if p.get("x-scope") not in ("shared", "private")])
    check("schemas: every record field declares x-tier", all(p.get("x-tier") in shedmod.TIERS for p in rec_props.values()),
          [k for k, p in rec_props.items() if p.get("x-tier") not in shedmod.TIERS])
    check("schemas: FIELD_SCOPE is derived from x-scope", shedmod.FIELD_SCOPE == {k: p["x-scope"] for k, p in rec_props.items()})
    check("schemas: her words are private in the schema", all(rec_props[k]["x-scope"] == "private" for k in ("human_first", "human_last", "session_topic", "transcript_path")))
    check("schemas: what shed derives from git is x-tier derived", all(rec_props[k]["x-tier"] == "derived" for k in ("progress", "files_touched", "turns", "first_commit_seconds")))
    check("schemas: a finalized archive is record.json's shape", shedmod.check_shape(shared2, shedmod.schema("record")) == [],
          shedmod.check_shape(shared2, shedmod.schema("record")))
    # tier enum = weft's rider enum, verbatim (compared only when weft is checked out beside shed)
    weft_tier = os.path.join(HERE, "..", "weft", "schemas", "ai.mythrl.tier.json")
    if os.path.exists(weft_tier):
        w = json.load(open(weft_tier))["properties"]["tier"]["enum"]
        check("schemas: tier enum equals weft's ai.mythrl.tier", shedmod.schema("tier")["enum"] == w, (shedmod.schema("tier")["enum"], w))
    else:
        print("skip schemas: weft not checked out beside shed; tier enum not compared")
    check("schemas: TIERS is read from tier.json", tuple(shedmod.schema("tier")["enum"]) == shedmod.TIERS)
    # basis rider: a record names the state of each instrument it read; a missing transcript is BLIND, never zero turns
    check("basis: finalized archive carries transcript + git readings",
          sorted(b["instrument"] for b in shared2.get("basis", [])) == ["git", "transcript"] and all(b["state"] == "read" for b in shared2["basis"]), shared2.get("basis"))
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", os.path.join(tmp, "no-such.jsonl"), "--sid8", "cccc0001"], tmp)
    dblind = json.load(open(os.path.join(tmp, "handoff/drafts/cccc0001.json")))
    tb = next(b for b in dblind["basis"] if b["instrument"] == "transcript")
    check("basis: missing transcript is BLIND, not empty", tb["state"] == "BLIND" and "no-such.jsonl" in tb.get("detail", "") and dblind["turns"] == 0, tb)
    # the gate: a tier-less entry is refused outright (absence is unstated; unstated never renders as authored)
    tp = draft_with("cccc0002", {"open_threads": [{"thread": "tz bug", "next_step": "fix cron matcher",
                                                   "evidence": {"turn": 1, "quote": "timezone bug in the cron matcher"}}]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "cccc0002", "--finalize"], tmp)
    check("gate: tier-less entry refused", rc != 0 and "no tier" in out, out)
    # finalize runs the schema check after the gate: an authored entry passes the gate but a bad lease fails the shape
    tp = draft_with("cccc0003", {"watch_outs": [{"text": "CI flaky on mondays", "tier": "authored", "expires": "soon"}]})
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", tp, "--sid8", "cccc0003", "--finalize"], tmp)
    check("finalize: shared record must be record.json's shape (bad lease refused, draft kept)",
          rc != 0 and "record.json" in out and "watch_outs[0].expires" in out and os.path.exists(os.path.join(tmp, "handoff/drafts/cccc0003.json")), out)
    # `check` verb: 0 = shape holds, 1 = problems listed, 2 = unreadable
    rc, out, _ = sh(["check", "thread", os.path.join(SCHEMAS, "fixtures", "thread_watch.json")], tmp)
    check("check verb rc0 on a fixture", rc == 0 and out.rstrip().endswith("thread ok"), out)
    rc, out, _ = sh(["check", "record", os.path.join(SCHEMAS, "fixtures", "thread_watch.json")], tmp)
    check("check verb rc1 names the problems", rc == 1 and "missing required 'sid8'" in out, out)
    rc, out, _ = sh(["check", "record", os.path.join(tmp, "does-not-exist.json")], tmp)
    check("check verb rc2 on an unreadable file", rc == 2, out)
    rc, out, _ = sh(["check", "brief", os.path.join(SCHEMAS, "fixtures", "lease.json")], tmp)
    check("check verb rc2 on a schema that is not one of the eight", rc == 2 and "no schema" in out, out)

    # 12. declare — the third verb. Adversarial pass 2026-09-25: the first shed-finalized archive carried zero
    # forward-intent, and the verb cut from slice one is the one that fills it without a model in the seat.
    fresh = os.path.join(tmp, "declare_repo")
    os.makedirs(fresh)
    fresh_repo(fresh)
    rc, out, _ = sh(["declare", "make the widget", "--root", fresh, "--sid8", "dcl00001"], fresh)
    check("declare rc0 + writes the draft", rc == 0 and "declared ->" in out, out)
    dd = json.load(open(os.path.join(fresh, "handoff/drafts/dcl00001.json")))
    check("declare: intent in the draft is intent.json's shape",
          shedmod.check_shape(dd["intent"], shedmod.schema("intent")) == [] and dd["intent"]["statement"] == "make the widget", dd.get("intent"))
    check("declare: same statement twice is a no-op", "already declared" in sh(["declare", "make the widget", "--root", fresh, "--sid8", "dcl00001"], fresh)[1])
    sh(["declare", "make the gadget", "--root", fresh, "--sid8", "dcl00001", "--reason", "widget was the wrong noun"], fresh)
    dd = json.load(open(os.path.join(fresh, "handoff/drafts/dcl00001.json")))
    check("declare: re-declaring keeps the old statement under superseded",
          dd["intent"]["statement"] == "make the gadget" and dd["intent"]["superseded"][0]["statement"] == "make the widget"
          and dd["intent"]["superseded"][0]["reason"] == "widget was the wrong noun", dd["intent"])
    t1 = transcript(fresh, "build the gadget please", "Started on the gadget.")
    sh(["handoff", "--root", fresh, "--transcript", t1, "--sid8", "dcl00001"], fresh)
    dd = json.load(open(os.path.join(fresh, "handoff/drafts/dcl00001.json")))
    check("declare: the Stop refresh carries intent", dd.get("intent", {}).get("statement") == "make the gadget", dd.get("intent"))
    rc, out, _ = sh(["orient", "--root", fresh, "--sid8", "dcl00002", "--max-lines", "0"], fresh)
    check("orient shows a peer's live declaration", "declared: make the gadget" in out, out)
    rc, out, _ = sh(["handoff", "--root", fresh, "--transcript", t1, "--sid8", "dcl00001", "--finalize"], fresh)
    check("declare: unarmed finalize rc0", rc == 0, out)
    a = json.load(open(os.path.join(fresh, out.strip())))
    th = a["open_threads"]
    check("declare: UNARMED becomes an authored open thread, statement verbatim",
          len(th) == 1 and th[0]["thread"] == "make the gadget" and th[0]["tier"] == "authored" and th[0]["intent_verdict"] == "UNARMED", th)
    check("declare: intent is in the shared archive", a.get("intent", {}).get("statement") == "make the gadget", a.get("intent"))
    check("declare: an archive with intent is record.json's shape", shedmod.check_shape(a, shedmod.schema("record")) == [],
          shedmod.check_shape(a, shedmod.schema("record")))
    rc, out, _ = sh(["orient", "--root", fresh, "--sid8", "dcl00002", "--max-lines", "0"], fresh)
    check("orient: the next session's page names the declared thread",
          "declared: make the gadget" in out and "- make the gadget ->" in out and "[intent UNARMED]" in out, out)
    rc, out, _ = sh(["declare", "x", "--root", fresh, "--sid8", "dcl00003", "--cmd", "true"], fresh)
    check("declare: --cmd without --expires refused", rc == 2 and "--expires" in out, out)
    t2 = transcript(fresh, "second", "Second done.")
    sh(["declare", "touch the marker file", "--root", fresh, "--sid8", "dcl00003", "--cmd", "test -f marker.txt", "--expires", "2099-01-01"], fresh)
    open(os.path.join(fresh, "marker.txt"), "w").write("x")
    rc, out, _ = sh(["handoff", "--root", fresh, "--transcript", t2, "--sid8", "dcl00003", "--finalize"], fresh)
    a = json.load(open(os.path.join(fresh, out.strip())))
    check("declare: MET becomes a progress line, not a thread",
          "intent MET: touch the marker file" in a["progress"] and a["open_threads"] == [], (a["progress"], a["open_threads"]))
    sh(["declare", "remove the marker file", "--root", fresh, "--sid8", "dcl00004", "--cmd", "test -f marker.txt", "--expect-absent",
        "--expires", "2099-01-01", "--scope", "repo"], fresh)
    rc, out, _ = sh(["handoff", "--root", fresh, "--transcript", t2, "--sid8", "dcl00004", "--finalize"], fresh)
    a = json.load(open(os.path.join(fresh, out.strip())))
    th = a["open_threads"]
    check("declare: UNMET becomes an authored thread carrying its own done_when + lease",
          len(th) == 1 and th[0]["intent_verdict"] == "UNMET" and th[0]["done_when"] == {"cmd": "test -f marker.txt", "expect_rc": 0, "expect_absent": True}
          and th[0]["expires"] == "2099-01-01" and th[0]["scope"] == "repo", th)
    sh(["declare", "run a command that does not exist", "--root", fresh, "--sid8", "dcl00005", "--cmd", "no_such_command_xyz", "--expires", "2099-01-01"], fresh)
    rc, out, _ = sh(["handoff", "--root", fresh, "--transcript", t2, "--sid8", "dcl00005", "--finalize"], fresh)
    a = json.load(open(os.path.join(fresh, out.strip())))
    check("declare: ERROR (rc 127) is a thread with blockers, never MET",
          a["open_threads"][0]["intent_verdict"] == "ERROR" and "instrument silent" in a["open_threads"][0]["blockers"], a["open_threads"])
    m = [json.loads(l) for l in open(os.path.join(fresh, "handoff", "metrics.jsonl"))]
    check("declare: metrics carry declare + intent-verdict events",
          any(r["event"] == "declare" for r in m) and any(r["event"] == "intent" and r.get("verdict") == "MET" for r in m))
    met = [r for r in m if r["event"] == "handoff" and r["sid8"] == "dcl00003"]
    met_arch = json.load(open(os.path.join(fresh, met[0]["path"]))) if met else {}
    real = [p for p in met_arch.get("progress", []) if not p.startswith("intent ")]
    check("declare: a MET line is not counted as a commit (progress aliased commits; athena witness 2026-09-30)",
          met and met[0]["commits"] == len(real), (met, met_arch.get("progress")))

    # 13. extractiveness is over the UNGATED prose: a gated claim is verbatim by construction and would score 1.0
    # whatever the model did (the adversarial pass called it a counter that cannot increment)
    exd = os.path.join(tmp, "extractiveness")
    os.makedirs(exd)
    tl = shedmod.read_transcript(transcript(exd, "build the gadget please", "Started on the gadget."))["turn_list"]
    base = {"tldr": "Started on the gadget.", "open_threads": [], "decisions": [], "watch_outs": []}
    grounded = dict(base, open_threads=[{"thread": "Started on the gadget", "tier": "interpreted", "evidence": {"turn": 1},
                                         "gloss": "Started on the gadget."}])
    invented = dict(base, open_threads=[{"thread": "Started on the gadget", "tier": "interpreted", "evidence": {"turn": 1},
                                         "gloss": "the gadget requires a new datacenter in Iowa and a fresh KMS key"}])
    e_g, e_i = shedmod.extractiveness(grounded, tl, []), shedmod.extractiveness(invented, tl, [])
    check("extractiveness: an invented gloss scores below a grounded one", e_g == 1.0 and e_i is not None and e_i < e_g, (e_g, e_i))

    # 14. progress / files_touched / first_commit_seconds are THIS session's — branch-scoped — and under the project
    # root (adversarial pass 2026-09-25: a peer's trunk commit became a session's first commit; `garage/` from the
    # monorepo root sat in its files_touched)
    mono = os.path.join(tmp, "mono")
    proj = os.path.join(mono, "proj")
    os.makedirs(proj)
    fresh_repo(mono)
    open(os.path.join(proj, "p.txt"), "w").write("p\n")
    git(mono, "add", "proj/p.txt")
    git(mono, "commit", "-q", "-m", "proj: base")
    git(mono, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(mono, "checkout", "-q", "-b", "session-a")
    ta = transcript(proj, "work on proj", "Working.")  # 2026-09-23 timestamps: every commit below is inside the window
    open(os.path.join(proj, "p.txt"), "a").write("a\n")
    git(mono, "add", "proj/p.txt")
    git(mono, "commit", "-q", "-m", "session-a: proj change")
    open(os.path.join(mono, "sibling.txt"), "w").write("s\n")
    git(mono, "add", "sibling.txt")
    git(mono, "commit", "-q", "-m", "session-a: sibling change outside proj")
    git(mono, "checkout", "-q", "main")
    open(os.path.join(proj, "peer.txt"), "w").write("x\n")
    git(mono, "add", "proj/peer.txt")
    git(mono, "commit", "-q", "-m", "peer: trunk commit in the window")
    git(mono, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(mono, "checkout", "-q", "session-a")
    sh(["handoff", "--root", proj, "--transcript", ta, "--sid8", "scp00001"], proj)
    d = json.load(open(os.path.join(proj, "handoff/drafts/scp00001.json")))
    subj = " | ".join(d["progress"])
    check("scope: a peer's trunk commit in the window is not this session's progress", "peer:" not in subj and "session-a: proj change" in subj, d["progress"])
    check("scope: a commit outside the project root is not progress", "sibling" not in subj, d["progress"])
    check("scope: files_touched stays under the project root", "proj/p.txt" in d["files_touched"] and not any("sibling" in f for f in d["files_touched"]), d["files_touched"])
    check("scope: first_commit_seconds is measured to THIS branch's first commit", isinstance(d["first_commit_seconds"], int), d["first_commit_seconds"])
    gb = next(b for b in d["basis"] if b["instrument"] == "git")
    check("scope: basis[git] names the branch range", "session-a" in gb.get("detail", "") and "origin/main..HEAD" in gb.get("detail", ""), gb)
    git(mono, "checkout", "-q", "main")
    sh(["handoff", "--root", proj, "--transcript", ta, "--sid8", "scp00002"], proj)
    d2 = json.load(open(os.path.join(proj, "handoff/drafts/scp00002.json")))
    gb2 = next(b for b in d2["basis"] if b["instrument"] == "git")
    check("scope: on trunk the basis says ANY session in the window", "ANY session" in gb2.get("detail", ""), gb2)

# ---------------------------------------------------------------- draft seat (fake endpoint, in-process)
import http.server
import socket
import threading

SEAT = {"reply": "", "requests": []}


class FakeSeat(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        SEAT["requests"].append({"path": self.path, "body": body})
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": SEAT["reply"]},
                                       "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


try:
    srv = http.server.HTTPServer(("127.0.0.1", 0), FakeSeat)
except OSError as e:
    # A sandbox that forbids binding loopback makes this instrument silent, not the seat broken:
    # fail by name so the count never reads as "all draft seat checks passed".
    check("draft seat: fake endpoint can bind 127.0.0.1 (else these checks did not run)", False, e)
    print("\n{} checks, {} failed".format(N[0], len(FAILS)))
    sys.exit(1)
threading.Thread(target=srv.serve_forever, daemon=True).start()
SPEC = "fake-3b@http://127.0.0.1:{}/v1".format(srv.server_address[1])

with tempfile.TemporaryDirectory() as tmp:
    fresh_repo(tmp)
    t = os.path.join(tmp, "t.jsonl")
    with open(t, "w") as f:
        for role, text in (("user", "make the importer handle empty files"),
                           ("assistant", "Done for CSV. The JSON importer still crashes on an empty file; that is next."),
                           ("user", "ok, and keep the old parser around"),
                           ("assistant", "Decided: the old parser stays behind a flag. Careful: the fixture dir is shared with CI.")):
            content = text if role == "user" else [{"type": "text", "text": text}]
            f.write(json.dumps({"type": role, "timestamp": "2026-09-23T20:00:00Z", "message": {"content": content}}) + "\n")
    sh(["handoff", "--root", tmp, "--transcript", t, "--sid8", "seat0001"], tmp)
    dp = os.path.join(tmp, "handoff/drafts/seat0001.json")
    d = json.load(open(dp))
    d["decisions"] = [{"text": "Ship CSV first.", "tier": "authored"}]  # a person's line the seat must keep
    json.dump(d, open(dp, "w"))
    metrics = os.path.join(tmp, "handoff/metrics.jsonl")

    def seat_rows():
        if not os.path.exists(metrics):
            return []
        return [json.loads(l) for l in open(metrics) if '"draft_seat"' in l]

    # (a) + (b): one grounded proposal per list, one fabricated, one claiming authored
    SEAT["reply"] = json.dumps({
        "open_threads": [{"thread": "The JSON importer still crashes on an empty file", "turn": 1,
                          "next_step": "fix JSON", "gloss": "JSON empty-file crash unfixed"},
                         {"thread": "The YAML importer is also broken", "turn": 1}],
        "decisions": [{"text": "the old parser stays behind a flag", "turn": 3, "tier": "authored"}],
        "watch_outs": [{"text": "the fixture dir is shared with CI", "evidence": {"turn": 3}},
                       {"text": "Careful: production is down", "turn": 3, "tier": "authored"}]})
    rc, out, err = sh(["draft", "--root", tmp, "--transcript", t, "--sid8", "seat0001", "--model", SPEC], tmp)
    d = json.load(open(dp))
    threads = [e.get("thread") for e in d["open_threads"]]
    check("draft seat: rc 0", rc == 0, out + err)
    check("draft seat: request went to /v1/chat/completions with the named model",
          SEAT["requests"] and SEAT["requests"][-1]["path"] == "/v1/chat/completions"
          and SEAT["requests"][-1]["body"].get("model") == "fake-3b", SEAT["requests"][-1:])
    check("draft seat (a): a grounded proposal lands in the draft, tier interpreted, evidence turn",
          d["open_threads"] and d["open_threads"][0] == {"thread": "The JSON importer still crashes on an empty file",
                                                         "next_step": "fix JSON", "gloss": "JSON empty-file crash unfixed",
                                                         "tier": "interpreted", "evidence": {"turn": 1}}, d["open_threads"])
    check("draft seat (b): an ungrounded proposal is dropped, never written",
          "The YAML importer is also broken" not in threads and not any("production" in (e.get("text") or "") for e in d["watch_outs"]),
          d)
    check("draft seat: a model cannot claim authored — its tier is set to interpreted",
          all(e.get("tier") == "interpreted" for e in d["watch_outs"]) and
          {"text": "the old parser stays behind a flag", "tier": "interpreted", "evidence": {"turn": 3}} in d["decisions"], d)
    check("draft seat: a person's authored entry survives the seat", {"text": "Ship CSV first.", "tier": "authored"} in d["decisions"], d["decisions"])
    rows = seat_rows()
    check("draft seat: metrics row {model, proposed, kept, rejected}",
          rows and rows[-1].get("model") == "fake-3b" and rows[-1].get("proposed") == 5
          and rows[-1].get("kept") == 3 and rows[-1].get("rejected") == 2, rows)

    # the gate the seat applies is the gate finalize applies: what it kept, finalize accepts
    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", t, "--sid8", "seat0001"], tmp)
    d_refreshed = json.load(open(dp))
    check("draft seat: a mechanical refresh keeps the seat's entries", d_refreshed["open_threads"] == d["open_threads"], d_refreshed["open_threads"])

    # (c) non-JSON reply / endpoint down / truncated JSON: draft byte-identical, rc 0, counted silent
    before = open(dp, "rb").read()
    for label, reply, spec in (("non-JSON reply", "Sure! Here are the threads: ...", SPEC),
                               ("truncated JSON", '{"open_threads": [{"thread": "The JSON', SPEC),
                               ("endpoint down", "", "fake-3b@http://127.0.0.1:{}/v1".format(free_port()))):
        SEAT["reply"] = reply
        n_rows = len(seat_rows())
        rc, out, err = sh(["draft", "--root", tmp, "--transcript", t, "--sid8", "seat0001", "--model", spec, "--timeout", "3"], tmp)
        rows = seat_rows()
        check("draft seat (c): {} leaves the draft byte-identical and exits 0".format(label),
              rc == 0 and open(dp, "rb").read() == before, out + err)
        check("draft seat (c): {} is recorded as silent, not as zero kept".format(label),
              len(rows) == n_rows + 1 and rows[-1].get("kept") is None and rows[-1].get("silent"), rows[-1:])

    # a fenced reply is still one object
    SEAT["reply"] = "```json\n" + json.dumps({"open_threads": [], "decisions": [], "watch_outs": []}) + "\n```"
    rc, out, _ = sh(["draft", "--root", tmp, "--transcript", t, "--sid8", "seat0001", "--model", SPEC], tmp)
    check("draft seat: a ```json fence around one object is read", "0 proposed" in out, out)

    rc, out, _ = sh(["handoff", "--root", tmp, "--transcript", t, "--sid8", "seat0001", "--finalize"], tmp)
    check("draft seat: finalize accepts what survives the seat", rc == 0 and "handoff/sessions/" in out, out)

    # Hooks: the seat runs once, at SessionEnd, never on Stop (Stop fires every turn; a real small
    # model takes 15-20 s a call)
    SEAT["reply"] = json.dumps({"open_threads": [{"thread": "The JSON importer still crashes on an empty file", "turn": 1}]})
    env = {k: v for k, v in os.environ.items() if k not in ("SHED_MODEL", "SHED_ENDPOINT")}

    def hook(event, sid, extra_env=None):
        p = json.dumps({"session_id": sid + "xxxx", "transcript_path": t, "cwd": tmp})
        return subprocess.run([PY, SHED, "hook", event], cwd=tmp, input=p, capture_output=True, text=True,
                              env=dict(env, **(extra_env or {})))

    def archive(sid):
        a = [f for f in os.listdir(os.path.join(tmp, "handoff/sessions")) if f.endswith("__{}.json".format(sid))]
        return json.load(open(os.path.join(tmp, "handoff/sessions", a[0]))) if a else None

    n_req, n_rows = len(SEAT["requests"]), len(seat_rows())
    r = hook("stop", "seat0002", {"SHED_MODEL": SPEC})
    check("draft seat: Stop hook never calls the model, even with SHED_MODEL set",
          r.returncode == 0 and len(SEAT["requests"]) == n_req and len(seat_rows()) == n_rows, r.stdout + r.stderr)
    r = hook("session-end", "seat0002")
    a = archive("seat0002")
    check("draft seat: SessionEnd without SHED_MODEL finalizes with no call",
          r.returncode == 0 and a is not None and a["open_threads"] == [] and len(SEAT["requests"]) == n_req, r.stdout + r.stderr)
    hook("stop", "seat0003")
    r = hook("session-end", "seat0003", {"SHED_MODEL": SPEC})
    a = archive("seat0003")
    check("draft seat: SessionEnd with SHED_MODEL seats once, then finalizes what the gate kept",
          r.returncode == 0 and a is not None and len(SEAT["requests"]) == n_req + 1
          and [e.get("thread") for e in a["open_threads"]] == ["The JSON importer still crashes on an empty file"],
          r.stdout + r.stderr + json.dumps(a and a["open_threads"]))
    r = hook("session-end", "seat0004", {"SHED_MODEL": "fake-3b@http://127.0.0.1:{}/v1".format(free_port()),
                                         "SHED_MODEL_TIMEOUT": "2"})
    check("draft seat: SessionEnd with a dead endpoint still finalizes, rc 0",
          r.returncode == 0 and archive("seat0004") is not None, r.stdout + r.stderr)
    # a richer archive already landed (a /handoff skill): shed stands down BEFORE spending a model call
    hook("stop", "seat0005")
    json.dump({"sid8": "seat0005", "source": "skill", "ended_at": "2099-01-01T00:00:00Z"},
              open(os.path.join(tmp, "handoff/sessions/2099-01-01T00-00-00Z__seat0005.json"), "w"))
    n_req = len(SEAT["requests"])
    r = hook("session-end", "seat0005", {"SHED_MODEL": SPEC})
    check("draft seat: stand-down comes before the seat — no model call for a record shed will not write",
          r.returncode == 0 and "stood down" in r.stdout and len(SEAT["requests"]) == n_req, r.stdout + r.stderr)

srv.shutdown()

# 12. finalize dating + trunk-aware stand-down (measured 2026-09-28: a backlog finalized in one
# second stamped 18 sessions as "now", and 4 of them duplicated a /handoff archive already on trunk)
with tempfile.TemporaryDirectory() as mono:
    fresh_repo(mono)
    proj = os.path.join(mono, "proj")
    os.makedirs(os.path.join(proj, "handoff", "sessions"))
    tt = transcript(proj, "an old session", "Old work finished.", minute=0)  # turns at 2026-09-23T20:00/20:01Z

    # 12a. the archive is dated by the transcript's last turn, not by when finalize ran
    rc, out, _ = sh(["handoff", "--root", proj, "--transcript", tt, "--sid8", "dat00001", "--finalize"], proj)
    names = [f for f in os.listdir(os.path.join(proj, "handoff/sessions")) if f.endswith("__dat00001.json")]
    check("dating: finalize rc0", rc == 0, out)
    check("dating: filename stamped by the last turn", names == ["2026-09-23T20-01-00Z__dat00001.json"], names)
    a = json.load(open(os.path.join(proj, "handoff/sessions", names[0]))) if names else {}
    check("dating: ended_at is the last turn", a.get("ended_at") == "2026-09-23T20:01:00Z", a.get("ended_at"))

    # 12b. a richer archive that exists only on trunk (published by pushing a sha) makes shed stand down
    rich_rel = "proj/handoff/sessions/2026-09-23T21-00-00Z__trk00001.json"
    with open(os.path.join(mono, rich_rel), "w") as f:
        json.dump({"ended_at": "2026-09-23T21:00:00Z", "source": "handoff-skill", "tldr": "rich"}, f)
    git(mono, "add", rich_rel)
    git(mono, "commit", "-q", "-m", "publish rich archive")
    git(mono, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(mono, "rm", "-q", rich_rel)
    git(mono, "commit", "-q", "-m", "shared checkout lags trunk")
    rc, out, _ = sh(["handoff", "--root", proj, "--transcript", tt, "--sid8", "trk00001", "--finalize"], proj)
    check("trunk stand-down: message names the trunk archive", "stood down" in out and "origin/main:" in out, out)
    check("trunk stand-down: no shed archive written", not [f for f in os.listdir(os.path.join(proj, "handoff/sessions"))
                                                          if f.endswith("__trk00001.json")])

    # 12c. a shed-sourced archive on trunk is NOT a richer record — finalize proceeds
    shed_rel = "proj/handoff/sessions/2026-09-23T21-00-00Z__trk00002.json"
    with open(os.path.join(mono, shed_rel), "w") as f:
        json.dump({"ended_at": "2026-09-23T21:00:00Z", "source": "shed"}, f)
    git(mono, "add", shed_rel)
    git(mono, "commit", "-q", "-m", "a shed archive on trunk")
    git(mono, "update-ref", "refs/remotes/origin/main", "HEAD")
    rc, out, _ = sh(["handoff", "--root", proj, "--transcript", tt, "--sid8", "trk00002", "--finalize"], proj)
    check("trunk stand-down: a shed archive on trunk does not stop finalize", "stood down" not in out and rc == 0, out)

    # 12d. orient shares the predicate: a draft whose richer archive is only on trunk is not advertised
    rich3_rel = "proj/handoff/sessions/2026-09-23T21-00-00Z__trk00003.json"
    with open(os.path.join(mono, rich3_rel), "w") as f:
        json.dump({"ended_at": "2026-09-23T21:00:00Z", "source": "handoff-skill", "tldr": "rich"}, f)
    git(mono, "add", rich3_rel)
    git(mono, "commit", "-q", "-m", "publish another rich archive")
    git(mono, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(mono, "rm", "-q", rich3_rel)
    git(mono, "commit", "-q", "-m", "shared checkout lags trunk again")
    os.makedirs(os.path.join(proj, "handoff", "drafts"), exist_ok=True)
    for s in ("trk00003", "trk00004"):  # trk00004 has no archive anywhere: the in-test control
        json.dump({"sid8": s, "draft": True, "started_at": "2026-09-23T20:00:00Z", "tldr": "A draft."},
                  open(os.path.join(proj, "handoff/drafts/{}.json".format(s)), "w"))
    rc, out, _ = sh(["orient", "--root", proj, "--sid8", "obs00001", "--max-lines", "0"], proj)
    check("trunk stand-down: orient hides a draft only trunk's archive supersedes",
          "Unfinalized draft from trk00003" not in out and rc == 0, out)
    check("trunk stand-down: orient still shows a draft nothing supersedes",
          "Unfinalized draft from trk00004" in out, out)

print("\n{} checks, {} failed".format(N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
