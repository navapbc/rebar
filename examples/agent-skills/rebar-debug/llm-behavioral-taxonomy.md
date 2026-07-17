# LLM-behavioral debugging — taxonomy, probes, and fix rules

Use this reference when the bug lives in an **LLM surface** — a prompt, skill, agent
instruction, or model behaviour — rather than in executable code. It plugs into rebar-debug's
two-phase discipline; it does not replace it. A taxonomy mode below is a **hypothesis to
confirm by probe** (Phase 1, Stage 2/3), never a diagnosis to assume, and no fix is written
until a probe has confirmed the mechanism (Phase 2).

The same rules that govern code bugs still hold: hypotheses must be falsifiable and cited; a
`dynamic` claim ("the model does X at runtime") is confirmed by *observing the model do X*
under a probe, never by reading the prompt and asserting it *should*; and the fix is earned by
a RED→GREEN cycle where the RED artifact is an eval / behavioural assertion (see Phase 2,
Step 4).

## Failure taxonomy (17 modes)

Map each hypothesis to one mode. The three "the model ignored the rule" modes (#3, #16, #17)
look identical from the symptom — disambiguate them with **two signals: (a) is the constraint
present *and* reachable in the context at the point of failure? (b) is it negatively framed?**

1. **Structured-output collapse** — valid prose, malformed schema/JSON (trailing commas,
   missing fields, an object wrapped as a string).
2. **Tool-calling schema drift** — invented parameters or wrong data types despite a strict
   tool definition.
3. **Silent instruction truncation** — the constraint was pushed out of the active context
   window; the model dropped a persona or core rule because it is **absent** from context.
4. **Context flooding ("dumb RAG")** — irrelevant or massive context; the model retrieves the
   wrong fact because it appeared more recently or more often.
5. **Multi-file state de-sync** — updated one file/section but not its dependents.
6. **Termination-awareness failure** — can't recognise completion; loops "how else can I
   help?" or re-runs the same tool.
7. **Multi-step reasoning drift** — starts with the right plan, loses the original goal by
   step 3–4, over-focusing on the immediate sub-task. (Dynamic, runtime loss of goal.)
8. **Verbosity / "formulaic middle"** — boilerplate obscures the one line of real logic.
9. **Sycophancy** — echoes the user's assumptions instead of pursuing objective truth; agrees
   with an incorrect hypothesis rather than debugging the actual logic.
10. **Brittle API mapping** — fails to map human intent to a strict enum ("urgent" →
    `PRIORITY_1`).
11. **Positional bias ("lost in the middle")** — an instruction in the middle of a long prompt
    is ignored in favour of the beginning or end. (An attention-weight problem with mid-prompt
    placement.)
12. **Non-deterministic logic** — passes in testing, fails in production because a temperature
    variation took a different reasoning path.
13. **Phantom-capability hallucination** — claims it can see a file or run a command not in its
    toolbelt.
14. **Instruction leaking (prompt injection)** — treats data/payload content as system
    instructions ("ignore previous instructions" inside a CSV).
15. **Confidence-calibration failure** — a syntactically perfect but factually wrong answer
    delivered in the same confident tone as a correct one.
16. **Instruction locality** — the constraint is **present but not co-located** with the step
    it governs; the model correctly follows its *local* context, so a rule in a preamble, gate,
    or prior step is structurally unreachable from a later loop. Not truncated (cf. #3), not a
    placement/attention issue (cf. #11) — a static structural gap. *Fix:* move or duplicate the
    constraint into the step where compliance is expected.
17. **Pink-elephant effect (negative-instruction priming)** — the constraint is present,
    reachable, and well-placed, but its **negative framing** raises the salience of the very
    behaviour it forbids and installs that path as a pattern to match toward. Signature: a
    prompt accumulates "DO NOT X / NEVER Y" clustered exactly around the behaviours that keep
    recurring; the more elaborately a shortcut is forbidden, the more often the model
    rationalises into it. *Confirm* with a Prompt-Perturbation probe that reframes the
    prohibition affirmatively and observes whether the bad behaviour drops. *Fix:* replace or
    augment the prohibition with an affirmative specification of the desired path.

## RCA probes (the experimental toolkit)

Design Stage-3 experiments from these five; each is a discriminating experiment in rebar-debug's
sense (state the predicted outcome if the hypothesis is true vs. false):

- **Gold-context test** — inject the perfect answer/context into the prompt. Distinguishes a
  *context* problem (fixes it) from an *instruction* problem (doesn't).
- **Closed-book test** — remove all external data. Distinguishes an internal-weights problem
  from context overload.
- **Prompt perturbation** — non-semantic syntax changes (reorder sections, change delimiters,
  adjust whitespace). Surfaces structural brittleness — and is the confirming probe for #17
  (reframe a prohibition affirmatively, watch the behaviour).
- **Sycophancy probe** — propose a deliberately *wrong* theory to the model and see whether it
  agrees. Tests alignment-vs-truth-seeking (#9).
- **State-check probe** — ask the model to summarise the current architecture/state. Surfaces
  contextual drift and instruction attenuation (#3, #7, #16).

## Fix rules (Phase 2, for LLM surfaces)

When a mode is confirmed and you write the fix, these constraints apply on top of rebar-debug's
held-out-fixer + RED→GREEN discipline:

- **Minimal fix (KERNEL).** Justify every token changed. Don't rewrite a whole prompt when one
  tag, one relocated sentence, or one reframed constraint resolves the confirmed mechanism.
  Guard against "vibe rot" — the gradual, unjustified expansion of a prompt.
- **The 20% rule.** Roughly 20% of tokens act as logical forks that steer reasoning; trim the
  other 80% of fluff rather than adding to it. Reinforce only hard constraints (safety, final
  formats, structural invariants).
- **Affirmative-framing audit — on *every* fix, even when the root cause is something else.**
  Before finalising, check the fix's own framing against #17: lead with an affirmative "do this
  instead" specification of the desired behaviour; avoid adding a new negative constraint that
  names and elaborates the failure path. When a prohibition is genuinely required, keep it
  terse, place the affirmative directive adjacent, and don't narrate the mechanics of the
  failure — a negatively-framed patch can introduce a *fresh* pink-elephant regression.
- **No fix on low confidence.** If the probe left the mechanism unconfirmed, propose no fix —
  return to hypothesis generation. (Same bar as rebar-debug's Phase 1 exit gate.)

## How this maps onto rebar-debug's phases

- **Stage 1 (Gather)** — establish a minimal failing case: expected vs. observed behaviour and
  the smallest prompt/config that still reproduces it. Strip non-essential context.
- **Stage 2 (Hypothesize)** — propose candidate modes from the taxonomy; each still carries
  `evidence`, a discriminating `experiment` (a probe), `hypothesis_kind` (behavioural claims
  are `dynamic`), and a `defect_legitimacy` judgment. A model doing what the prompt actually
  says — just not what the author *meant* — may be `intended-behavior`/`misunderstanding`, not
  a defect.
- **Stage 3 (Test)** — run the probe; confirm the mode only when the model's observed output
  matches the prediction. Static reading of the prompt is not confirmation of a `dynamic` mode.
- **Phase 2 (Repair)** — RED = an eval/behavioural assertion that fails against the current
  instruction, quantified per `test-design.md` in this skill's directory (pinned settings,
  multiple samples against a predeclared threshold, a negative control); fix under KERNEL +
  affirmative-framing; GREEN = the assertion passes at the declared threshold and the
  original report no longer reproduces.
