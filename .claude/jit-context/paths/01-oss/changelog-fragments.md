---
title: "Changelog fragments"
match: changelog.d/
---

One file per pull request, so two open PRs never touch the same file. `CHANGELOG.md` is assembled
from these at release time and the fragments are deleted.

**Name:** `<issue>.<section>.md`, where the section is a Keep a Changelog heading, lowercased:
`added`, `changed`, `deprecated`, `removed`, `fixed`, `security`.

**Body:** a single top-level `-` list. No headings, no raw HTML, no unclosed fences. Name the issue
in the text as well as the filename -- the filename is metadata, and metadata does not survive being
read out of context.

**Do not hand-edit `CHANGELOG.md`** while this directory exists. The fold overwrites it and deletes
the fragments; an entry written directly into the file is lost at the next release, silently,
because the fold has no way to know it was meant to stay.

Check before pushing:

```bash
python3 scripts/assemble_changelog.py --check --check-links
```
