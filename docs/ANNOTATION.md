# Annotation workflow

Each bundle keeps immutable raw evidence and stores reviewer work in `annotation/`. `ontology.json` defines labels, `annotations.jsonl` is appendable JSONL, and `adjudication.jsonl` is reserved for conflicts. An annotation points to stable `event_id` values and physical source lines, so it can be audited back to the original rollout.

Run:

```powershell
atk annotate path\to\bundle
```

The offline workbench supports interaction navigation, labels, review status, notes, and downloading an `annotations.jsonl` file. Replace the sidecar file with the downloaded version when you finish a review. Do not publish raw bundles without reviewing sensitive content.
