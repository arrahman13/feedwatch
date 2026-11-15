# FeedWatch — demo video storyboard (target: 3–4 minutes)

**Format:** terminal screen recording, 1080p. One window, large font.
Narration can be voice-over or captions; the beats below work either way.
Everything shown is a real run against a local Ollama model — no API key,
no hosted service.

## Before recording (setup, off-camera)

```bash
cd feedwatch
source .venv/bin/activate
export OPENMINION_HOME="$PWD"
ollama serve &                 # if not already running
rm -rf digests .feedwatch      # clean slate: no state, no old digests
```

Have a browser tab open on
`https://github.com/OpenMinion/openminion/releases` for the cross-check
in beat 3.

**Timing note:** on the throttled shared VM used for the reference runs,
each run took 15–20 minutes (local 1.5B model at ~30 tokens/s). On a
normal laptop or with a hosted model (e.g. Cortensor Portal) the same
runs take seconds to a couple of minutes. If recording against a slow
local model, record the terminal at full length and speed up the waiting
parts in editing — the digest output is the payoff, not the wait.

## Beat 1 — The problem (0:00–0:30)

- Show the three default feeds in `feedwatch.py` (`DEFAULT_FEEDS`).
- Say: "Builders depend on projects like OpenMinion and Ollama, but
  release notes are easy to miss. FeedWatch is a monitoring agent built
  on the OpenMinion 0.1 Python SDK. It watches release and news feeds
  and writes a digest of only what's new — once."

## Beat 2 — First run: the agent fetches, then summarizes (0:30–2:00)

- Run (one feed, `--max-items 10` so the whole current backlog is
  digested in this run — beat 4 depends on the backlog being drained):
  ```bash
  python feedwatch.py --only openminion-releases --max-items 10
  ```
- Narrate the two turns while it runs:
  - "Turn one: the agent fetches its own inputs. Feed fetching is an
    OpenMinion tool — `@tool(min_scope="READ_ONLY", idempotent=True)` —
    listed in `forced_tools`, and the program checks
    `stats['tool_calls']` afterwards: if the agent made no tool calls,
    the run refuses to write a digest. A digest without fetched data
    would be fabricated."
  - Point at the `[feedwatch] fetch turn: tool_calls=...` diagnostics
    line when it appears.
  - "Turn two, same session: `output_type=Digest` — the answer is a
    validated Pydantic model, not free text. Program-side guardrails
    then fix the facts a small model gets wrong — the headline count,
    and 'nothing new' claims when items exist."
- When the digest prints, scroll through the highlights. Note the
  `[feedwatch] session=... thread=... run_state=...` diagnostics line.

## Beat 3 — Cross-check against reality (2:00–2:40)

- Open the saved file: `cat digests/digest-*.md` (latest).
- Switch to the browser tab of OpenMinion releases and show that the
  top highlight matches the latest real release, and that the link in
  the digest is the real release URL.
- Say: "Links are copied from the fetched items by the program — the
  model never writes URLs, so they can't be hallucinated."

## Beat 4 — Second run: repeatability (2:40–3:20)

- Run the exact same command again:
  ```bash
  python feedwatch.py --only openminion-releases --max-items 10
  ```
- The digest headline is **"Nothing new since the last run"** with zero
  highlights, because beat 2 drained the feed's backlog into the state
  file. (Mention: `--max-items` bounds one run's intake, so a big
  backlog normally drains over successive runs.)
- Show `.feedwatch/state.json` (the seen-item ids).
- Say: "The tool de-duplicates against this state file, and ids are only
  committed after a digest is written — a failed run can't lose items.
  That's what makes the workflow safe to run on a schedule."

## Beat 5 — Provider neutrality + close (3:20–4:00)

- Run `python feedwatch.py --only hn-frontpage --max-items 5` (or show
  the pre-recorded run 3 log in `runs/` if time is tight): a different
  feed produces fresh highlights with the same code path.
- Show the setup lines from the README: the same code runs on Cortensor
  Portal by changing only `openminion setup` — no code edits.
- Close: "FeedWatch: a small OpenMinion agent that turns noisy feeds
  into a once-only digest. Code, building-block notes, and lessons
  learned are in the repo."
