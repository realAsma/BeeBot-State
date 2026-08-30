# Design

## One work item, two pieces

A work item is **one row** in the shared `index.jsonl` plus **one work file** of
its own, bucketed by workspace. The row is what you search; the file is what you
read once you have chosen. Recall is therefore two cheap steps rather than a
scan: filter the rows, then open one file.

```text
~/.beebot_states/
├── index.jsonl                        one row per work item — the search surface
├── schema.json                        validates every write
└── home-you-projects-qwen35-recovery-4b1e9c07/   ← bucket = one workspace
    └── qwen35-9b-sft-coding-recovery.json        ← one work file
```

The bucket name is a readable rendering of `cwd` followed by a digest of the
whole path. The digest is not decoration: a work name is only unique within its
`cwd`, so two directories sharing a bucket would resolve to one file and one
record would silently overwrite the other. Truncation and the `/`→`-` flattening
both make the readable half ambiguous, so the digest is what carries uniqueness.

## The row

A short pointer, not the content. Rows stay small and uniform so an agent can
scan every ongoing item — across all workspaces — and still pick one without
pulling any of that prose into context:

```json
{"work_name": "qwen35-9b-sft-coding-recovery", "completion": "open",
 "cwd": "/home/you/projects/qwen35-recovery", "updated": "2026-03-04T18:20:11Z",
 "short_description": "Recover the coding regression after NVFP4 PTQ",
 "work_state_path": "home-you-projects-qwen35-recovery-4b1e9c07/qwen35-9b-sft-coding-recovery.json"}
```

`(cwd, work_name)` is the key. A name only has to be unique in the directory the
work runs in, which is why `cwd` is required on every read and write and not
merely a filing convenience: a piece of work is identified by *where* it runs
and *what* it is.

`cwd` is fixed at initialize, because the bucket is derived from it.
`work_state_path` is stored rather than recomputed, so changing how buckets are
named leaves old rows resolvable.

## The file

```json
{
  "description": "Recover the coding regression after NVFP4 PTQ. Done when HumanEval and MBPP are within 1 pt of BF16.",
  "current_status": "Two LoRA SFT runs done on the 40k code mix. HumanEval 68.4 → 71.2; MBPP still trails by 4.",
  "prior_actions": [
    "Swept LoRA rank 16/32/64 — rank 32 best, rank 64 overfit by epoch 2.",
    "Chased the MBPP gap into the fine-tune for a day; it was the eval harness applying the wrong chat template."
  ],
  "next_steps": ["Re-run MBPP with the fixed template, then sweep LR 1e-5 to 5e-5 at rank 32."],
  "blockers": ["MBPP numbers untrustworthy until the harness fix lands."],
  "artifacts": [{"item": "runs/qwen35_9b_lora_r32/checkpoint-1200", "note": "Best checkpoint so far."}],
  "final_learnings": "Verify the eval harness before blaming the fine-tune."
}
```

`next_steps` and `blockers` are rewritten whole on each save; `final_learnings`
is optional and usually written once at the end.

`prior_actions` carries the dead ends — the most valuable field, because it is
what stops the next session repeating a day you already spent. `artifacts` holds
durable references only; the content they point at lives elsewhere.
