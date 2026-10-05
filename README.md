# File Operations

Plan file changes, inspect the exact proposal, then apply a matching explicit decision.
Planning does not change the selected file tree. A persistent ledger belongs to each
block instance and records admitted plans and actual outcomes.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![File Operations block](media/thumbnail.webp)](media/cover.png)

## Ports

| Direction | Port | Purpose |
| --- | --- | --- |
| Input | `request` | JSON plan ID, revision and explicit operations |
| Input | `decision` | Approve or reject an exact plan ID, revision and hash |
| Output | `plan` | Before/after paths, source fingerprints, expiry and approval hash |
| Output | `receipt` | Applied, rejected, expired, refused, partial or uncertain outcome |
| Output | `issues` | Conflicts, replay indicators and incomplete-apply diagnostics |

An event on `request` only plans. An event on `decision` never replays a retained request.
Manual execution with both inputs populated is refused as ambiguous. No UI field or
button implicitly authorizes a plan. Connect `decision` only to a trusted approval path.
The digest binds the plan; it is **not authentication**, and an LLM output alone is not
proof that a human approved the change.

## Example

Set `allowed_root` to an existing directory dedicated to the workflow. This proposal
copies a regular file to another existing directory without overwriting any target:

```json
{"id":"filing-001","revision":1,"operations":[{"op":"copy","source":"incoming/report.pdf","target":"review/report.pdf"}]}
```

After reviewing the emitted `plan`, send its exact `id`, `revision` and `plan_hash`:

```json
{"id":"filing-001","revision":1,"plan_hash":"COPY_THE_EXACT_EMITTED_HASH","decision":"approve"}
```

Use `reject` to discard the pending proposal without changing source files. To replace
a pending proposal, send the same ID with a strictly newer revision; any old approval
then fails. Replaying an already processed decision returns its receipt and an
`already_processed` issue; it does not repeat the file operation. Consumers should
deduplicate receipts by plan ID, revision and hash.

## Operations and settings

- `copy`: create a new regular file, with mode 0600, from a verified stable source.
- `move`: same-filesystem move to an unused name. File identity and permissions remain.
- `quarantine`: move to a unique name under `.blox-quarantine` within the selected root.
  It never deletes the quarantined data or empties that directory automatically.
- `allowed_operations` restricts what incoming proposals may request.
- `plan_ttl_sec` limits how long a proposal may be approved.
- Advanced: `max_operations`, combined `max_bytes`, retained `max_plans`, `timeout_sec`.

The root is mandatory; an empty value disables file planning and changes. Sources and
targets are explicit relative paths. Parents must already exist except the fixed
quarantine directory, created only after approval. There are no recursive directory
operations, glob expansion, shell commands, silent overwrites or permanent-delete action.
Symlinks in the root, parents, sources and existing targets are refused. Quarantine paths
are reserved: recovery is a separately reviewed operator action using the receipt.
All source hashes are rechecked before the first intended change. Policy or root changes
also invalidate application of an incompatible plan.

## Persistence and partial outcomes

The ledger uses only `get_block_storage_dir()` and survives runs and restarts. It retains
the latest revision per plan ID; it is not an immutable audit archive. Save receipts
downstream if a full history is required. Capacity is bounded; nothing is silently evicted.
The selected tree is not mutated until admission is committed to the ledger.

An approved multi-file plan is **not a filesystem transaction**. Earlier successful
operations remain if a later one fails. The receipt records each created target and
removed source name. Moves use an exclusive hard link followed by unlinking the original
name, so a failed move can temporarily leave two names for the same data. A crash can leave
an `applying` status whose last write is uncertain: automatic retry is refused. Inspect
the actual files and receipt before proposing recovery. Do not infer that a missing
completion record means no side effect occurred.

Operations require a POSIX host with descriptor-relative, no-follow file operations.
They are not an OS sandbox or a defense against a hostile concurrent filesystem owner.
Keep the selected tree stable during planning/application and isolate untrusted processes.
The block does not preserve extended attributes on copies, perform cross-filesystem moves,
or automatically roll back completed operations. Receipts include paths and hashes but
not file contents.

## Verification

Package tests cover no-mutation planning, copy/move/quarantine, exact approvals, expiry,
replay, stale sources, collisions, symlink escapes, crash admission and partial receipts.
Runtime tests use disposable projects and temporary files; browser tests exercise both
managed and linked settings surfaces at desktop and mobile sizes.

## License

Apache-2.0.
