# Live Conversation — 5-Minute Walkthrough and Live-Change Drills

The brief says: *"A short live conversation follows where you explain your results and
change your system, so only submit what you can defend."* Nothing here is uploaded — it
is rehearsal material for that conversation. Everything below is a command that works on
this repo and a claim that is already backed by a row in `results.csv` or a probe log.

Run it once end to end so the numbers are in your mouth, not on the screen.

---

## Part 1 — The 5-minute script

**0:00 — The one decision.**
"Every reply fast, every reply good, the bill small, nobody turned away — the brief says
I cannot have all four. On 8k tokens a minute, a request is not a request, it is a token
debt. So admission is token-aware, not request-count-aware (ADR-004). That single choice
produced the queue, the deadline, the refusals and the load results."

**0:45 — What the real service actually does.**
"I did not trust the documentation. Thirty-one tiny requests: thirty came back 200, the
31st came back 429 `Limit 30, Used 30, Requested 1` and cleared in about two seconds.
So RPM is 30, measured (`logs/probe.jsonl`). TPM 8,000 and RPD 1,000 came from the
response headers. TPM binds first: a balanced answer is ~2,900 tokens, which is 26
seconds of budget per answer."

**1:30 — Show one answer.**
```
py -m the_relay.main ask "What BLEU score did the big Transformer achieve on WMT 2014?"
```
Point at: `citations: ['S1', ...]`, `tokens in/out`, `first token`. Then:
```
py -m the_relay.main ask "what is the capital of france?"
```
"Zero calls, zero tokens, `refusal reason: scope:external_fact`. The refusal is decided
before the model is paid for."

**2:15 — Where the time goes.**
"On the real run, TTFT p50 was 1,516 ms but the tail was 58 s. The 58 s is not
generation — it is the fourth request of a burst waiting for token budget to refill.
That is why Tables 2a and 2b are separated: latency alone would blame the model."

**3:00 — Quality and cost together.**
"23 questions, three retrieval presets, graded on facts and citations, not on vibes.
Light setting: 47.8% correct, 1,426 tokens per answer. Balanced: 56.5%, 2,063. Extensive:
56.5%, 2,483 — so extensive buys nothing and costs 20% more. I ship balanced."
If asked why real and stand-in differ: "the real model refuses more, because the
deterministic verifier rejects paraphrases; that is the trade I accepted for zero extra
tokens."

**3:45 — Failure behaviour.**
"Under burst, 75% were rate-limited, not wrong. Under overload, the queue rejects
immediately with `error` rather than making people wait for a timeout. Daily budget gone:
every request is rejected before the model is called, with a plain message — no guessing."

**4:15 — What I would fix next.**
"Conditional LLM verification for the five real answers that got refused wrongly. It
costs about one extra call on maybe 20% of questions, and I can show the token delta
before shipping it."

---

## Part 2 — Live-change drills

The interviewer will ask you to change something. Pick one, run it, read the number out
loud. Each drill takes under two minutes.

### Drill A — Halve the per-minute budget (shows you understand admission)

```
$env:RELAY_QUOTA_TPM='4000'
py -m the_relay.main ask "What BLEU score did the Transformer achieve?"
```
Say: "Same question, half the budget. What should happen is a longer wait before the
call, not a wrong answer; if the deadline is shorter than the wait, an honest timeout."
Then check the printed `total:` and the trace.

### Drill B — Shrink the deadline (shows you understand give-up behaviour)

```
$env:RELAY_REQUEST_DEADLINE_MS='3000'
py -m the_relay.main ask "Compare the Transformer and Moshi on latency."
```
Say: "The deadline is the user's patience, not the HTTP timeout. When it expires the
request is dropped with `timeout`, and that row is in the CSV as a given-up request."

### Drill C — Change retrieval live (shows cost/quality is a knob, not a slogan)

```
py -m the_relay.main ask --preset light "What is dynamic chunking?"
py -m the_relay.main ask --preset extensive "What is dynamic chunking?"
```
Say: "Watch `tokens:` and `citations:`. Light is cheaper and cites fewer passages; that
is the same trade Table 3 measures at 23 questions instead of one."

### Drill D — Prove the CSV drives the tables

```
py -m the_relay.main metrics
```
Say: "Nothing here is typed by hand. Delete a row and the table changes; that is what
'must match the raw file' means."

---

## Part 3 — Answers to the questions that are coming

| Question | Answer, in one breath |
|---|---|
| "Why not just retry on 429?" | Retrying a 429 immediately spends the same exhausted budget. Backoff is capped and deadline-aware; a daily-quota 429 is never retried (test_retry.py). |
| "Why is your p99 equal to your p95?" | 6 requests. Nearest-rank, no interpolation — an interpolated p99 from 6 points would be invented precision (metrics.py:52). |
| "Is the stand-in cheating?" | No: every row says `source=stand-in`, and its latency is calibrated from 11 real calls (`config/standin_calibration.json`). Its failure rates are chosen, and I say so. |
| "Biggest weakness?" | Lexical verification is not entailment, and paraphrase retrieval is weak without embeddings (ADR-002, ADR-003). |
| "What would you do with a paid plan?" | Raise concurrency and drop the reserve; the queue stays, because bursts do not disappear with a bigger budget. |
| "What is yours here?" | Goal ordering, load shapes, the 23 questions and the 13/10 split, the presets, the 120 s deadline, and publishing measured limits instead of documented ones. |

---

## Part 4 — Do not say

- Do not call stand-in numbers "real".
- Do not quote a limit without saying whether it came from headers or from a 429 body.
- Do not claim the LLM verifier was measured — it is implemented and off by default.
- Do not claim Vaani feedback exists before the owner writes it.
