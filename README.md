# The Relay

A small, evidence-grounded research assistant over exactly three arXiv papers:

| Key | Paper | arXiv version |
|---|---|---|
| `attention` | *Attention Is All You Need* | 1706.03762v7 |
| `moshi` | *Moshi: a speech-text foundation model for real-time dialogue* | 2410.00037v1 |
| `dynamic_chunking` | *Dynamic Chunking for End-to-End Hierarchical Sequence Modeling* | 2507.07955v2 |

The assistant answers **only** from these PDFs, cites the paper, page and
section for every factual sentence, and refuses when the retrieved evidence does
not support an answer. The project also measures its own reliability and cost:
every user request produces one row in a strict 13-column `results.csv`.

---

## Quick start (no API key required)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Build the three indexes (light / balanced / extensive)
python -m the_relay.main ingest

# Ask a question with the deterministic stand-in
python -m the_relay.main ask "What BLEU score did the big Transformer achieve on WMT 2014?"

# Run the automated tests
python -m pytest
```

The stand-in is **not** an LLM. It is a deterministic extractive baseline that
selects sentences from the retrieved evidence and cites them. It exists so the
whole pipeline, including failures and load behaviour, can be exercised without
spending quota. Every row it produces is labelled `source=stand-in`.

## Using the real Groq provider

1. Copy `.env.example` to `.env`.
2. Set `GROQ_API_KEY` and `RELAY_PROVIDER=groq`.
3. Probe the account before running anything expensive:

```powershell
python -m the_relay.main --provider groq probe --requests 3
```

The probe records HTTP status, latency and the real `x-ratelimit-*` headers in
`logs/probe.jsonl`. Those headers are the authoritative limits for the account,
not the documentation.

### What the probe measures and where the tokens come from

Each probe request sends this tiny prompt:

```
system: "Reply with the single word: ready."
user:   "Are you ready?"
```

That is 10 content words, but Groq reports **86 input tokens** because its chat
template wraps every message in special tokens (`<|start_header_id|>`,
`<|end_header_id|>`, `<|eot_id|>`, role names, BOS/EOS). The local estimator
counts content words only; the quota manager's 15% safety reserve absorbs this
discrepancy.

Output is capped at `max_tokens=8`. In the probe, all 8 output tokens are consumed
by the model's hidden reasoning, leaving `text: ""` (empty visible response).
This is what led to `RELAY_GROQ_REASONING_EFFORT=low` — without it, reasoning
eats the entire output budget.

Cost of the probe: ~3 × 86 = 258 input tokens + 24 output tokens ≈ 282 tokens
total. Negligible against the 200,000 daily budget.

### RPM probe test (31 requests, 2026-11-11)

Command: `python -m the_relay.main --provider groq probe --requests 31`

Result: **30 requests returned HTTP 200; the 31st returned HTTP 429.**
- Error: `"Rate limit reached for model openai/gpt-oss-120b ... on requests per
  minute (RPM): Limit 30, Used 30, Requested 1. Please try again in 2s."`
- Recovery time: ~2 seconds (rolling window).
- Output: `logs/probe.jsonl` (34 lines total: 3 from first probe + 30 ok + 1 error).
- Token cost: ~2,666 input + ~248 output ≈ 2,914 tokens (1.5% of daily budget).

This upgrades RPM from "documented, medium confidence" to **measured, high
confidence**: 30 is a hard limit, recovery is ~2 s, and the 429 body contains
the exact limit/used/requested counts.

```powershell
python -m the_relay.main --provider groq ask --preset balanced "Your question"
```

---

## What happens to one question

```
question
  -> scope check                  (is it about these papers?)
  -> exact cache                  (safe: key includes corpus + prompt + model)
  -> paper targeting              (does the wording name one paper?)
  -> retrieval                    (BM25 + TF-IDF + RRF + deterministic reranker)
  -> evidence gate                (sufficient / partial / insufficient)
       insufficient -> refuse, zero model calls
  -> bounded evidence loop        (at most `evidence_max_loops` extra retrievals)
  -> quota admission              (token-aware rolling minute/day windows)
  -> scheduler                    (concurrency cap, bounded queue, deadline)
  -> provider call                (Groq streaming, or the stand-in)
  -> verification                 (citations exist and are supported)
       failure -> one regeneration -> transparent partial answer if needed
  -> one row in logs/results.csv
```

Two details matter for honesty:

* **Refusal is a feature.** If the named paper's body never mentions a
  distinctive term in the question (references excluded), the system refuses
  instead of answering from a topically adjacent passage.
* **Estimates are labelled.** Before a call, token counts are estimated. After
  a successful call, the provider's reported usage replaces the estimate in
  `logs/model_calls.jsonl` and the row. Failed calls are charged conservatively
  in traces and marked `usage_estimated=true`.

---

## Commands

| Command | What it does |
|---|---|
| `ingest` | Extract and index the three PDFs at all three retrieval presets |
| `ask "..."` | Answer one question, optionally `--stream` |
| `probe --requests N` | Send N real requests and record limits/latency |
| `eval --presets light balanced extensive` | Answer the 23-question evaluation set |
| `experiment --scenarios steady burst overload reduced_quota daily_exhausted` | Run the five load shapes |
| `metrics [--json] [--export]` | Compute Tables 2a, 2b and 3 from `results.csv` |
| `reconcile` | Check that model-call logs sum to the CSV rows |

Useful flags:

```powershell
--provider groq|standin      # override RELAY_PROVIDER for one command
--preset light|balanced|extensive
--deadline-ms 25000
```

---

## Evaluation

`evaluation/questions.json` contains 23 hand-written questions: 19 answerable
and 4 deliberately unanswerable controls. Every answerable question lists the
fact groups that a correct answer must contain, so grading is auditable rather
than a model judging another model.

Grades are exactly the four values from the brief:

| Grade | Meaning |
|---|---|
| `correct` | All required facts are present **and** the answer cites at least one source |
| `wrong` | A fact is missing, the citation is missing, or an infrastructure failure delivered no answer to an answerable question |
| `refused_correctly` | The question is unanswerable and the assistant refused |
| `refused_wrongly` | The question is answerable but the assistant refused |

Infrastructure failures are graded `wrong` for answerable questions and
`refused_correctly` for unanswerable ones. That rule is stated in the report so
quality percentages can be read correctly.

---

## Repository layout

```
the_relay/
  config.py            all settings, with the unit contract documented
  core/                scope, evidence, quota, scheduler, retry, verification, pipeline
  rag/                 ingest, index, retrieval, paper targeting
  providers/           Groq adapter, deterministic stand-in, factory
  eval/                dataset loader, grader, metrics
  experiments/         scenarios, budget planner, runner
  obs/                 traces, model-call log, strict results.csv writer
config/                retrieval presets, stand-in calibration, corpus manifest
documents/             the three source PDFs
evaluation/            questions.json
tests/                 89 automated tests (no network, no quota)
reports/               two-page report and live-discussion preparation
logs/                  raw traces and the canonical results.csv (git-ignored, regenerable)
results/               exported evidence and probe summaries
```

---

## Tests

```powershell
python -m pytest
```

The suite covers paper targeting, corpus-absence refusal, the evidence gate,
quota admission (including the rolling-window boundary), the bounded scheduler,
retry classification, exact-cache keys, citation normalisation and verification,
the strict CSV schema, stand-in determinism, and end-to-end pipeline runs. It
never calls Groq. 90 tests pass in ~2 seconds.

### Automated tests (no network, no quota)

```powershell
$env:PYTHONPATH='P:\ProjectRelay'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -q
```
**Expected:** `90 passed in ~2s`. Covers targeting, evidence, grader, quota, retry,
scheduler, CSV schema, cache, verification, stand-in determinism, metrics, and
end-to-end pipeline runs. No test calls Groq.

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
**Expected:** All 90 tests listed with pass/fail status. Use this to identify which
specific test fails if any.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_quota.py -v
```
**Targets:** Rolling-window admission, boundary retry, daily limits, release/cancel
reconciliation. **Motive:** These pin BUG-002, BUG-003, BUG-004, BUG-007.
**Expected:** 9 passed.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -v
```
**Targets:** Concurrency cap, bounded queue, deadline rejection, peak in-flight.
**Motive:** Pins BUG-008 (max_queue_depth=0).
**Expected:** 5 passed.

### Corpus ingestion

```powershell
.\.venv\Scripts\python.exe -m the_relay.main ingest
```
**Targets:** PDF extraction, running-header removal, chunking at 3 presets
(light/balanced/extensive), index building (BM25 + TF-IDF).
**Motive:** Proves the corpus is parseable and indexes are reproducible.
**Expected:** JSON with chunk counts: balanced ~324, extensive ~445, light ~247.
All three docs verify OK.

### Single question (ask)

```powershell
.\.venv\Scripts\python.exe -m the_relay.main ask "What BLEU score did the big Transformer achieve on WMT 2014?"
```
**Targets:** Full pipeline: scope → targeting → retrieval → evidence → generate →
verify → CSV row.
**Motive:** Proves a normal question produces a cited, verified answer.
**Expected:** `status: ok`, `verified: True`, `citations: ['S1', ...]`, answer
contains "28.4" and "BLEU".

```powershell
.\.venv\Scripts\python.exe -m the_relay.main ask --stream "How many GPUs trained the Transformer?"
```
**Targets:** Streaming token delivery, TTFT measurement, on_token callback.
**Motive:** Proves the provider streams and first-token latency is measured.
**Expected:** Text streams token-by-token, then `first token: ~600 ms`,
`total: ~1300 ms`.

```powershell
.\.venv\Scripts\python.exe -m the_relay.main ask "What is the capital of France?"
```
**Targets:** Scope filter rejects before retrieval/model. Zero tokens spent.
**Motive:** Proves the system doesn't waste tokens on off-topic questions.
**Expected:** `tokens: 0 in / 0 out`, `calls: 0`, refusal message about the
3 papers.

```powershell
.\.venv\Scripts\python.exe -m the_relay.main ask "How does the Transformer handle speech input?"
```
**Targets:** Paper targeting + corpus-level topic presence check. "speech" never
appears in the Transformer paper's body → refused.
**Motive:** Proves the system refuses honestly when a named paper doesn't cover
the topic.
**Expected:** `calls: 0`, refusal mentions "speech" absent from the paper's body.

```powershell
.\.venv\Scripts\python.exe -m the_relay.main ask "Compare the Transformer and Moshi on latency"
```
**Targets:** Evidence gate returns `partial` (cross-paper comparison from one
doc). Pipeline produces partial answer + gap statement.
**Motive:** Proves the system doesn't fabricate a comparison it can't support.
**Expected:** `evidence: partial`, answer states what's supported and what's
missing.

```powershell
.\.venv\Scripts\python.exe -m the_relay.main ask --preset light "What is dynamic chunking?"
```
**Targets:** Preset override via CLI flag.
**Motive:** Proves retrieval preset can be changed per-request.
**Expected:** `retrieval: light`, answer cites dynamic chunking.

### Real Groq provider

```powershell
$env:RELAY_PROVIDER='groq'
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_probe3'
.\.venv\Scripts\python.exe -m the_relay.main --provider groq probe --requests 3
```
**Targets:** Real HTTP adapter, header reading, latency measurement.
**Motive:** Learn actual limits before spending quota.
**Expected:** 3× `ok, http 200`, totals ~300-900 ms, headers show
`limit-tokens: 8000` / `limit-requests: 1000`.

```powershell
$env:RELAY_PROVIDER='groq'
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_rpm_test'
.\.venv\Scripts\python.exe -m the_relay.main --provider groq probe --requests 31
```
**Targets:** RPM hard limit verification.
**Motive:** Confirm 30 RPM is a real limit, not documentation.
**Expected:** 30× HTTP 200, 31st returns HTTP 429 "Limit 30, Used 30,
Requested 1. Please try again in 2s." Token cost ~2,914.

```powershell
$env:RELAY_PROVIDER='groq'
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_ask1'
.\.venv\Scripts\python.exe -m the_relay.main --provider groq ask "What BLEU score did the Transformer achieve?"
```
**Targets:** End-to-end real pipeline with streaming, verification, CSV row.
**Motive:** Prove a real answer is cited and verified.
**Expected:** `status: ok`, `verified: True`, `citations: ['S1', ...]`,
answer contains "28.4".

```powershell
$env:RELAY_PROVIDER='groq'
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_real_quality'
.\.venv\Scripts\python.exe -m the_relay.main --provider groq eval --presets balanced
```
**Targets:** Full evaluation on real Groq. 23 questions, 120 s deadline.
**Motive:** Measure real answer quality and latency.
**Expected:** 23 `ok`, grades ~12 correct / 7 refused_wrongly / 4 refused_correctly,
~724 s wall clock, ~58,791 tokens in.
**Warning:** Takes ~12 minutes and consumes ~59k tokens.

### Quality evaluation (stand-in)

```powershell
$env:RELAY_PROVIDER='standin'
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_eval'
.\.venv\Scripts\python.exe -m the_relay.main eval --presets light balanced extensive
```
**Targets:** Answer quality per retrieval setting. 23 questions × 3 presets = 69 rows.
**Motive:** Table 3 data (correct/wrong/refused per preset).
**Expected:** All 69 `ok`. light: ~11 correct, balanced: ~13 correct,
extensive: ~13 correct. ~2-3 min.

```powershell
$env:RELAY_VERIFICATION_MODE='deterministic'
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_eval_det'
.\.venv\Scripts\python.exe -m the_relay.main eval --presets balanced
```
**Targets:** Baseline quality with deterministic verification only.
**Motive:** Compare with conditional_llm to measure LLM verifier impact.
**Expected:** ~13 correct, ~6 wrong, 4 refused_correctly, 0 refused_wrongly.

```powershell
$env:RELAY_PROVIDER='groq'
$env:RELAY_VERIFICATION_MODE='conditional_llm'
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_eval_llm'
.\.venv\Scripts\python.exe -m the_relay.main eval --presets balanced
```
**Targets:** Quality with LLM verifier enabled.
**Motive:** Measure whether LLM verification improves quality vs. cost.
**Expected:** ~14-16 correct, fewer refused_wrongly, ~10-15% more tokens.

### Load scenarios (experiment)

```powershell
$env:RELAY_PROVIDER='standin'
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_exp'
.\.venv\Scripts\python.exe -m the_relay.main experiment --scenarios steady burst overload reduced_quota daily_exhausted
```
**Targets:** Load behaviour under 5 shapes.
**Motive:** Table 2a and Table 2b data.
**Expected:** steady 6 ok; burst 2 ok + 6 rate_limited; overload 2 ok + 2 rl + 8
error; reduced_quota 2-3 ok + rest rl; daily_exhausted 4 rl (zero calls). ~3-4 min.

```powershell
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs_qualityfirst'
.\.venv\Scripts\python.exe -m the_relay.main experiment --quality-first --scenarios steady burst overload reduced_quota daily_exhausted
```
**Targets:** Combined: quality run first, then load scenarios.
**Motive:** Full canonical run (produces 128-row CSV).
**Expected:** 69 quality rows + 36 load rows = 105 rows in logs/results.csv.

### Metrics and reconciliation

```powershell
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs'
.\.venv\Scripts\python.exe -m the_relay.main metrics
```
**Targets:** Tables 2a, 2b, 3 + grade counts from `logs/results.csv`.
**Motive:** Verify every number is computable from the CSV.
**Expected:** Tables printed with scenario/setting rows, grades summary.

```powershell
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs'
.\.venv\Scripts\python.exe -m the_relay.main metrics --export
```
**Targets:** Copy `logs/results.csv` → `results.csv` (root).
**Motive:** The brief requires `results.csv` at project root.
**Expected:** `results.csv exported to P:\ProjectRelay\results.csv`

```powershell
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs'
.\.venv\Scripts\python.exe -m the_relay.main reconcile
```
**Targets:** Cross-check: every CSV row with tokens has matching model_calls.jsonl entries.
**Motive:** Catch missing/extra model calls or token mismatches.
**Expected:** `mismatch_count: 0`

```powershell
$env:RELAY_LOGS_DIR='P:\ProjectRelay\logs'
.\.venv\Scripts\python.exe -m the_relay.main metrics --json
```
**Targets:** Same tables but as JSON.
**Motive:** Useful for scripting/CI.
**Expected:** JSON with `rows`, `grades`, `table_2a`, `table_2b`, `table_3`.

### Merging real rows

```powershell
$env:PYTHONPATH='P:\ProjectRelay'
.\.venv\Scripts\python.exe scripts/merge_results.py 'C:\Users\Prachin\AppData\Local\Temp\opencode\relay_real_balanced_v4\results.csv' 'P:\ProjectRelay\logs\results.csv'
```
**Targets:** Append real `q-balanced-*` rows to canonical `logs/results.csv`.
**Motive:** One `results.csv` with both stand-in and real rows (unique request IDs).
**Expected:** `merged 23 rows ... (128 total)`

### Re-grading with softened grader

```powershell
$env:PYTHONPATH='P:\ProjectRelay'
.\.venv\Scripts\python.exe -c "
import json
from pathlib import Path
from the_relay.eval.dataset import load_questions
from the_relay.eval.grader import explain_grade
qs={q.qid:q for q in load_questions(Path('evaluation/questions.json'))}
rows=[json.loads(l) for l in open(r'C:\Users\Prachin\AppData\Local\Temp\opencode\relay_real_balanced_v4\eval_answers.jsonl',encoding='utf-8') if l.strip()]
for r in rows:
    q=qs[r['qid']]
    ex=explain_grade(q, r['answer'], 'ok')
    print(r['qid'], 'old=%s'%r['answer_grade'], 'new=%s'%ex.grade, 'missing=%s'%list(ex.missing_groups))
"
```
**Targets:** Show which questions flip grade after grader softening.
**Motive:** Verify the hyphen normalisation fix works on real data.
**Expected:** A04 and M01 flip from `refused_wrongly` to `correct`.

### Live-discussion modification exercises

```powershell
$env:RELAY_EVIDENCE_MIN_COVERAGE='0.95'
$env:RELAY_EVIDENCE_MIN_TOP1_SCORE='0.95'
.\.venv\Scripts\python.exe -m the_relay.main ask "What BLEU score did the Transformer achieve?"
```
**Targets:** Evidence gate refuses before any model call.
**Motive:** Prove refusal is enforced, not requested from the model.
**Expected:** `calls: 0`, refusal message.

```powershell
$env:RELAY_QUOTA_TPM='1200'
.\.venv\Scripts\python.exe -m the_relay.main ask "What latency does Moshi report?"
```
**Targets:** Token-aware admission rejects when prompt exceeds available TPM.
**Motive:** Prove the system doesn't oversubscribe.
**Expected:** `status: rate_limited` or delay, `calls: 0`.

```powershell
.\.venv\Scripts\python.exe -m the_relay.main --provider standin ask "What is dynamic chunking?"
.\.venv\Scripts\python.exe -m the_relay.main --provider groq ask "What is dynamic chunking?"
```
**Targets:** Provider factory swaps implementation based on flag.
**Motive:** Prove the pipeline is provider-agnostic.
**Expected:** Same pipeline, different `source` values (`stand-in` vs `real`).

```powershell
.\.venv\Scripts\python.exe -m the_relay.main ask "How does the Transformer paper handle speech input?"
```
**Targets:** Corpus-level topic presence check.
**Motive:** Prove refusal is based on corpus, not keyword blocklist.
**Expected:** Refusal, `calls: 0`, mentions "speech" absent from paper's body.

### Bug regression tests

```powershell
$env:PYTHONPATH='P:\ProjectRelay'
.\.venv\Scripts\python.exe -c "
import time, asyncio
from the_relay.core.quota import QuotaLimits, QuotaManager
async def main():
    qm = QuotaManager(QuotaLimits(rpm=30, tpm=100, rpd=1000, tpd=200000, reserve_fraction=0.0))
    await qm.try_acquire(100)
    try:
        await qm.acquire(50, deadline_monotonic=time.monotonic() + 0.05)
        print('FAIL: should have raised')
    except Exception as e:
        print('OK: raised', type(e).__name__)
asyncio.run(main())
"
```
**Targets:** Deadline expiry raises instead of waiting forever.
**Motive:** Pins BUG-002 regression.
**Expected:** `OK: raised QuotaExceededError`

```powershell
$env:PYTHONPATH='P:\ProjectRelay'
.\.venv\Scripts\python.exe -c "
import asyncio
from the_relay.core.pipeline import Relay
from the_relay.config import Settings
async def main():
    s = Settings(provider='standin', cache_enabled=False, quota_rpm=1000, quota_tpm=10000000, quota_rpd=100000, quota_tpd=100000000)
    r = Relay(s)
    res = await r.ask('What is the capital of France?', preset_name='extensive')
    print('evidence_level:', res.evidence_level)
    print('retrieval_setting:', res.retrieval_setting)
    print('model_calls:', res.model_calls)
    await r.aclose()
asyncio.run(main())
"
```
**Targets:** Scope refusal records the preset, not "none".
**Motive:** Pins BUG-009 regression.
**Expected:** `evidence_level: out_of_scope`, `retrieval_setting: extensive`,
`model_calls: 0`

### Summary: what each test proves

| # | Test | What it proves |
|---|---|---|
| 1.1 | 90 pytest | Every component works in isolation |
| 2.1 | ingest | PDFs parse, indexes build |
| 3.1 | ask (grounded) | Full pipeline produces cited answer |
| 3.2 | ask --stream | Streaming + TTFT measurement |
| 3.3 | ask (out of scope) | Zero-token refusal |
| 3.4 | ask (corpus absence) | Corpus-level topic check refuses |
| 4.1 | probe --requests 3 | Real limits from headers |
| 4.2 | probe --requests 31 | RPM=30 is hard limit |
| 5.1 | eval (3 presets) | Quality per retrieval setting |
| 6.1 | experiment (5 scenarios) | Load behaviour + Table 2a/2b |
| 7.1 | metrics | Tables computable from CSV |
| 7.3 | reconcile | Model calls match CSV rows |
| 8.1 | merge_results.py | Real rows merge without duplicates |
| 9.1-9.4 | Modification exercises | System is configurable |
| 10.1-10.2 | Bug regression | Fixes hold |

---

## Results journey (what changed and why)

The project was built incrementally. Each change below moved a measured number;
the previous value is kept so the progression is visible.

| Change | Why | Before | After |
|---|---|---|---|
| Grader: refusal checked before facts | An answer with all facts plus an honest caveat was graded `refused_wrongly` | A03, A04, M01, D01 graded `refused_wrongly` despite containing required facts | Facts decide the grade first; refusal only marks `refused_wrongly` when facts are missing |
| Grader: unicode hyphen normalisation | Groq emits non-breaking hyphens (U+2011); `Encoder-decoder` failed to match `encoder-decoder` | Real run: 12 correct, 7 refused_wrongly, 4 refused_correctly | Re-grade: 14 correct, 5 refused_wrongly, 4 refused_correctly (A04, M01 flipped) |
| Stand-in calibration (base_latency 850→810, ttft 0.35→0.77, stream_chunk 20→0) | Provisional values pre-dated any real measurement | Stand-in TTFT ~300 ms (unrealistic) | Stand-in TTFT ~620 ms (matches real median 703 ms) |
| Deadline unit fix (ms→s) | `deadline_mono` was in ms but compared to `time.monotonic()` in s | 4 real requests rejected as `rate_limited` despite having quota | All 23 real questions completed with `status=ok` |
| Zero-wait boundary fix | `wait_s==0` at rolling-window boundary raised instead of re-checking | 4 admissible requests rejected | Re-check succeeds |
| Scheduler `max_queue_depth=0` fix | `_waiting >= 0` rejected the first request even with a free worker | `test_full_queue_is_rejected_immediately` failed | Test passes; first request admitted |
| Out-of-scope `retrieval_setting` fix | Scope refusals wrote `"none"`, creating a phantom Table 3 row | Table 3 had 4 settings (light/balanced/extensive/none) | Table 3 has 3 settings; U01 counted under its preset |
| Real quality deadline 60s→120s | TPM pacing: 4th call waits ~58 s; 60 s deadline expired during normal pacing | 1 timeout in v2 run | 0 timeouts in v4 run (724.1 s wall clock) |

The raw measurements (128 rows) are in `logs/results.csv` and exported to `results.csv`.
Every table in `reports/REPORT.md` is recomputable from that file via `metrics --export`.

## Honest limitations

- Retrieval is lexical (BM25 + TF-IDF), not neural. Paraphrased questions can
  miss a passage that a human would find.
- The evidence gate is a heuristic, not entailment. It can produce a false
  refusal or accept a passage that merely shares vocabulary.
- The deterministic verifier checks citation existence and lexical support. It
  cannot prove that a cited sentence entails a claim.
- The stand-in is an extractive baseline. Its quality numbers describe the
  pipeline, not a language model.
- The exact cache is in-process, so it is lost on restart and not shared
  between workers.
- Real Groq quality latencies include admission waiting for the shared TPM
  window; they are not directly comparable to stand-in latencies.

See `PROJECT_JOURNAL.md` for the full engineering record, including failed
experiments and the reasoning behind each design decision.
