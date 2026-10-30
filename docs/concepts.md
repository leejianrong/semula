# Concepts

Eight words you will meet in the tutorial. Read this once, or come back when a term is unfamiliar.

```mermaid
flowchart LR
    P[Project] --> E[Entry]
    E --> R[Recorded answer]
    C[Call from your app] --> M{Match a tier?}
    M -- yes --> R
    M -- no, record mode --> K[Parked call]
    K -- an agent answers --> E
```

**Project**
:   A namespace for recordings, usually one per app. Each project has its own entries, aliases and mode. See [One project per app](tutorial/projects.md).

**Entry**
:   One stored prompt with its answer (or answers). Calls are matched against entries. You can edit, pin and template them: [Edit, pin and template](tutorial/editing.md).

**Mode**
:   `record` answers unknown prompts by holding them for an agent. `replay` only serves what is stored and returns an error for anything else. Set it per project.

**Parked call**
:   A call semula has no answer for, in record mode. "Parked" means the HTTP request is held open while it waits. The same call shows up as *pending* in lists and the dashboard. An agent or a person answers it, and the app's request then completes.

**Miss**
:   A call that matched nothing. In record mode it parks; in replay mode it returns an error ([Errors](reference/errors.md)).

**Tier**
:   One way of matching a call to an entry, tried cheapest first: exact, normalised, alias, words, meaning. A weak match is never served silently. It goes to review instead. See [Near misses and review](tutorial/near-misses.md).

**Alias**
:   A human-approved link saying "this prompt may be answered by that entry".

**Cassette**
:   A project exported to a directory of JSON files, so recordings can live in git and be imported in CI. See [Keep recordings in git](tutorial/cassettes.md).

Advanced terms (answerer hook, record-through, claim and lease) are explained where they are used, in [Run it as a service](guide/service.md).
