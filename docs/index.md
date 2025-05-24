# django-pgtransaction

django-pgtransaction offers a drop-in replacement for the default `django.db.transaction` module which, when used on top of a PostgreSQL database, extends the functionality of that module with Postgres-specific features.

At present, django-pgtransaction offers an extension of the `django.db.transaction.atomic` context manager/decorator which allows one to dynamically set [transaction characteristics](https://www.postgresql.org/docs/current/sql-set-transaction.html) including:
- [Isolation level](https://www.postgresql.org/docs/current/transaction-iso.html)
- Read mode (READ WRITE/READ ONLY)
- Deferrability (DEFERRABLE/NOT DEFERRABLE)
- Retry policy for Postgres locking exceptions

See [module docs](module.md) and the quickstart below for examples.

## Quickstart

After [installation](installation.md), set transaction characteristics using [pgtransaction.atomic][]:

### Isolation Levels

Set the isolation level for specific consistency guarantees:

```python
import pgtransaction

with pgtransaction.atomic(isolation_level=pgtransaction.SERIALIZABLE):
    # Do queries with SERIALIZABLE isolation...
```

There are three isolation levels: `pgtransaction.READ_COMMITTED`, `pgtransaction.REPEATABLE_READ`, and `pgtransaction.SERIALIZABLE`. By default it inherits the parent isolation level, which is Django's default of "READ COMMITTED".

### Read-Only Transactions

Read-only mode can be used queries that don't modify data:

```python
with pgtransaction.atomic(read_mode=pgtransaction.READ_ONLY):
    # Can only read, not write
    results = MyModel.objects.all()
```

### Deferrable Transactions

Prevent serialization failures for long-running queries by blocking:

```python
with pgtransaction.atomic(
    isolation_level=pgtransaction.SERIALIZABLE,
    read_mode=pgtransaction.READ_ONLY,
    deferrable=pgtransaction.DEFERRABLE
):
    # Long-running read-only query that won't cause serialization conflicts
    analytics_data = expensive_query()
```

Note: `DEFERRABLE` only works with `SERIALIZABLE` isolation level and `READ_ONLY` mode.

### Retries for Concurrent Updates

When using stricter isolation levels like `pgtransaction.SERIALIZABLE`, Postgres will throw serialization errors upon concurrent updates to rows. Use the `retry` argument with the decorator to retry these failures:

```python
@pgtransaction.atomic(isolation_level=pgtransaction.SERIALIZABLE, retry=3)
def do_queries():
    # Do queries...
```

!!! note

	The `retry` argument will not work when used as a context manager. A `RuntimeError` will be thrown.

By default, retries are only performed when `psycopg.errors.SerializationError` or `psycopg.errors.DeadlockDetected` errors are raised. Configure retried psycopg errors with `settings.PGTRANSACTION_RETRY_EXCEPTIONS`. You can set a default retry amount with `settings.PGTRANSACTION_RETRY`.

[pgtransaction.atomic][] can be nested, but keep the following in mind:

1. The isolation level cannot be changed once a query has been performed.
2. The retry argument only works on the outermost invocation as a decorator, otherwise `RuntimeError` is raised.

## Compatibility

`django-pgtransaction` is compatible with Python 3.9 - 3.13, Django 4.2 - 5.1, Psycopg 2 - 3, and Postgres 13 - 17.

## Other Reading

Check out the [Postgres docs](https://www.postgresql.org/docs/current/transaction-iso.html) to learn about transaction isolation in Postgres. 
