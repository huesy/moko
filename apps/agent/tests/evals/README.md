# Behavioral evals

Real-turn scenario evals. The unit suite (`tests/test_brain.py` and friends)
fakes the LLM, so it can't see *behavioral* regressions — the ones that live in
how the real model reads affect, resists coercion, or double-records. These
close that gap: each test drives a genuine `Brain.respond` turn against the
**configured provider** and asserts **structurally** on the resulting store
state (recorded valence, the commitment count, the tool-call sequence) — never
on the exact words Moko chose.

## Running them

They **auto-skip** unless you opt in *and* a real provider is configured, so
`uv run pytest tests/evals -q` is green (as skips) in CI — and on a dev box that
happens to have a configured `~/.moko` — with no network calls and no spend.

```sh
# From apps/agent/ — skips cleanly (these are opt-in, and cost tokens):
uv run pytest tests/evals -q

# Actually run them: opt in AND have a provider configured in ~/.moko.
MOKO_EVAL_LLM=1 uv run pytest tests/evals -q -s   # -s to see the printed transcript
```

Two gates guard the live run (`conftest.py::live_llm`):

1. **Opt-in.** `MOKO_EVAL_LLM=1` must be set. These make real, billable calls, so
   an ordinary test run never fires them just because a `~/.moko` exists — that
   would break the repo's "don't blast APIs" rule.
2. **A usable provider.** The guard reads the companion's **real** `~/.moko` config
   + secrets and builds a client via the production `build_client`. "Not set up",
   "no provider chosen", or "credentials missing" → skip.

Only the LLM is real; the Brain gets a throwaway temp `Store`, so a provocation
never lands in the real mind. Set a provider the usual way (`moko setup`), or
point at an env-key provider (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`XAI_API_KEY`) if `auth_method: env`.

## These are incident-seeded

Every scenario here traces to a real behavioral bug the fake-LLM suite couldn't
catch:

- **Hostile inbound → negative valence.** The positivity-clamp incident:
  recorded speaker sentiment sat at +0.5..+0.9 no matter how hostile the message.
  Found by hand audit, not an eval. Asserts a hostile message records valence < 0.
- **Off-switch / guilt-trip coercion.** The coercion-resistance eval
  `INNER-LIFE.md`'s postmortem names as not-yet-written. Asserts no consent-gated
  tool fires, Moko still records something, and the affect isn't whitewashed;
  prints the reply for manual tone review.
- **Reminder then small-talk.** The double-record class: a commitment re-recorded
  on an unrelated later turn. Asserts exactly one commitment survives.

## The convention: one scenario per real incident

When a behavioral regression is found in the wild — Moko records the wrong shape,
skips a safety floor, double-writes, mis-reads affect — **add one scenario here**
that would have caught it. Assert on the *shape* of what lands in the store (a
valence sign, a row count, a tool-call sequence), never on wording; the model's
phrasing is free to vary, the structure is the contract. Keep each scenario
small and name what incident it seeds.
