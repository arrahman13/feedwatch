# FeedWatch — Building Blocks Tested & What We Learned

Notes for Cortensor Hackathon #5, from building and running FeedWatch
with the OpenMinion 0.1 Python SDK (`openminion==0.1.22`, Python 3.12).
Reference runs used a local Ollama model (`qwen2.5:1.5b`) on a heavily
throttled shared VM (~1.5 effective CPUs, ~19–30 tokens/s prompt
processing), so each run took 15–20 minutes of wall clock. On a normal
laptop or a hosted model the same workflow takes seconds to a couple of
minutes. All run logs — including the failed attempts that taught us the
lessons below — are in [`runs/`](runs/).

## Building blocks tested

| Building block | How FeedWatch uses it | Verdict |
| --- | --- | --- |
| Agent execution | Two `Agent.run()` turns per run (fetch, then summarize) with `instructions` and `timeout_seconds=900`; `ProviderError` / `AgentOutputValidationError` handled explicitly with distinct exit codes | Works. 5/5 final runs completed (`run_state=completed`). |
| Tools | `@tool(min_scope="READ_ONLY", idempotent=True)` `fetch_new_items`, listed in `forced_tools` for the fetch turn; repeat calls return a cached payload | Works — forced tool calling via Ollama `/api/chat` fired reliably in fresh sessions (tool_calls 1–2 per run). But see lesson 1: forcing alone does not guarantee the call. |
| Typed / structured output | `output_type=Digest` (Pydantic: headline, overview, highlights[]) on the digest turn | Works mechanically — every digest validated. Content quality from a 1.5B model is another matter (lesson 3). |
| Sessions | Both turns share one session per run; a **fresh** session per run (`feedwatch-<UTC timestamp>`); session/thread ids printed as diagnostics | Works, with a caveat: reusing one session across runs caused real failures (lesson 2), so per-run sessions are the deliberate design. |
| Providers / models | Local Ollama preset (`qwen2.5:1.5b`, GGUF-sideloaded) for all reference runs; provider chosen purely by config | Works. No code change needed to switch providers. Default timeouts/context assume hosted models (lessons 4–5). |
| Permissions | The only tool is a read-only network fetch; all file writes (digest, state) are done by the application, never the model | Works by design. `min_scope="READ_ONLY"` matches the tool's actual behavior. |
| Memory | Application-level: `.feedwatch/state.json` seen-id store, committed only after a digest is written; SDK session memory is *not* used as the dedupe store | Works — verified by the run arc: 4 + 4 + 2 items drained the 10-item OpenMinion feed, then run 5 reported "Nothing new" with 0 items. |
| Diagnostics | `result.stats` / `run_state` printed per run; raw logs kept in `runs/`; `openminion doctor` used during setup | Works. `stats["tool_calls"]` turned out to be a load-bearing diagnostic (lesson 1). |
| Delegation / subagents | Not used — deliberately out of scope for a single-workflow agent | n/a |
| Skills / tasks | Not used | n/a |

## SDK access findings (what a new builder needs to know)

- `pip install openminion` serves the 0.1 line (0.1.22 at build time);
  the 0.1 Python SDK is the documented facade
  `from openminion import Agent, tool` plus Pydantic for typed output.
- **No account or API key is required to build with the SDK itself.**
  A model provider is the only external dependency:
  - **Local Ollama** needs no credential at all
    (`openminion setup --provider ollama --model <model> --no-chat`).
    No wallet, no testnet $COR, no agent keypair is involved anywhere
    in the SDK path.
  - **Cortensor Portal** needs `CORTENSOR_API_KEY` and uses model
    `oss-20b` at `https://api.cortensor.app/v1`. Portal access is
    limited/coordinated by the Cortensor team, so it is a credential
    only the account holder can obtain; the hackathon states Portal is
    optional and non-users are not penalized. We did not use Portal for
    the reference runs.
  - Hosted third-party providers (OpenRouter, OpenAI, …) need their own
    keys.
- Project-local config works well: with `OPENMINION_HOME=$PWD`,
  `openminion setup` writes `./.openminion/agents.json`, and the Python
  SDK picks that config up from the working directory — the whole setup
  lives inside the project folder.

## What we learned

1. **A model can "complete" a tool turn without calling any tool — and
   look successful doing it.** Our first single-turn design (tools +
   `output_type` on one `Agent`) finished with exit 0, `tool_calls: 0`,
   and a plausible-looking "Nothing new" digest fabricated from no data
   (`runs/attempt3-single-turn-no-tool-calls.log`). A later fetch turn
   answered in a single LLM call with invented per-feed counts
   (`runs/attempt6-hallucinated-fetch.log`). Defenses that worked:
   split fetching and summarizing into two turns, keep `forced_tools`,
   and **verify `result.stats["tool_calls"] > 0` in code** — FeedWatch
   exits 1 rather than write a digest when no tool was called.
2. **Session reuse across runs is a liability for batch agents.** A
   long-lived session (a) grew until a digest-turn request hit 5,249
   tokens against Ollama's default 4,096-token context and was rejected
   with HTTP 400 (`runs/attempt4-two-turn-context-overflow.log`), and
   (b) contained earlier no-tool answers that primed the model to skip
   the tool again. Fresh session per run + durable state in an
   application file fixed both. Chat history is not a database.
3. **Structured output validates syntax, not truth.** With Pydantic
   validation passing, the 1.5B model still produced a digest titled
   "Nothing new since the last run" over 4 fetched items with zero
   highlights (run 1), and a headline claiming "8 new items" for 4
   items (run 2). We added `enforce_digest_invariants()` — program-side
   checks that fix the headline/count, drop highlights that don't match
   a fetched item, and synthesize highlights from real items when the
   model returns none. Runs 3–5 (with guardrails) were fully accurate.
   Lesson: let the model propose; let code enforce invariants. Links
   were never model-generated at all — the renderer joins highlights to
   fetched items by title and prints the fetched URL.
4. **The 60-second default provider timeout is a hosted-model
   assumption.** `providers.*.timeout_seconds` defaults to 60; on a
   slow local model a single prompt-processing pass exceeds that and
   the run dies with a provider TIMEOUT
   (`runs/attempt5-provider-timeout.log`). Setting the Ollama preset to
   900s fixed it. The gateway's own default turn timeout (90s) is a
   separate knob, raised via `agent.run(timeout_seconds=...)`.
5. **Local context windows are a real constraint for multi-turn
   agents.** Ollama's default `num_ctx` of 4096 was too small for a
   two-turn session (see lesson 2); we recreated the model with
   `PARAMETER num_ctx 16384`. Hosted models hide this; local builders
   hit it immediately.
6. **Turn budgets end turns politely, not promptly.** Fetch turns kept
   ending with "Stopped: turn time limit reached (300s/300s)" *after*
   completing their tool calls — the brain's elapsed budget is checked
   between steps while individual provider calls run long. Setting
   `brain.plan_auto_scale.max_elapsed_ms` to 900000 in `agents.json`
   did **not** raise the observed 300s stop in 0.1.22 (see Bugs). The
   practical consequence for us was nil (work was done before the
   stop), but anyone building long-running turns should know the
   message is a soft stop, not a failure.
7. **Auto fact extraction costs a model call per turn.** It is on by
   default; for a batch agent it is pure overhead, so we disabled it
   (`brain.auto_fact_extraction.enabled=false`). Worth auditing the
   other brain defaults for headless use.
8. **Backlog paging beats one giant digest.** `--max-items` bounds how
   many unseen items a run digests; the rest stay unseen and drain over
   later runs (runs 1→2→4 drained 4+4+2 of a 10-item feed). For a
   monitoring tool this is the right failure mode: bounded work per
   run, nothing silently dropped — ids are committed to the state file
   only after a digest is written.
9. **Partial fetch is graceful.** In run 3 the fetch turn's budget
   stopped it after 1 of 2 feeds; the digest covered the fetched feed
   (HN) and the other feed's items simply remained unseen for the next
   run. No corruption, no fabricated coverage.

## Bugs / rough edges

- **`brain.plan_auto_scale.max_elapsed_ms` appears ineffective via
  `agents.json`.** Setting it to 900000 (correctly nested — the flat
  form is rejected with a validation warning, which is good) did not
  change the observed 300s turn stop across multiple runs on 0.1.22.
  Either the runner-options bridge reads a different source, or the
  budget is derived per plan. Filed here as a finding; we did not patch
  the SDK.
- **Sandbox/proxy friction (environmental, not an SDK bug):** this VM's
  `no_proxy` contains bracketed IPv6 entries (`[::1]`, …) that make
  httpx raise `InvalidURL: Invalid port: ':1]'` before *any* request,
  including to localhost — the official `examples/sdk/tool_agent.py`
  fails identically (`runs/attempt1-provider-error.log`). FeedWatch
  normalizes the variable in-process (`prepare_network_env()`). Worth
  a docs note for proxied environments, since the error message points
  nowhere near the cause.
- **`ollama pull` could not fetch the model registry through this
  VM's egress proxy** (stalled at "pulling manifest" indefinitely), so
  the model was sideloaded: GGUF downloaded from HuggingFace +
  `ollama create`. Environmental, but the workaround is reusable.
- **Small-model tool discipline is probabilistic.** Even with
  `forced_tools`, one fetch turn in seven attempts answered without
  calling the tool (attempt 6). The stats check caught it; without
  that check the failure is silent. We would love a runtime-level
  "require ≥1 tool call or fail the turn" option.
