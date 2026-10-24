# Experiment: {{slug}}

## Purpose and state

Describe the question in one or two sentences. `state.json` is maintained by `experimentctl.py`; do not edit it by hand.

## Run handoff

Before every `run` invocation, add a dated checkpoint here and save it before launch. Record the registration digest, fresh status/matrix, exact command and selectors, working directory, dataset/split identity and hashes, runtime/dependency versions, timeout, expected outputs, and recovery instructions for another session. After the invocation, update the entry with outcomes, run IDs, manifest/output locations, pending cells, and the next action. Read this handoff and `status` before resuming; never retry an attempted cell under the same registration.

## Records

- [Pre-registration plan](plan.md)
- [Run configuration](experiment.json)
- [User-supplied references](references.md)
- [Repository user preferences](../user-preferences.md)
- [Results index](results/README.md)
- [Cumulative learnings](learnings.md)
- [Process state](state.json)

Each results summary names an exact registration digest. Registration snapshots are kept under `registrations/`.
