---
name: experiment-workflow
description: Manage small ablation experiments from pre-registration through a default one-seed screen, optional user-requested five-seed grounding, reviewable user preferences, and evidence-linked results.
---

# Experiment workflow

Use this skill to plan, run, and document a small ablation. The bundled `scripts/experimentctl.py` creates the structure and enforces lifecycle transitions; this skill supplies the scientific decisions and keeps results interpretable.

## User preference record

Keep a repository-wide, reviewable preference file at `experiments/user-preferences.md`. `init` creates it from the bundled template if it is absent and links it from `experiments/README.md`; it preserves an existing file. For an existing repository, run `preferences-init` to create the file and index link without scaffolding another experiment; it also preserves an existing file.

At the start of each new experiment and before a revised registration, review the file with the user: summarize the confirmed preferences and invite corrections or additions. Record only preferences the user states directly or confirms from a proposed summary; never infer a preference from repeated choices or behavior. Update `Last reviewed` and append a dated review-history entry, including when the user confirms no changes. If the user does not want to review it then, continue using the last confirmed version without blocking the experiment.

Preferences can inform tradeoffs such as run budget, turnaround, and reporting detail. They do not determine a hypothesis, metric, success criterion, significance protocol, or run authorization; those remain explicit choices for each experiment. Keep the file repository-local and editable by the user.

## Per-registration state machine

`draft → registered → running → execution_complete → results_pending → results_documented`

State belongs to each registration. One experiment can have several sequential registrations, each with its own digest and lifecycle. `validate` is a guard while the experiment is in `draft`; it is not a persisted state. A request to run is an authorization gate handled by the skill: never invoke `run` until the user explicitly asks for execution.

| State | Allowed action and transition |
|---|---|
| `draft` | Edit `plan.md` and `experiment.json`, validate, then register. Successful registration freezes the exact bytes and enters `registered`. |
| `registered` | After an explicit user request, run unattempted cells. A partial selection returns to `registered`; when every registered cell has a terminal attempt, enter `execution_complete`. |
| `running` | Wait for the active helper or inspect status. Do not start another run. An interruption or unknown outcome enters `uncertain`. |
| `uncertain` | Inspect the recorded PID, logs, process, and outputs. Use `resolve` only after confirming the process stopped; the registration returns to `registered`, or advances to `execution_complete` if all cells are terminal. |
| `execution_complete` | Generate a result summary with `report`; this enters `results_pending`. Completion means every cell has a terminal attempt, including failed or incomplete attempts. It does not mean the workload succeeded. |
| `results_pending` | Fill the summary, append its linked learning entry, then finalize. `report` may create another summary revision while results remain pending. Successful finalization enters `results_documented`. |
| `results_documented` | This registration is terminal. A revised or grounding round starts from an edited draft and a new digest, after all prior registrations are documented. |

Transition details:

- `running → registered` when a selected subset finishes and cells remain unattempted.
- `running → execution_complete` when every matrix cell has a terminal attempt.
- `running → uncertain` after interruption or unknown execution; after inspection and explicit resolution, continue to `registered` or `execution_complete`.
- `execution_complete → results_pending → results_documented` through summary, linked learning entry, and finalization.
- After `results_documented`, a later round is a new registration with its own lifecycle, never a backward transition on the completed registration.

Check `status` before resuming. It prints the allowed next action for each registration. Let the helper update `state.json`; do not edit state, registered snapshots, or run manifests by hand. Never infer that a missing response means a run failed or retry it automatically.

## Durable pre-run and cross-session handoff

The preregistration records the scientific choices; it is not by itself an operational handoff. Before every `run` invocation, save a dated checkpoint in a `Run handoff` section of `experiments/<slug>/README.md`. This is essential for long-running, failure-prone, or session-spanning jobs. Do not leave recovery context only in chat.

Before launch, record the exact registration digest and frozen plan/config links, a fresh `status <slug>` snapshot with attempted and pending variant/seed cells, the exact run command and selectors, working directory, dataset/split identity and hashes, runtime/dependency versions, timeout, expected outputs, and what the next session should inspect first. Save the checkpoint before starting the process. After the invocation, update that entry with the command outcome, run IDs, manifest/output locations, remaining cells, and the next permitted action.

When continuing in another session, read the latest handoff and run `status` before acting. If execution may still be active or its outcome is unknown, inspect the operation lock, PID/process, manifest, logs, and outputs; never start a duplicate. After confirming the process stopped, resolve an uncertain registration explicitly, then continue only its unattempted cells. A failed, incomplete, or launch-error cell is already attempted and cannot be retried under the same registration. Document and finalize that registration, then revise the draft with the retry rationale, validate, and create a new digest for another attempt. Never describe a new registration as a retry of an unchanged attempt without recording why it is needed.

## State-gated actions

Use the state table above as the transition contract. `status` is observational; commands that mutate state must pass the helper's guards.

## Workflow

1. Scaffold: `python path/to/this-skill/scripts/experimentctl.py --root path/to/repository init <slug>`.
2. Fill `experiments/<slug>/plan.md` and `experiment.json`. Predeclare the research question, H1/H0, variables, baselines, one primary metric, aggregation/significance protocol, success criterion, seeds, dataset/split identity, and budget. Start with one seed for the screening run. Treat its result as a pilot; do not claim statistical significance from one seed.
3. Set `defaultDataAmount` to a positive number of examples. Declare a baseline and one or more ablation variants. A variant may override `dataAmount`; otherwise it uses the default. The command must consume `{seed}` and `{data_amount}`. Variant `args` are appended to the base argv.
4. Run `validate <slug>`, resolve all missing choices, then `register <slug>`. Registration freezes exact plan/config bytes; every run names that digest. The helper publishes the complete snapshot atomically and can recover a matching snapshot if interrupted before updating `state.json`; rerun `register` with the same draft to reconcile it.
5. Before each invocation, write the durable `Run handoff` checkpoint described above, especially if the job may outlive the session or fail. Execute only after the user explicitly requests a run. `run <slug> --registration <digest>` runs all declared variants × seeds, sequentially; with the default one-seed set, this is one cell per variant. `--variant` and `--seed` can narrow the matrix. The helper continues to other cells after a failure, records each cell once, and never retries a cell for the same registration.
6. Inspect `status <slug>`. When every matrix cell has a terminal attempt, `report <slug> --registration <digest>` creates a new, non-overwriting result summary under `experiments/<slug>/results/` with links to run manifests.
7. Fill every section in the summary with measured values, analysis against the registered criterion, limits, and decision. Use `learning-add <slug> --registration <digest> --summary <path> --title ... --finding ... --limits ... --decision ...` to append a dated evidence-linked entry; then run `finalize <slug> --registration <digest> --summary <path>` to mark the state `results_documented`.

Five-seed grounding is optional; do not start it unless the user asks. After the screening registration reaches `results_documented`, revise the draft plan/config to declare five seeds, note that this is an optional grounding check, then validate and register a new digest. When the user asks for grounding, that request authorizes running the new five-seed variant × seed matrix. Treat it as a stability check; five seeds alone do not guarantee statistical significance. If the user does not ask for grounding, the documented one-seed screen completes the workflow.

Resolve interrupted execution only after inspecting the process and outputs and confirming the process stopped:

```sh
python path/to/this-skill/scripts/experimentctl.py --root path/to/repository resolve <slug> --registration sha256-REPLACE_WITH_DIGEST --note "Process inspected and stopped; outputs reviewed" --process-stopped
```

Do not register another plan for this experiment until the current registration reaches `results_documented`. To continue with a revised matrix, edit the draft plan/config and register its new digest after documenting the prior registration.

## Reference structure

```text
experiments/<slug>/
  README.md                 # record index and dated pre-run/cross-session handoff
  plan.md                   # human-readable pre-registration
  experiment.json           # seed/data defaults, variants, command and outputs; one seed by default
  references.md             # user-supplied data/source identities
  learnings.md              # append-only dated findings linked to summaries
  state.json                # script-maintained state transitions
  registrations/<digest>/   # immutable plan/config snapshots
  results/<digest>/         # summary revisions, never overwritten
.experiment-runs/<slug>/    # local logs and declared output files
```

The summary records every variant/seed attempt and links a copied manifest stored under its registration's results folder. Process success, output completeness, measured values, and the scientific conclusion remain separate. The helper does not parse arbitrary metrics or produce statistical verdicts. Workloads run locally as the current OS user, are not sandboxed, and may consume local resources; there is no cloud runner.

The run manifest stores the exact argv and stdout/stderr are captured verbatim. Do not place credentials or secret values in command arguments or program output; configured environment variable values are not copied into the manifest.
