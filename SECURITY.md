# Security policy

Report vulnerabilities privately through GitHub's vulnerability reporting
form at https://github.com/xvaara/pg_ztype/security/advisories/new, not in a
public issue. Include a reproduction against the disposable cluster
`make test` starts where possible. Expect an acknowledgement within a week.

What counts: anything that lets a non-superuser read dictionary bytes or
training queries, crash or corrupt a backend through stored or supplied
values, or escalate through the `SECURITY DEFINER` codec and registration
functions. Stored values are trusted PostgreSQL data: corruption of raw
payloads and of native jsonb containers is detected only structurally, as
README.md states under "Storage format".
