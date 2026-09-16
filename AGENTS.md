# MCP project instructions

## Changelog

Maintain [CHANGELOG.md](CHANGELOG.md) for this MCP repository.

- Update the changelog in the same task as meaningful changes to MCP behavior,
  configuration, data handling, startup, compatibility, or project documentation.
- Use dated entries, newest first. Extend the related entry for work on the same
  date instead of duplicating it. Do not invent release numbers.
- Describe the problem addressed and the resulting behavior. Clearly distinguish
  the current implementation from earlier approaches that it replaced.
- Record relevant validation actually performed and any remaining limitations.
  Distinguish automated tests, cache-copy checks, and live MCP validation.
- Include data repairs and operational changes when they affect users. Link to
  supporting reports when available; never record credentials or secrets.
- Keep entries concise and factual. Documentation-only updates do not require
  another live MCP run; check their content and links as appropriate.

## Investigation and validation code

- Put ad hoc investigation, profiling, migration-validation and live-check scripts
  in `study/`, including copied source baselines used by those tools.
- Do not put executable tool code in `data/`. That directory holds runtime data,
  caches, logs and generated validation evidence/reports.
- Run study scripts from the repository root and keep their output paths explicit.
  Update script references when relocating tools. Production code belongs in
  `src/`; automated regression tests belong in `tests/`.
