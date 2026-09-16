# F7 worked example — fabricated placeholder content

Every file under this directory is fiction, written to demonstrate the file-location +
frontmatter + prose-body shape documented in `../f7-telos-identity-schema.md`. Principal "Alex
Doe" and assistant "Nova" do not exist. No content here was copied from any real LifeOS install.

Run the proof:

```bash
python3 docs/lifeos-exit/f7-example/probe_bare_read.py
```

It loads every `.md` file below with a plain file read and a ~10-line hand-rolled frontmatter
splitter — no `pyyaml`, no LifeOS import, no parser beyond the standard library — and asserts each
one carries the three required `pai-freshness-v1` keys and a non-empty prose body.
