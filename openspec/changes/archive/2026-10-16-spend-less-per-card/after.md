# After: PCME dashboard waves 1.5 + 2 (15–16 Oct 2026)

Generated with (from the `wave-2-fixes` worktree, on a `/tmp` copy of the export so nothing is
written next to the project):

```
cp -R ~/Downloads/pcme_dashboard/.claude/worktrees/groom-waves-1-5-2-4a201c/.orchestra/logs-export /tmp/wave2-evidence/export
python3.12 -m orchestra.cli usage /tmp/wave2-evidence/export --markdown --json /tmp/wave2-evidence/after.json
```

Source: `logs-export/` (37 agent sessions for T15–T33 plus the interactive groom session; queue
state from `tasks-full.json`, `events.log` and `sessions.json`). Reading this layout needs
wave-2-fixes fix 9. The copy was checked identical to the source with `diff -rq`.

**Acceptance check:** recorded (`tasks.cost_usd`), the CLI's final totals and the token-priced
totals all come to **$17.40**. In wave 1 they were $30.41, $23.21 and $22.72.

Everything this run cost was agent work on a different set of cards than wave 1 (the coaching
handout addendum, ingestion and validation instead of the dashboard foundations), with 7 cards
still pending and T17 blocked. Read the comparison as "same project, same pipeline, after the
fixes", not as a controlled experiment.

## Comparison with `baseline.md`

| | wave 1 (baseline) | this run (after) | change |
|---|---|---|---|
| actual spend (CLI totals) | $23.21 | $17.40 | |
| recorded by Orchestra's budget | $30.41 (+32%) | $17.40 (exact) | fix 0 works: resumes no longer re-count |
| merged cards | 11 | 10 | |
| **cost per merged card** | **$2.11** | **$1.74** | −18% |
| **spend on cancelled/blocked cards** | **$6.08 (26%)**: T01, T04, T05 | **$4.22 (24%)**: T20 $3.31 (cancelled → split), T17 $0.91 (blocked) | about the same share; both cards were unsized (`estimated_lines` 0), see wave-2-fixes fix 1 |
| **cold-start cache writes** | 720k at session start (16.4k/session), 0 mid-session | 598k at session start (16.2k/session), 141k mid-session in 3 sessions | per-session cold start unchanged; the mid-session re-writes (T15 worker 54k, T17 worker 62k, T33 test writer 24k; ≈ $0.55) are the 5-minute TTL's price after long pauses, near the $0.65 predicted for wave 1 |
| 1-hour cache writes (agents) | 1.66M | 0 | fix 8 works; the same 1.64M writes at the 1-hour price would have cost **$4.21 more** |
| **peak context (agents)** | 145k (T05 worker, cancelled) | 133k (T15 worker, merged after a hand split) | the two largest were again oversized cards (T15, T20); median worker peak 45k |
| peak context (interactive) | 296k | 168k | |
| **first-round review pass rate** | 9/11 (82%); T02, T10 rejected | 9/10 (90%); T19 rejected | |
| gate-fix trim rounds | 6 | 0 | fix 2 works: the size check stops workers before the gate |
| validator sessions | 8 ($1.71) | 1 ($0.12) | fix 3 works |
| tool output over 8,000 chars | 56 (44 Bash, 12 Read; incl. interactive) | 19 agent (1 Bash, 18 Read) + 4 interactive; 53 Bash results already capped | fix 4 works; the 1 uncapped Bash was a reviewer (wave-2-fixes 9b) |

### Where the money went this time

- **Test writers are the largest single item:** 6 Opus sessions, $5.60 (32%), against $1.32 in
  wave 1. Of this, T20's $1.52 was thrown away when the card was split, and T30–T33's $3.65 rebuilt
  the same fixtures one card at a time. wave-2-fixes fix 6 hands split children the parent's
  tests and gives test writers a size check. Moving them to Sonnet (6b, held) would have cost
  $3.29 for the same tokens.
- **Unsized cards are still the main waste.** T20 (862 lines) and T17 (1,762, mostly a copied
  JSON file) were groomed without estimates, because PCME's committed groom skill predated the
  sizing work. See wave-2-fixes fixes 1 and 7.
- **Resume rounds:** 2 decision resumes (490k tokens) and 1 revise (64k); no gate-fix rounds.

### Items spend-less-per-card couldn't check in its smoke test

- **Resumed-session cost deltas:** exercised by T16 and T17's decision resumes and T19's revise.
  Recorded equals the CLI exactly.
- **Single validator:** exercised by T19 (1 session for its findings).
- **Oversize straight to triage:** not exercised through the gate. The in-worker size check
  stopped T15, T17 and T20 first, and their workers reported "blocked". T17 and T20 then went to
  triage (#139, #151). T15 had already used its one triage on a stale branch (#38–41), so it went
  to the human (#70).
- **Reviewer note order:** not measurable from transcripts.

## Full report


## Totals (Orchestra agents)

37 sessions · 406 turns · 19.34M tokens (cache read 90%, cache write 8%, 1-hour writes 0k) · priced from transcripts $17.40 · CLI's final totals $17.40 · recorded in tasks.cost_usd $17.40

Cold starts: 598k written at session start, 141k mid-session.

Interactive sessions (not in Orchestra's spend): 1 · 50 turns · 7.14M tokens · peak context 168k · priced $3.16

## Cards

| card | status | recorded | CLI | priced | tokens | c.write | cold | peak | sessions (role:n/turns) | gate fails | reviews | 1st review | revise | resumed tok |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T15 | merged | $4.04 | $4.04 | $4.04 | 5.72M | 283k | 88k | 133k | revi:1/4 tria:1/2 work:3/75 | 0 | 1 | approved | 0 | 0k |
| T16 | merged | $0.22 | $0.22 | $0.22 | 132k | 45k | 25k | 20k | revi:1/3 work:1/4 | 0 | 1 | approved | 0 | 56k |
| T17 | blocked | $0.91 | $0.91 | $0.91 | 999k | 155k | 26k | 82k | tria:1/5 work:1/19 | 0 | 0 |  | 0 | 434k |
| T18 | merged | $0.77 | $0.77 | $0.77 | 714k | 78k | 37k | 39k | revi:1/2 test:1/11 work:1/12 | 0 | 1 | approved | 0 | 0k |
| T19 | merged | $0.76 | $0.76 | $0.76 | 676k | 113k | 76k | 32k | revi:2/9 tria:1/2 vali:1/4 work:2/16 | 0 | 2 | rejected | 1 | 64k |
| T20 | cancelled → T30, T31, T32, T33 | $3.31 | $3.31 | $3.31 | 3.45M | 237k | 42k | 107k | test:1/28 tria:1/7 work:1/24 | 0 | 0 |  | 0 | 0k |
| T21 | pending | $0.00 | - | - | 0k | 0k | 0k | 0k |  | 0 | 0 |  | 0 | 0k |
| T22 | pending | $0.00 | - | - | 0k | 0k | 0k | 0k |  | 0 | 0 |  | 0 | 0k |
| T23 | merged | $0.49 | $0.49 | $0.49 | 484k | 64k | 47k | 51k | revi:1/3 work:1/14 | 0 | 1 | approved | 0 | 0k |
| T24 | pending | $0.00 | - | - | 0k | 0k | 0k | 0k |  | 0 | 0 |  | 0 | 0k |
| T25 | pending | $0.00 | - | - | 0k | 0k | 0k | 0k |  | 0 | 0 |  | 0 | 0k |
| T26 | pending | $0.00 | - | - | 0k | 0k | 0k | 0k |  | 0 | 0 |  | 0 | 0k |
| T27 | pending | $0.00 | - | - | 0k | 0k | 0k | 0k |  | 0 | 0 |  | 0 | 0k |
| T28 | pending | $0.00 | - | - | 0k | 0k | 0k | 0k |  | 0 | 0 |  | 0 | 0k |
| T29 | merged | $0.50 | $0.50 | $0.50 | 604k | 70k | 37k | 40k | revi:1/10 work:1/11 | 0 | 1 | approved | 0 | 0k |
| T30 | merged | $0.99 | $0.99 | $0.99 | 867k | 101k | 48k | 45k | revi:1/2 test:1/10 work:1/14 | 0 | 1 | approved | 0 | 0k |
| T31 | merged | $1.77 | $1.77 | $1.77 | 1.80M | 160k | 59k | 74k | revi:1/2 test:1/19 work:1/18 | 0 | 1 | approved | 0 | 0k |
| T32 | merged | $1.66 | $1.66 | $1.66 | 1.99M | 150k | 48k | 73k | revi:1/2 test:1/22 work:1/17 | 0 | 1 | approved | 0 | 0k |
| T33 | merged | $1.99 | $1.99 | $1.99 | 1.90M | 185k | 65k | 93k | revi:1/2 test:1/19 work:1/14 | 0 | 1 | approved | 0 | 0k |

14 cancelled cards with no sessions (T01–T14) not shown.

## By role

| role | sessions | turns | tokens | c.read | c.write | cold start | priced |
|---|---|---|---|---|---|---|---|
| worker | 15 | 238 | 12.44M | 11.40M | 832k | 172k | $9.47 |
| interactive | 1 | 50 | 7.14M | 6.75M | 342k | 29k | $3.16 |
| test_writer | 6 | 109 | 5.43M | 4.90M | 392k | 67k | $5.60 |
| reviewer | 11 | 39 | 1.17M | 812k | 338k | 310k | $1.62 |
| triage | 4 | 16 | 245k | 172k | 61k | 34k | $0.58 |
| validator | 1 | 4 | 65k | 46k | 18k | 15k | $0.12 |

## By model

| model | sessions | turns | tokens | c.read | c.write | cold start | priced |
|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | 25 | 328 | 16.76M | 15.27M | 1.17M | 329k | $15.29 |
| claude-sonnet-5-5 | 13 | 128 | 9.72M | 8.81M | 815k | 298k | $5.26 |

## Rounds started by a resume prompt

| prompt | rounds | tokens |
|---|---|---|
| decision | 2 | 490k |
| revise | 1 | 64k |

## Sessions

| session | card | role | model | turns | tokens | c.write | peak | priced | CLI | resumed by | synthetic |
|---|---|---|---|---|---|---|---|---|---|---|---|
| groom-session/3430591c-f4d7-4019-89ad-20 | - | interactive | sonnet-5-5 | 50 | 7.14M | 342k | 168k | $3.16 | $2.75 |  | 0 |
| orchestra-worktrees-T15/f2779d5b-ae0a-49 | T15 | worker | opus-5-5 | 30 | 3.02M | 123k | 133k | $2.09 | $2.09 |  | 0 |
| orchestra-worktrees-T15/87c1aa00-5e67-46 | T15 | worker | opus-5-5 | 38 | 2.39M | 93k | 93k | $1.59 | $1.59 |  | 0 |
| orchestra-worktrees-T20/7a7124ba-cca7-4a | T20 | test_writer | opus-5-5 | 28 | 1.70M | 101k | 101k | $1.52 | $1.52 |  | 0 |
| orchestra-worktrees-T20/4465ec23-2e70-43 | T20 | worker | opus-5-5 | 24 | 1.62M | 107k | 107k | $1.49 | $1.49 |  | 0 |
| orchestra-worktrees-T33/4c989271-56e9-4d | T33 | test_writer | opus-5-5 | 19 | 1.18M | 83k | 93k | $1.26 | $1.26 |  | 0 |
| orchestra-worktrees-T32/e61f37a6-df6d-45 | T32 | test_writer | opus-5-5 | 22 | 1.07M | 63k | 73k | $0.95 | $0.95 |  | 0 |
| orchestra-worktrees-T17/0292b5ad-80b2-40 | T17 | worker | sonnet-5-5 | 19 | 930k | 139k | 82k | $0.73 | $0.73 | decision | 1 |
| orchestra-worktrees-T31/af90216b-0cca-45 | T31 | test_writer | opus-5-5 | 19 | 863k | 65k | 74k | $0.98 | $0.98 |  | 0 |
| orchestra-worktrees-T32/f79f675e-3473-41 | T32 | worker | opus-5-5 | 17 | 855k | 51k | 61k | $0.61 | $0.61 |  | 0 |
| orchestra-worktrees-T31/b55cc842-8c61-40 | T31 | worker | opus-5-5 | 18 | 849k | 50k | 60k | $0.67 | $0.67 |  | 0 |
| orchestra-worktrees-T33/e9fda2a6-bb29-4b | T33 | worker | opus-5-5 | 14 | 640k | 59k | 59k | $0.61 | $0.61 |  | 0 |
| orchestra-worktrees-T30/ec66e269-dfbb-47 | T30 | worker | opus-5-5 | 14 | 518k | 35k | 45k | $0.46 | $0.46 |  | 0 |
| orchestra-worktrees-T29/51186ca3-065b-44 | T29 | worker | sonnet-5-5 | 11 | 349k | 40k | 40k | $0.23 | $0.23 |  | 0 |
| orchestra-worktrees-T18/d6ace031-b24c-44 | T18 | worker | opus-5-5 | 12 | 344k | 23k | 33k | $0.28 | $0.28 |  | 0 |
| orchestra-worktrees-T23/25f4c4f4-c410-4b | T23 | worker | sonnet-5-5 | 14 | 334k | 21k | 30k | $0.21 | $0.21 |  | 0 |
| orchestra-worktrees-T18/2ca520ac-31ef-42 | T18 | test_writer | opus-5-5 | 11 | 322k | 39k | 39k | $0.44 | $0.44 |  | 0 |
| orchestra-worktrees-T30/603ff5e7-bc55-44 | T30 | test_writer | opus-5-5 | 10 | 298k | 41k | 41k | $0.46 | $0.46 |  | 0 |
| orchestra-worktrees-T19/941bbba2-022f-40 | T19 | worker | sonnet-5-5 | 11 | 291k | 22k | 32k | $0.16 | $0.16 | revise | 1 |
| orchestra-worktrees-T29/d3a45ebc-013f-4c | T29 | reviewer | opus-5-5 | 10 | 255k | 30k | 30k | $0.27 | $0.27 |  | 0 |
| orchestra-worktrees-T15/8d9e5a9b-2d20-48 | T15 | reviewer | sonnet-5-5 | 4 | 159k | 40k | 40k | $0.14 | $0.14 |  | 0 |
| orchestra-worktrees-T23/65cc884b-c140-47 | T23 | reviewer | opus-5-5 | 3 | 150k | 43k | 51k | $0.28 | $0.28 |  | 0 |
| orchestra-worktrees-T15/0d688bbb-12d8-45 | T15 | worker | opus-5-5 | 7 | 135k | 21k | 21k | $0.16 | $0.16 |  | 0 |
| orchestra-worktrees--triage/19d851a4-2f9 | T20 | triage | opus-5-5 | 7 | 134k | 29k | 29k | $0.29 | $0.29 |  | 0 |
| orchestra-worktrees-T19/f5ab8e59-391c-4f | T19 | reviewer | opus-5-5 | 5 | 113k | 26k | 26k | $0.20 | $0.20 |  | 0 |
| orchestra-worktrees-T19/b303748d-3942-42 | T19 | worker | sonnet-5-5 | 5 | 100k | 22k | 22k | $0.08 | $0.08 |  | 0 |
| orchestra-worktrees-T31/71e57864-17ae-4e | T31 | reviewer | sonnet-5-5 | 2 | 90k | 45k | 45k | $0.13 | $0.13 |  | 0 |
| orchestra-worktrees-T19/d77baf83-931c-41 | T19 | reviewer | opus-5-5 | 4 | 87k | 15k | 23k | $0.14 | $0.14 |  | 0 |
| orchestra-worktrees-T33/1ed27fe8-ec2f-44 | T33 | reviewer | sonnet-5-5 | 2 | 85k | 43k | 43k | $0.12 | $0.12 |  | 0 |
| orchestra-worktrees-T16/fbbe48e7-84ff-46 | T16 | worker | sonnet-5-5 | 4 | 73k | 26k | 19k | $0.09 | $0.09 | decision | 1 |
| orchestra-worktrees-T32/69a0916a-a433-41 | T32 | reviewer | sonnet-5-5 | 2 | 70k | 35k | 35k | $0.10 | $0.10 |  | 0 |
| orchestra-worktrees--triage/b9a6cb5c-1c9 | T17 | triage | opus-5-5 | 5 | 69k | 15k | 15k | $0.18 | $0.18 |  | 0 |
| orchestra-worktrees-T19/17772051-b21b-47 | T19 | validator | opus-5-5 | 4 | 65k | 18k | 18k | $0.12 | $0.12 |  | 0 |
| orchestra-worktrees-T16/ec9996dd-4223-45 | T16 | reviewer | opus-5-5 | 3 | 59k | 20k | 20k | $0.13 | $0.13 |  | 0 |
| orchestra-worktrees-T30/9cb53c62-f779-4a | T30 | reviewer | sonnet-5-5 | 2 | 51k | 25k | 25k | $0.07 | $0.07 |  | 0 |
| orchestra-worktrees-T18/db2c2e9b-51f1-44 | T18 | reviewer | sonnet-5-5 | 2 | 47k | 16k | 24k | $0.05 | $0.05 |  | 0 |
| orchestra-worktrees--triage/a550dc14-682 | T15 | triage | opus-5-5 | 2 | 22k | 6k | 11k | $0.05 | $0.05 |  | 0 |
| orchestra-worktrees--triage/09b8139a-e10 | T19 | triage | opus-5-5 | 2 | 20k | 10k | 10k | $0.07 | $0.07 |  | 0 |

## Tool output over 8,000 chars

19 agent results ({'Bash': 1, 'Read': 18}) · 154,855 chars over the cap · ~684k context tokens if each was re-read on every later turn
Not counted: 53 already cut by the Bash cap, 4 in interactive sessions.
| chars | tool | session | later turns |
|---|---|---|---|
| 30,340 | Read | orchestra-worktrees-T20/4465ec23-2e70-43 | 18 |
| 26,865 | Read | orchestra-worktrees-T32/f79f675e-3473-41 | 15 |
| 26,865 | Read | orchestra-worktrees-T33/4c989271-56e9-4d | 17 |
| 23,970 | Read | orchestra-worktrees-T15/f2779d5b-ae0a-49 | 28 |
| 20,835 | Read | groom-session/3430591c-f4d7-4019-89ad-20 | 49 |
| 18,370 | Read | orchestra-worktrees-T33/4c989271-56e9-4d | 17 |
| 18,341 | Bash | groom-session/3430591c-f4d7-4019-89ad-20 | 48 |
| 16,713 | Read | orchestra-worktrees-T15/f2779d5b-ae0a-49 | 28 |
| 16,713 | Read | orchestra-worktrees-T29/51186ca3-065b-44 | 10 |
| 15,943 | Read | orchestra-worktrees--triage/19d851a4-2f9 | 4 |
