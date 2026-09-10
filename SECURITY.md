# Security policy

pg_ztype is pre-release software. Report vulnerabilities privately by email to
the maintainer listed in the repository (see the commit history or LICENSE)
rather than in a public issue, and include a reproduction against `make test`'s
disposable cluster where possible. Expect an acknowledgement within a week.

What counts: anything that lets a non-superuser read dictionary bytes or
training queries, crash or corrupt a backend through stored or supplied values,
or escalate through the `SECURITY DEFINER` codec entry points. Stored values
are trusted PostgreSQL data; corruption of raw payloads and native jsonb
containers is detected only structurally, as README.md states.
