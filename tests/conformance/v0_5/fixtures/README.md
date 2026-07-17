# Dynamic fixture contract

Future scenario fixtures must generate Skill IDs, Skill directories, task objects, Artifact
contents, paths, and correlation IDs at runtime. Each scenario needs at least three semantic input
variants and a reverse or negative variant. Fixtures must not alter the production Composition Root
or introduce a success switch.

Recovery evidence must terminate the service process and start a new process against the same
PostgreSQL data. Changing an in-memory object is not a restart.
