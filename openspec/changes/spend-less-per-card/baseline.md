# Baseline: PCME dashboard wave 1 (14–15 Oct 2026)

Generated with:

```
orchestra usage ~/Downloads/pcme_dashboard/.claude/worktrees/orchestra-wave-1-groom-9eb1dc/.orchestra/export/orchestra-logs-pcme-wave1 --markdown
```

Source: the contents of `orchestra-logs-pcme-wave1.zip` (44 agent sessions for T01–T15 plus the
interactive session and its subagent; queue state from `state/tasks.json` and `state/events.json`).

**Acceptance check:** the "recorded" column (`tasks.cost_usd`) sums to **$30.41**, matching the brief.

## How to read the money columns

| column | meaning |
|---|---|
| recorded | `tasks.cost_usd`: what Orchestra's budget counted |
| CLI | the Claude Code CLI's own final cost per session (last `cost-state` record), summed per card |
| priced | tokens × `orchestra/pricing.py` (1-hour cache writes at 2× input) |

**recorded ≠ actual spend.** For every card, recorded equals the sum of the CLI's *cumulative*
session totals taken at each run end (checked card by card; T09 is the only exception, see below).
The SDK's `ResultMessage.total_cost_usd` (and `model_usage`) covers the whole session so far, and
`store.add_cost` adds it again after every resume (`orchestrator.py:710`, `agents.py:511`). So every
gate-fix, revise, usage-limit or decision resume re-counts all earlier spend in that session:

- Wave 1 actually cost **$23.21** by the CLI's final totals ($22.72 priced from tokens), not $30.41: **$7.47 (32%) over-counted**.
- The third `orchestra run` recorded $25.77 and stopped at its $25 budget ("run budget $25.00 reached",
  events.log line 149). Sessions that started in that run actually cost **$19.42**, so the budget
  stop fired about $5.60 early and T15 was left to be done by hand.
- The three cancelled cards (T01, T04, T05) cost **$6.08 (26%)**, not $10.98 (36%).
- T09 is under-counted by $0.27: its first run was interrupted ("recovered from interrupted run",
  events.log line 51) and that session's cost was never recorded.

Priced and CLI agree to the cent except T03's worker ($1.31 priced vs $1.78 CLI; the session hit
the usage limit mid-turn, unconfirmed whether the CLI charged the cut-off request) and T09 ($0.02).

## Definitions

- **tokens** = input + output + cache read + cache write, each assistant message counted once by message id.
- **cold** = cache written by a session's first turn (nothing reusable from an earlier session).
- **resumed tok** = tokens in rounds a resume prompt started (gate fix, reviewer revise, usage limit).
  `<synthetic>` entries cost nothing: 8 of 9 are the CLI's "No response requested." when Orchestra
  resumes a session, 1 is the CLI's usage-limit notice (T03).
- **sessions (role:n/turns)**: work = worker, revi = reviewer, vali = validator, test = test writer, tria = triage.
- Every cache write in this wave used the 1-hour TTL (2× input price); none used 5 minutes.

## Totals (Orchestra agents)

44 sessions · 518 turns · 26.28M tokens (cache read 92%, cache write 6%, 1-hour writes 1.66M) · priced from transcripts $22.72 · CLI's final totals $23.21 · recorded in tasks.cost_usd $30.41

Cold starts: 720k written at session start, 0k mid-session.

Interactive sessions (not in Orchestra's spend): 2 · 144 turns · 24.64M tokens · peak context 296k · priced $7.82

## Cards

| card | status | recorded | CLI | priced | tokens | c.write | cold | peak | sessions (role:n/turns) | gate fails | reviews | 1st review | revise | resumed tok |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T01 | cancelled → T06, T07, T08, T09 | $2.31 | $1.30 | $1.30 | 2.28M | 94k | 27k | 83k | tria:1/2 work:1/42 | 1 | 0 |  | 0 | 248k |
| T02 | merged | $3.96 | $3.24 | $3.23 | 2.22M | 296k | 206k | 49k | revi:3/15 vali:6/19 work:2/36 | 1 | 3 | rejected | 1 | 556k |
| T03 | merged | $4.03 | $3.29 | $2.82 | 2.28M | 186k | 78k | 75k | revi:1/2 test:1/18 work:1/24 | 0 | 1 | approved | 0 | 956k |
| T04 | cancelled → T10, T11 | $2.76 | $1.59 | $1.59 | 3.07M | 118k | 16k | 119k | tria:1/2 work:1/36 | 1 | 0 |  | 0 | 803k |
| T05 | cancelled → T12, T13, T14, T15 | $5.91 | $3.19 | $3.19 | 4.35M | 141k | 12k | 145k | tria:1/2 work:1/45 | 1 | 0 |  | 0 | 860k |
| T06 | merged | $0.60 | $0.48 | $0.48 | 474k | 49k | 30k | 25k | revi:1/4 work:2/19 | 1 | 1 | approved | 0 | 49k |
| T07 | merged | $0.64 | $0.64 | $0.64 | 489k | 64k | 47k | 50k | revi:1/4 work:1/11 | 0 | 1 | approved | 0 | 0k |
| T08 | merged | $0.80 | $0.80 | $0.80 | 724k | 79k | 47k | 57k | revi:1/6 work:1/13 | 0 | 1 | approved | 0 | 0k |
| T09 | merged | $0.86 | $1.13 | $1.11 | 1.17M | 101k | 31k | 40k | revi:1/8 work:3/30 | 0 | 1 | approved | 0 | 0k |
| T10 | merged | $3.04 | $2.06 | $2.06 | 2.59M | 178k | 101k | 65k | revi:2/24 vali:2/5 work:1/29 | 1 | 2 | rejected | 1 | 751k |
| T11 | merged | $1.16 | $1.16 | $1.16 | 1.51M | 95k | 43k | 56k | revi:1/12 work:1/21 | 0 | 1 | approved | 0 | 0k |
| T12 | merged | $0.65 | $0.65 | $0.65 | 438k | 46k | 22k | 40k | revi:1/2 work:1/13 | 0 | 1 | approved | 0 | 0k |
| T13 | merged | $1.83 | $1.83 | $1.83 | 2.30M | 109k | 30k | 95k | revi:1/2 work:1/33 | 0 | 1 | approved | 0 | 0k |
| T14 | merged | $1.86 | $1.86 | $1.86 | 2.38M | 106k | 30k | 92k | revi:1/3 work:1/36 | 0 | 1 | approved | 0 | 0k |
| T15 | cancelled | $0.00 | - | - | 0k | 0k | 0k | 0k |  | 0 | 0 |  | 0 | 0k |

## By role

| role | sessions | turns | tokens | c.read | c.write | cold start | priced |
|---|---|---|---|---|---|---|---|
| interactive | 2 | 144 | 24.64M | 24.02M | 503k | 29k | $7.82 |
| worker | 18 | 388 | 21.64M | 20.34M | 939k | 154k | $14.66 |
| reviewer | 14 | 82 | 3.14M | 2.64M | 448k | 364k | $4.55 |
| test_writer | 1 | 18 | 820k | 720k | 69k | 18k | $1.32 |
| validator | 8 | 24 | 612k | 423k | 180k | 162k | $1.71 |
| triage | 3 | 6 | 70k | 31k | 26k | 23k | $0.47 |

## By model

| model | sessions | turns | tokens | c.read | c.write | cold start | priced |
|---|---|---|---|---|---|---|---|
| claude-sonnet-5-5 | 19 | 390 | 35.95M | 34.49M | 1.14M | 243k | $14.57 |
| claude-opus-5-5 | 27 | 272 | 14.96M | 13.68M | 1.02M | 506k | $15.97 |

## Rounds started by a resume prompt

| prompt | rounds | tokens |
|---|---|---|
| gate_fix | 6 | 2.56M |
| revise | 2 | 704k |
| usage_limit | 1 | 956k |

## Sessions

| session | card | role | model | turns | tokens | c.write | peak | priced | CLI | resumed by | synthetic |
|---|---|---|---|---|---|---|---|---|---|---|---|
| groom-dispatch-review-session/f74aceaf-6 | - | interactive | sonnet-5-5 | 111 | 22.43M | 400k | 296k | $7.14 | $4.43 |  | 0 |
| T05/1b863f8a-a7b4-4f15-a2e7-c1f6360bcd5a | T05 | worker | opus-5-5 | 45 | 4.33M | 136k | 145k | $3.06 | $3.06 | gate_fix | 1 |
| T04/3ed9d4a2-9a4e-4d74-b108-e3f430d5136b | T04 | worker | sonnet-5-5 | 36 | 3.05M | 109k | 119k | $1.45 | $1.45 | gate_fix | 1 |
| T14/60517a15-35b5-421f-b596-1490135dc9ee | T14 | worker | opus-5-5 | 36 | 2.29M | 82k | 92k | $1.74 | $1.74 |  | 0 |
| T01/3d0d8210-4482-429f-b279-4c38e1ef3a91 | T01 | worker | sonnet-5-5 | 42 | 2.26M | 83k | 83k | $1.09 | $1.09 | gate_fix | 1 |
| T13/74b0ee39-74cf-4b20-9766-3c463664e3a3 | T13 | worker | opus-5-5 | 33 | 2.23M | 85k | 95k | $1.72 | $1.72 |  | 0 |
| groom-dispatch-review-session/f74aceaf-6 | - | interactive | sonnet-5-5 | 33 | 2.21M | 103k | 103k | $0.68 |  |  | 0 |
| T10/148186cb-8027-4db7-a0cc-bb1f85536730 | T10 | worker | sonnet-5-5 | 29 | 1.54M | 56k | 65k | $0.67 | $0.67 | gate_fix,revise | 2 |
| T03/bf3d9716-9310-46fb-bbcb-477e7c814d5a | T03 | worker | opus-5-5 | 24 | 1.37M | 75k | 75k | $1.31 | $1.78 | usage_limit | 1 |
| T11/e15d489b-f436-4857-8dd8-380feb689d05 | T11 | worker | sonnet-5-5 | 21 | 887k | 46k | 56k | $0.53 | $0.53 |  | 0 |
| T03/aa052849-1516-4462-84f3-23faf4fc0864 | T03 | test_writer | opus-5-5 | 18 | 820k | 69k | 69k | $1.33 | $1.33 |  | 0 |
| T02/cfbb48b7-318a-42b7-9ecd-3de1ba4509d2 | T02 | worker | sonnet-5-5 | 22 | 805k | 39k | 49k | $0.52 | $0.52 | gate_fix,revise | 2 |
| T10/db2fb48c-7ae2-449d-be03-7caa8d8e6114 | T10 | reviewer | opus-5-5 | 18 | 702k | 41k | 49k | $0.61 | $0.61 |  | 0 |
| T11/96eed5b9-2b6d-4a43-b597-a92fc32be37d | T11 | reviewer | opus-5-5 | 12 | 620k | 49k | 56k | $0.63 | $0.63 |  | 0 |
| T02/93c08331-1a76-4936-9db8-996555ca9c13 | T02 | worker | sonnet-5-5 | 14 | 429k | 38k | 40k | $0.37 | $0.38 |  | 0 |
| T08/831d5b72-f855-4df4-859e-130b37ee85b0 | T08 | worker | sonnet-5-5 | 13 | 400k | 30k | 40k | $0.29 | $0.29 |  | 0 |
| T12/3c9a1768-f204-495b-8bbe-709468f36f93 | T12 | worker | opus-5-5 | 13 | 391k | 30k | 40k | $0.57 | $0.57 |  | 0 |
| T09/5fc84140-8c20-4bd6-a7ed-05864e042450 | T09 | worker | sonnet-5-5 | 11 | 359k | 27k | 40k | $0.29 | $0.29 |  | 0 |
| T09/06bb2c61-857e-4ef1-9492-78ac93200019 | T09 | worker | sonnet-5-5 | 10 | 327k | 30k | 40k | $0.27 | $0.27 |  | 0 |
| T08/3bab71d3-5c05-4e84-b117-d529cd7753c8 | T08 | reviewer | opus-5-5 | 6 | 324k | 49k | 57k | $0.51 | $0.51 |  | 0 |
| T07/2a7c2308-8259-4f78-895e-08fab3c06ee0 | T07 | worker | sonnet-5-5 | 11 | 289k | 21k | 31k | $0.22 | $0.22 |  | 0 |
| T09/023fd017-ae3b-4242-833e-6825292f0063 | T09 | worker | sonnet-5-5 | 9 | 282k | 23k | 37k | $0.26 | $0.28 |  | 0 |
| T02/60ff88d1-4ae8-46f0-bfa5-46565c364cdc | T02 | reviewer | opus-5-5 | 7 | 258k | 32k | 40k | $0.44 | $0.44 |  | 0 |
| T06/1dab6bb9-7e67-489e-8b05-ef49a7b011a2 | T06 | worker | sonnet-5-5 | 11 | 231k | 14k | 24k | $0.14 | $0.14 | gate_fix | 1 |
| T09/9f9aa4a7-ff40-4299-a7ed-a7cc5ef19f7a | T09 | reviewer | opus-5-5 | 8 | 207k | 21k | 29k | $0.29 | $0.29 |  | 0 |
| T10/81e7c863-72fc-4762-92d6-e8797c6efb98 | T10 | reviewer | opus-5-5 | 6 | 207k | 28k | 36k | $0.30 | $0.30 |  | 0 |
| T07/e398d623-d604-4822-9492-e7492c3b55cf | T07 | reviewer | opus-5-5 | 4 | 200k | 43k | 50k | $0.41 | $0.41 |  | 0 |
| T06/04d99e5e-1ef3-442b-af3b-e6dff3d6bb3f | T06 | worker | sonnet-5-5 | 8 | 170k | 16k | 25k | $0.13 | $0.13 |  | 0 |
| T02/ba9acafa-4940-45ac-a57e-072a10a10112 | T02 | validator | opus-5-5 | 6 | 155k | 25k | 29k | $0.26 | $0.26 |  | 0 |
| T02/06e6d6e4-c705-44dc-a549-db8af216d28b | T02 | validator | opus-5-5 | 6 | 149k | 26k | 26k | $0.28 | $0.28 |  | 0 |
| T02/0ad8a46a-d86b-4f96-a67f-a3aa2457574b | T02 | reviewer | opus-5-5 | 4 | 128k | 34k | 34k | $0.38 | $0.38 |  | 0 |
| T02/642994ea-629e-44ee-b2a3-5fcee3c8bf63 | T02 | reviewer | opus-5-5 | 4 | 126k | 26k | 33k | $0.29 | $0.29 |  | 0 |
| T02/f4f8668a-73bb-4d3c-b3c9-c795358a735b | T02 | validator | opus-5-5 | 4 | 97k | 20k | 25k | $0.20 | $0.20 |  | 0 |
| T14/fd824624-9ce8-4a9e-880c-8dd8e9386881 | T14 | reviewer | sonnet-5-5 | 3 | 96k | 24k | 32k | $0.12 | $0.12 |  | 0 |
| T10/c9af9e87-e815-4466-98ef-7e098428f8c8 | T10 | validator | opus-5-5 | 3 | 86k | 26k | 30k | $0.24 | $0.24 |  | 0 |
| T03/4860a04d-b326-4949-ad66-732441a11228 | T03 | reviewer | sonnet-5-5 | 2 | 85k | 43k | 43k | $0.18 | $0.18 |  | 0 |
| T06/c3c070b9-4619-4fa2-84be-a34b427b71c3 | T06 | reviewer | opus-5-5 | 4 | 73k | 19k | 19k | $0.21 | $0.21 |  | 0 |
| T13/449dfdb4-3cc1-4a94-9993-5f1db060f506 | T13 | reviewer | sonnet-5-5 | 2 | 64k | 24k | 32k | $0.12 | $0.12 |  | 0 |
| T10/bca26269-9c80-40c0-8957-4797b1d9823b | T10 | validator | opus-5-5 | 2 | 54k | 27k | 27k | $0.24 | $0.24 |  | 0 |
| T12/970f9bb7-d1cc-4f2b-b137-6385678bb8cf | T12 | reviewer | sonnet-5-5 | 2 | 47k | 16k | 23k | $0.07 | $0.07 |  | 0 |
| triage/69b4262f-26d9-45b7-9e7a-b102fdb40 | T01 | triage | opus-5-5 | 2 | 27k | 12k | 12k | $0.21 | $0.21 |  | 0 |
| T02/b7a4304f-7dac-4984-af09-d747c31b6946 | T02 | validator | opus-5-5 | 1 | 24k | 19k | 23k | $0.17 | $0.17 |  | 0 |
| T02/6c23b88e-614f-4d5b-832e-614a55d38f62 | T02 | validator | opus-5-5 | 1 | 24k | 19k | 23k | $0.16 | $0.16 |  | 0 |
| T02/a7b03239-f891-407d-ba97-02adc939641a | T02 | validator | opus-5-5 | 1 | 23k | 18k | 22k | $0.16 | $0.16 |  | 0 |
| triage/bdca1a05-dbb6-476c-896f-82207771f | T05 | triage | opus-5-5 | 2 | 22k | 5k | 9k | $0.13 | $0.13 |  | 0 |
| triage/a78a08c8-feaf-4571-83c6-853961b8a | T04 | triage | opus-5-5 | 2 | 20k | 9k | 9k | $0.14 | $0.14 |  | 0 |

## Tool output over 8,000 chars

56 results ({'Bash': 44, 'Read': 12}) · 252,888 chars over the cap · ~2.13M context tokens if each was re-read on every later turn
| chars | tool | session | later turns |
|---|---|---|---|
| 28,824 | Read | T13/74b0ee39-74cf-4b20-9766-3c463664e3a3 | 26 |
| 24,139 | Read | T05/1b863f8a-a7b4-4f15-a2e7-c1f6360bcd5a | 35 |
| 23,965 | Bash | T14/60517a15-35b5-421f-b596-1490135dc9ee | 34 |
| 21,834 | Bash | T04/3ed9d4a2-9a4e-4d74-b108-e3f430d5136b | 35 |
| 20,835 | Read | groom-dispatch-review-session/f74aceaf-6 | 110 |
| 18,273 | Bash | T09/06bb2c61-857e-4ef1-9492-78ac93200019 | 9 |
| 18,060 | Read | T10/148186cb-8027-4db7-a0cc-bb1f85536730 | 26 |
| 15,893 | Bash | T05/1b863f8a-a7b4-4f15-a2e7-c1f6360bcd5a | 43 |
| 15,559 | Read | T14/60517a15-35b5-421f-b596-1490135dc9ee | 33 |
| 15,312 | Bash | groom-dispatch-review-session/f74aceaf-6 | 29 |
