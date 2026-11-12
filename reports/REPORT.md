# The Relay — Two-Page Report

**Scope:** answers only from *Attention Is All You Need* (1706.03762v7),
*Moshi* (2410.00037v1) and *Dynamic Chunking* (2507.07955v2). Every factual
sentence cites paper, page and section; the system refuses when evidence is
insufficient. Raw measurements live in `logs/results.csv` (canonical) and are
exported to `results.csv`. One row per user request; 13 columns:

`request_id, scenario, source, start_time, first_token_ms, total_ms, status,
retries, tokens_in, tokens_out, retrieval_setting, answer_grade`

## 1. What I built and why

A ~4-stage pipeline per question:

1. **Scope filter** — rejects questions not about the three papers before any
   model call (zero tokens spent).
2. **Paper targeting** — when the wording names one paper, retrieval is
   restricted to that paper; bare "Transformer" is only a weak hint and plural /
   cross-paper wording disables narrowing.
3. **Retrieval + evidence gate** — BM25 + TF-IDF + RRF + deterministic reranker,
   then a deterministic sufficiency screen (`sufficient` / `partial` /
   `insufficient`). `insufficient` refuses with zero model calls.
4. **Generate → verify → (regenerate / partial answer)** — bounded evidence loop,
   token-aware admission, bounded scheduler, provider call, deterministic
   verification; one regeneration on failure, then a transparent partial answer.

The decision that shaped everything: **token-aware admission control** (ADR-004).
For a ~2,000-token prompt, the 8,000-token-per-minute window binds long before
the 30-requests-per-minute limit, so a request-count gate would admit two calls
and then hit a 429. The accountant estimates tokens before each call and
reconciles the provider's actual usage after it; every retry, regeneration and
verification passes through it.

Deliberately left out: Redis, fastembed, FastAPI, semantic cache, torch
(ADR-006 to ADR-009). An exact in-process cache is safe because its key includes
the normalised question plus corpus, prompt, model and settings; semantic
caching can serve a confidently wrong answer for near-identical opposite
questions.

## 2. The real service and my stand-in

**Real service:** `openai/gpt-oss-120b` on Groq's free tier via
`https://api.groq.com/openai/v1`, with `RELAY_GROQ_REASONING_EFFORT=low`.
Limits observed from live `x-ratelimit-*` headers (probe, 3× HTTP 200):

| Limit | Value found | How found | Confidence |
|---|---|---|---|
| Requests per minute | 30 | measured: 30 succeeded, 31st returned HTTP 429 "Limit 30, Used 30, Requested 1" | high |
| Tokens per minute | 8,000 | `x-ratelimit-limit-tokens: 8000` | high |
| Requests per day | 1,000 | `x-ratelimit-limit-requests: 1000` | high |
| Tokens per day | 200,000 | documentation | medium |
| Requests in flight at once | not measured | — | — |
| What it returns when a limit is hit | HTTP 429 with `retry-after` ~2 s (RPM); delayed (TPM window) / rejected (queue, deadline) | observed: 31st request returned 429 "Please try again in 2s"; 44–58 s TPM waits | high |
| Time until it recovers | ~2 s (RPM) / 44–58 s (TPM window) | 429 response + `x-ratelimit-reset-tokens` | high |

The single real measurement that matters: with a ~2,000-token prompt, the TPM
window admits about three calls per minute. The fourth waits **44–58 s** for
`x-ratelimit-reset-tokens`. That admission wait — not model latency — dominates
real quality runs (median per-call latency 812 ms; TTFT median 703 ms).

**Stand-in:** a deterministic, seeded extractive baseline, *not* an LLM. It
selects and quotes sentences from retrieved evidence, cites them (`[S1]`), and
streams. Every row is labelled `source=stand-in`. Calibrated from the 11
successful real calls:

| Parameter | Calibrated value | Evidence |
|---|---|---|
| base_latency_ms | 810 | real total median 812 ms (n=11, min 735, max 1,781) |
| ttft_fraction | 0.77 | real median TTFT share 703/812 |
| stream_chunk_ms | 0 | real stream finishes in ~0.1 s after TTFT |
| failure_rate / timeout_probability | 0.02 / 0.01 | chosen, not measured (real Groq: 0 failures in 24 requests; the only 429 was self-inflicted in the 31-request probe) |

The stand-in cannot paraphrase; its error mode is "quoted the wrong sentence,"
never "invented a fact." Its quality numbers describe the retrieval/verification
pipeline, not a language model.

## 3. Results

**Real runs:** real quality run v4 completed 2026-11-11 (23 questions, all `ok`,
12 correct / 7 refused_wrongly / 4 refused_correctly, 0 wrong, 0 retries,
58,791 tokens in / 5,674 tokens out, 724.1 s wall clock). Real rows are in
`logs/results.csv` (merged via `scripts/merge_results.py`) and exported to
`results.csv` (128 rows total). Reconcile: 0 mismatches.

**Table 2a — latency by load scenario (stand-in, recomputed from
`logs/results.csv`; real rows appended after v4 completes). p99 is noisy on
these small samples.**

| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50/p95/p99 (ms) | Total time p50/p95/p99 (ms) |
|---|---|---:|---:|---:|---:|---:|
| steady | stand-in | 6 | 101.7 | 3.5 | 797 / 1125 / 1125 | 1,375 / 1,750 / 1,750 |
| burst | stand-in | 8 | 1.3 | 360.1 | 578 / 672 / 672 | 31 / 1,297 / 1,297 |
| overload | stand-in | 12 | 1.4 | 517.2 | 578 / 688 / 688 | 0 / 1,360 / 1,360 |
| reduced_quota | stand-in | 6 | 75.0 | 4.8 | 578 / 15,703 / 15,703 | 16 / 16,328 / 16,328 |
| daily_exhausted | stand-in | 4 | 0.1 | 2,526.3 | None / None / None | 16 / 31 / 31 |

**Table 2b — outcomes by load scenario (stand-in):**

| Scenario | Succeeded % | Rate-limited % | Timed out / given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
|---|---:|---:|---:|---:|---:|---:|---:|
| steady | 100.0 | 0.0 | 0.0 | 0.0 | 1 | 1,824.5 | $0.3713 |
| burst | 25.0 | 75.0 | 0.0 | 0.0 | 4 | 2,371.5 | $0.4527 |
| overload | 16.7 | 16.7 | 66.7 | 0.0 | 4 | 2,371.5 | $0.4527 |
| reduced_quota | 33.3 | 66.7 | 0.0 | 0.0 | 2 | 1,936.5 | $0.3883 |
| daily_exhausted | 0.0 | 100.0 | 0.0 | 0.0 | 1 | None | None |

**Answer quality (≥15 hand-written questions; 23 total: 19 answerable, 4
unanswerable controls), stand-in, all statuses `ok`:**

| Retrieval setting | Questions asked | Correct | Wrong | Refused correctly | Refused wrongly | Tokens per answer |
|---|---:|---:|---:|---:|---:|---:|
| light | 23 | 11 (47.8%) | 8 (34.8%) | 4 (17.4%) | 0 | 1,426.5 |
| balanced | 23 | 13 (56.5%) | 6 (26.1%) | 4 (17.4%) | 0 | 2,063.0 |
| extensive | 23 | 13 (56.5%) | 6 (26.1%) | 4 (17.4%) | 0 | 2,483.1 |

`correct` requires at least one `[S#]` citation plus all required fact groups.
The 4 refusals per setting are the unanswerable controls, graded
`refused_correctly`. Real quality results (correct / wrong / refused) are
added from `results.csv` when v4 completes. Infrastructure failures grade
`wrong` for answerable questions and `refused_correctly` for unanswerable ones.

**Real quality (Groq, balanced preset, 23 questions, all `ok`):**

| Retrieval setting | Source | Questions asked | Correct | Wrong | Refused correctly | Refused wrongly | Tokens per answer | TTFT p50/p95 (ms) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| balanced | real | 23 | 12 (52.2%) | 0 (0.0%) | 4 (17.4%) | 7 (30.4%) | 2,802.8 | 1,516 / 58,281 |

Real Groq never fabricated a wrong fact (0 wrong), but it refused 7 answerable
questions (refused_wrongly) versus 0 for the stand-in — the real model is more
cautious. TTFT p95 (58,281 ms) is admission-wait dominated, not generation.

After the grader softening (unicode hyphen normalisation, committed in fce3ff1),
re-grading the same answers flips A04 and M01 from refused_wrongly to correct:
14 correct, 5 refused_wrongly, 4 refused_correctly. The raw CSV grades (12/7/4)
are retained as the measured-at-run-time values; the re-grade is the post-hoc
improvement.

A number we could not have guessed without running it: real TTFT is 77% of
total latency on average, and the fourth call in a minute waits ~44–58 s purely
for the TPM window to reset — invisible in per-call latency.

## 4. Where the time goes

For a real request (A03 / A04):

* first token: ~0.5–0.6 s (model streaming, measured TTFT: A03 500 ms,
  A04 609 ms);
* total: ~59–60 s (A03 59,688 ms, A04 60,140 ms);
* delay after first token (~58–59 s): regeneration waiting for the TPM window
  to admit the retry / verification call — admission pacing, not generation.

The real per-call median is 812 ms total (TTFT 703 ms) for non-throttled calls;
stand-in answers finish in well under a second. The gap between them is the
application's own token admission, which both providers share.

## 5. When a limit is hit

* **Per-minute token (TPM):** admission waits for the rolling window; the
  request is delayed, not failed, unless the deadline expires first.
* **Per-minute request (RPM):** same admission path; documented 30/min.
* **Per-day (RPD / TPD):** `preconsume_daily` refuses before any model call with
  the `DAILY_QUOTA_EXHAUSTED` user message; no retry can restore a daily budget.
* **Queue full:** `QueueFullError` → immediate rejection, status `error`.
* **Deadline exceeded:** `DeadlineExceededError` → honest `timeout` status.
* **Provider 429 / 5xx:** bounded retry honouring `retry-after`; declined if the
  backoff crosses the deadline or the token budget is insufficient.

The real quality run saw no 429 (23 requests, paced). The 429 was produced directly by
the 31-request probe: HTTP 429 body `"Limit 30, Used 30, Requested 1. Please try again in
2s."`, cleared in about 2 s — measured, in `logs/probe.jsonl`.

## 6. Retrieval: quality versus cost

Three presets (piece size, pieces fetched, re-rank): light, balanced, extensive.
Quality is essentially flat (47.8% / 56.5% / 56.5% correct) while tokens per
answer rise (1,426.5 → 2,063.0 → 2,483.1). The `extensive` preset uses smaller
chunks (a tested hypothesis: more, tighter pieces surface exact terms but cost
more tokens). Re-ranking is on by default; turning it off lowers top-1 relevance
and makes numeric questions more likely to surface a table row.

Real Groq on balanced: 52.2% correct (12/23), 0 wrong, 4 refused correctly,
7 refused wrongly — the real model is more cautious than the stand-in.

Quality plateaus because the binding constraint is the evidence gate and the
deterministic extractive stand-in, not raw retrieval recall. Bigger context buys
tokens, not correct answers, on this corpus.

## 7. How this library will mislead a naive retriever

* **Shared vocabulary:** all three papers discuss Transformers, training, tokens
  and attention. A pure similarity retriever returns a Moshi passage for a
  Transformer question — the words match, the paper does not answer it.
* **Running headers:** Moshi repeats its title in every page header; naive
  extraction treats it as body text and heading detection fires on nearly every
  page (removed: repeated short lines on ≥30% of pages).
* **Table rows flattened to prose:** `N d_model d_ff h d_k d_v ... 6 512 2048 ...`
  crowd out real sentences (filtered by numeric density).
* **Bibliography rows:** treated as evidence unless filtered as boilerplate /
  back matter.
* **Full-width brackets:** Groq emits `【S1】`; normalised to `[S1]` before
  verification, or a correct answer is discarded.

The fix is deterministic paper targeting plus a corpus-level topic-presence
check: distinctive question terms absent from the *named paper's body*
(references excluded) force an honest refusal.

## 8. Retries

Only temporary failures are retried (HTTP 429, 5xx, connection reset, read
timeout); never 400/401/403/404/413/422, never daily-quota exhaustion. Bounded
exponential backoff with jitter, capped by `max_delay_ms` and by the user's
deadline — a retry whose backoff would cross the deadline is declined, because
retrying a 429 immediately consumes another RPM request while the token bucket
is still empty. Default `retry_max_attempts = 1`: one controlled retry.

## 9. What the person sees

* A cited, grounded answer when evidence is sufficient.
* A partial answer (supported sentences only) plus an explicit gap when evidence
  is partial.
* An honest refusal ("the papers do not contain enough evidence …") with zero
  model calls when evidence is insufficient or the question is out of scope.
* At 3 pm with the daily budget gone: the `DAILY_QUOTA_EXHAUSTED` message — the
  request is rejected before the model is called, not answered with a guess.
* Every interaction produces one row in `results.csv` with status and grade, so
  a person (or reviewer) can recompute every table.

## 10. What I'm unsure about, what I left out, what I learned

**Least certain assumption:** that lexical overlap between a claim and the passage it
cites is a usable proxy for support. It catches fabricated numbers and uncited
sentences, not logical support. If it is wrong, the verifier lets through a confident
sentence that misstates the paper — the one failure the brief says must never happen.
**Second:** the stand-in's failure rates are chosen, not measured, so the load tables
describe my stand-in's design rather than Groq's true failure distribution. Nothing
outside those two rows depends on them.

**Left out, deliberately:** a neural embedding retriever (paraphrase recall stays weak)
and a semantic cache (one false hit serves a wrong answer instantly). Also no LLM
verifier A/B, no HTTP API, no Redis.

**Learned:** admission control beats request counting; refusing before spending tokens
is more honest than asking a model to behave; the stand-in's extractive errors expose
retrieval and verification bugs that a fluent model would hide; and on this free tier,
answer latency is set by token pacing, not by generation.

## 11. How I worked with the assistant

I used the assistant as a pair programmer and a hostile reviewer, not as an author of
measurements. Two rules held for the whole build: it never invented a number, and it
never ran a long real-quota command for me — it wrote the command, I ran it, and I
pasted the output back for it to analyse. Every figure in Tables 1–3 comes from a file
I produced.

**The three prompts that mattered most** (the first two are paraphrased — the original
chat text was not recoverable, so they state the substance rather than the wording; the
third is quoted from the brief).

1. *Focus on two of the four goals, quality and speed, with quality first.* This
   ordering chose the architecture: verification is never skipped to save tokens, a
   request waits rather than getting a weaker answer (the 120 s real-run deadline), and
   an honest refusal beats a plausible guess.
2. *Send 31 real requests to Groq and test the limits its documentation claims.* This
   produced: 30× HTTP 200, then HTTP 429 "Limit 30, Used 30, Requested 1", i.e. RPM 30
   with about two seconds of recovery. The RPM row in Table 1 is measured, not copied
   from a web page.
3. *"Every answer must cite where in the papers it came from. If the papers do not say,
   the assistant must say so instead of guessing."* This produced the citation contract,
   the evidence gate, and the 23-question evaluation; the real run (58,791 tokens in,
   724 s) showed that quality latency is TPM pacing, not generation.

**One thing it got wrong, and how I caught it.** Tidying `RUNS.md` it rewrote the file
instead of appending, which silently deleted the record of commands already run. The
post-write check on the file (`has Section 1 commands: False`) caught it before commit;
the 371-line original was restored with `git show HEAD~1:RUNS.md` and the new section
merged into it. Documentation gets the same verification as code.

**One decision I overruled.** Its easier default was to let the model classify scope and
verify its own answer on every question (ADR-001 option a, ADR-003 option c). I overruled
both: scope is decided deterministically before any call, and verification is
deterministic by default with the LLM verifier behind `verification_mode`. The effect is
measurable — "what is the capital of france?" costs 0 model calls and 0 tokens — and a
second call would have roughly doubled tokens per answer on an 8k-TPM budget.

**What is distinctly mine.** The goal ordering (quality first, then speed, with cost and
availability the two I chose to trade away), the five load shapes and their
parameters, the 23 questions and the 13/10 deterministic-versus-LLM split, the three
retrieval presets, the 120 s real-run deadline, and the decision to publish measured
limits rather than documented ones. The assistant wrote most of the code to those
decisions; it did not choose them.
