# Live-discussion preparation

The brief says the live discussion may ask us to modify the system. These are
the exercises we have actually rehearsed, with the exact commands and the
expected observable change. None of them should require editing more than one
file.

Run every command from the repository root with the virtual environment active.

```powershell
$env:PYTHONPATH = (Get-Location).Path
```

---

## 1. Make retrieval cheaper and noisier

Edit `config/retrieval.yaml`, balanced preset:

```yaml
balanced:
  chunk_words: 220     # was larger
  overlap_words: 40
  top_k: 6             # was 8
  rerank: true
```

Re-ingest and evaluate:

```powershell
python -m the_relay.main ingest
python -m the_relay.main eval --presets balanced
```

**Expected:** fewer prompt tokens per answer, lower first-token latency, and a
higher risk of missing a fact that lived in a dropped chunk. Table 3 shows the
trade-off; it is not automatically an improvement.

## 2. Turn re-ranking off

```powershell
# In config/retrieval.yaml
balanced:
  rerank: false
```

**Expected:** the fused order (BM25 + TF-IDF + RRF) is used directly. Top-1
relevance usually falls and numeric questions are more likely to surface a table
row. No re-ingest is needed; re-ranking is a query-time step.

## 3. Force an honest refusal

```powershell
# In config/retrieval.yaml
# ...or raise the thresholds in .env:
$env:RELAY_EVIDENCE_MIN_COVERAGE = "0.95"
$env:RELAY_EVIDENCE_MIN_TOP1_SCORE = "0.95"
python -m the_relay.main ask "What BLEU score did the Transformer achieve?"
```

**Expected:** the evidence gate answers `insufficient` and the model is never
called (`calls: 0`). This demonstrates that refusal is enforced before spending
tokens, not by asking the model to behave.

## 4. Change the provider without touching code

```powershell
python -m the_relay.main --provider standin ask "What is dynamic chunking?"
python -m the_relay.main --provider groq    ask "What is dynamic chunking?"
```

**Expected:** the same pipeline, two different `source` values. The stand-in is
extractive and deterministic; Groq is generative and uses real quota.

## 5. Trigger the per-minute limit deliberately

```powershell
$env:RELAY_QUOTA_TPM = "1200"   # far below a normal prompt
python -m the_relay.main ask "What latency does Moshi report?"
```

**Expected:** either a delay while the manager waits for the rolling window, or
`status: rate_limited` with `error_type: quota`. The row shows `model_calls: 0`
when admission rejected it, which proves the rejection happened before the
provider was called.

## 6. Trigger a provider 429 and a retry

The stand-in can fail deterministically:

```python
# In a scratch script, or via the experiment runner
Relay(settings, standin_overrides={"rate_limit_on_calls": (1,)})
```

**Expected:** the first call raises `RateLimitError`, the retry policy decides
whether the deadline and token budget allow another attempt, and
`model_calls.jsonl` shows both attempts. With `RELAY_RETRY_MAX_ATTEMPTS=0` the
request ends `rate_limited` after exactly one attempt.

## 7. Show why retrying a 429 can hurt

Set a short deadline and a long `retry-after`:

```powershell
$env:RELAY_RETRY_MAX_ATTEMPTS = "1"
$env:RELAY_REQUEST_DEADLINE_MS = "1500"
```

**Expected:** the policy declines the retry because the backoff would cross the
deadline. The trace says `backoff would cross the request deadline`. Retrying
would have consumed another request and still failed.

## 8. Make a citation invalid

Ask a question, then (in a scratch copy of the provider output) change `[S1]`
to `[S9]`, or run a provider that emits a fake id.

**Expected:** the deterministic verifier returns
`invalid_citations=('S9',)`, the pipeline performs one regeneration, and if that
also fails it returns a transparent partial answer rather than the invalid
claim.

## 9. Break the corpus-absence rule

Ask:

```powershell
python -m the_relay.main ask "How does the Transformer paper handle speech input?"
```

**Expected:** refusal with zero model calls, because `speech` never occurs in
the Transformer paper's body. Then ask:

```powershell
python -m the_relay.main ask "How does the Transformer handle attention?"
```

**Expected:** a normal cited answer. The difference is the corpus-level topic
presence check, not a keyword blocklist.

---

## Numbers to have ready

Be able to state, from the final `results.csv`:

* how many requests each scenario sent, and the offered rate;
* first-token and total p50/p95/p99, and the sample size behind them;
* success, rate-limited and timed-out percentages per scenario;
* retries per request;
* tokens per answer and the shadow cost per 1,000 answers;
* the quality percentages for each retrieval setting;
* the one real Groq request used for the probe and what its headers said.

If a number was not measured, say "not measured". Do not estimate it aloud.

## Questions to expect

1. **Why is the stand-in not an LLM?** Because the comparison that matters is
   the pipeline's behaviour under load and its honesty, not a second model's
   eloquence. A deterministic baseline is reproducible and cannot hide a
   retrieval bug behind fluent prose.
2. **Why lexical retrieval?** It is exact for technical terms ("BLEU", "RQ-
   Transformer", "dechunking") and costs no embedding calls. The cost is
   paraphrase recall, which is stated as a limitation.
3. **Why not semantic caching?** Near-identical questions can have opposite
   answers. A false hit is a confidently wrong answer served instantly. The
   exact cache keys on the normalised question plus corpus, prompt, model and
   settings.
4. **What breaks if the TPM estimate is wrong?** The safety reserve (15%) absorbs
   modest underestimation. A large underestimate can produce a real 429, which
   is why the retry policy honours `retry-after` and why the probe records the
   provider's own headers.
5. **What is the least certain assumption?** That lexical overlap between a
   claim and a cited passage is evidence of support. It catches fabricated
   numbers and uncited sentences, but it cannot prove entailment.
