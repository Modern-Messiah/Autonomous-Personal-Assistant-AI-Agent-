# PostgreSQL Apartment Upserts Design

## Goal

Make apartment persistence idempotent under concurrent manual searches and monitor
ticks, without rolling back unrelated work in the surrounding transaction when both
paths process the same listing.

## Validated finding

Three repository functions use a read-before-write sequence:

- `upsert_apartment_records()` selects by `(source, external_id)` and URL before
  inserting;
- `upsert_apartment_feedback()` selects the composite key before inserting;
- `mark_apartments_seen()` selects existing composite keys before inserting.

Two transactions can both observe an absent row and then attempt the same insert. The
database correctly raises `IntegrityError`, but the exception aborts the transaction,
which can also roll back monitor timestamps and other writes.

The relevant database constraints already exist:

- `apartments(source, external_id)` is unique;
- `apartments.url` is independently unique;
- `apartment_feedback(user_id, apartment_id)` is the primary key;
- `seen_apartments(user_id, apartment_id)` is the primary key.

## Scope decision

This change covers the three apartment-related functions named above.

`upsert_telegram_user()`, `upsert_monitor_settings()`, and
`replace_active_search_criteria()` contain related concurrency risks. The last function
also needs a schema constraint before it can use a true upsert. They are recorded as
follow-up work rather than silently expanding this fix into a wider persistence
redesign.

## Design

### Apartment records

Use `sqlalchemy.dialects.postgresql.insert()` with:

```text
ON CONFLICT (source, external_id)
DO UPDATE SET url = excluded.url, payload = excluded.payload
RETURNING apartment row
```

`(source, external_id)` is the authoritative listing identity. The statement does not
rewrite `source` or `external_id` during conflict handling.

Normalize one batch before executing it:

1. deduplicate values by `(source, external_id)`;
2. preserve the last payload for a repeated key, matching the final state of the
   current loop;
3. execute one bulk upsert;
4. map returned ORM rows by identity;
5. reconstruct the result in the original input order, including repeated inputs.

Callers currently pair returned records with their input apartments, so preserving
order and cardinality is part of the repository contract. PostgreSQL `RETURNING` order
must not be assumed.

The separate `UNIQUE(url)` constraint remains. If one URL arrives with two different
authoritative identities, PostgreSQL may still reject the inconsistent data. That is
not the `/search` plus monitor race described by this finding: those paths carry the
same source and external ID. Silently merging different identities by URL could attach
feedback and seen history to the wrong listing, so dual-key reconciliation is not
included in this change.

### Apartment feedback

Deduplicate input apartment IDs, then bulk insert feedback rows with:

```text
ON CONFLICT (user_id, apartment_id)
DO UPDATE SET
    decision = excluded.decision,
    decided_at = excluded.decided_at,
    deleted_at = NULL
RETURNING feedback row
```

The update deliberately preserves `notion_page_id` and `notion_synced_at`. Re-saving a
soft-deleted apartment restores it by clearing `deleted_at`, matching current behavior.

Map returned rows by apartment ID and reconstruct the list in input order.

### Seen apartments

Deduplicate apartment IDs and bulk insert links with:

```text
ON CONFLICT (user_id, apartment_id) DO NOTHING
RETURNING apartment_id
```

Only IDs returned by PostgreSQL were newly inserted. Convert those IDs back to
`ApartmentRecord` objects in the original input order, preserving the current meaning
of the return value: apartments newly marked as seen by this call.

This removes the race-prone pre-selection from the write path.
`get_unseen_apartment_records()` remains available for read-only callers.

### Transaction behavior

The functions continue to flush through the caller-owned `AsyncSession`; they do not
commit or roll back. Expected duplicate writes are handled by PostgreSQL and no longer
poison the surrounding transaction.

Unexpected constraint violations, including conflicting URL identities or foreign-key
errors, continue to fail loudly.

## Alternatives rejected

Keeping select-then-insert and catching `IntegrityError` would require a savepoint and
another query for every conflict. It is slower and easier to misuse because a failed
statement invalidates the transaction until rolled back.

Serializing all searches with application locks would reduce concurrency and would not
protect writes from another process that ignores the lock.

Removing `UNIQUE(url)` would hide inconsistent parser identity data and is not required
to solve concurrent processing of the same listing.

## Tests and validation

Repository concurrency behavior must be tested against PostgreSQL. SQLite and SQL text
compilation cannot prove PostgreSQL conflict semantics.

Add integration tests that:

- start two independent sessions and concurrently upsert the same apartment identity;
- verify both calls succeed and only one apartment row exists with a valid final
  payload;
- verify return order and cardinality for mixed and repeated apartment inputs;
- concurrently upsert the same feedback key and verify one row with a complete valid
  decision;
- verify feedback upsert clears `deleted_at` but preserves Notion sync metadata;
- concurrently mark the same apartment as seen and verify one link exists without an
  `IntegrityError`;
- verify only the transaction that inserted a seen link reports it as newly seen;
- verify a deliberate same-URL/different-identity collision still fails;
- verify unrelated writes in each successful transaction can commit.

Configure the CI test job with a PostgreSQL service and run migrations before repository
integration tests. Keep pure unit tests independent of PostgreSQL.

Run the full pytest, Ruff, strict mypy, and migration upgrade gates.

## Remaining risks

- A URL reused with a different source/external identity is still a data conflict by
  design.
- Last-writer-wins updates can replace one fresh payload with another concurrent fresh
  payload; they cannot create duplicate identity rows.
- User, monitor-settings, and active-criteria upserts retain their separate race risks
  until follow-up work adds the required conflict handling and constraints.

## Non-goals

- changing apartment identity rules or existing unique constraints;
- merging records that disagree on source/external ID but share a URL;
- changing transaction ownership or adding implicit commits;
- fixing every select-before-insert repository function;
- adding distributed locks or serializable transaction isolation.
