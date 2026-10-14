# Delta: cli

## ADDED Requirements

### Requirement: [git] section
`orchestra.toml` SHALL accept a `[git]` section (`author`, `co_author`, `run_hooks`); invalid
`author` or `co_author` values SHALL be config errors. `orchestra init` SHALL write the section
with comments.
