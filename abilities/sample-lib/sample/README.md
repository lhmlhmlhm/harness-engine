# The sample directory

Three status folders and nothing else. `sample-change` moves an item's json file between them, and
the borrowed fact reads WHICH folder it is in — the directory is the status, which is what makes a
claimed move checkable instead of self-reported.

The folders are tracked (via `.gitkeep`) so a fresh clone has them: the provider declares this
directory as a capability, and a sample whose capability reports absent on the first `validate` is a
bad first sentence. The json files a run writes here are NOT tracked — see the repository's
`.gitignore`.

An item file looks like:

```json
{"status": "review", "failed": 0}
```
