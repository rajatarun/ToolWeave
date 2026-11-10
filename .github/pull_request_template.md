## Summary

What changed and why?

## Testing

- [ ] `python3 -m pytest tests/ -q`
- [ ] Docs updated (if behavior or setup changed)

## Security / safety

Does this touch MCP auth, `plan_guard`, egress, observatory proposals, or spec
ingestion? If yes, describe how invariants in `CLAUDE.md` are preserved.

## Checklist

- [ ] No secrets, tokens, or account-specific identifiers added to docs or code
- [ ] Vendored directories (`cipherweave_verify`, etc.) unchanged unless synced
      via the documented scripts
