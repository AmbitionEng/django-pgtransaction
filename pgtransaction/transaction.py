from __future__ import annotations

import logging
from functools import cached_property, wraps
from typing import TYPE_CHECKING, Any, Callable, Final, Literal, TypeVar, overload

import django
from django.db import DEFAULT_DB_ALIAS, Error, transaction
from django.db.utils import NotSupportedError

from pgtransaction import config

_C = TypeVar("_C", bound=Callable[..., Any])

READ_COMMITTED: Final = "READ COMMITTED"
REPEATABLE_READ: Final = "REPEATABLE READ"
SERIALIZABLE: Final = "SERIALIZABLE"
READ_WRITE: Final = "READ WRITE"
READ_ONLY: Final = "READ ONLY"
DEFERRABLE: Final = "DEFERRABLE"
NOT_DEFERRABLE: Final = "NOT DEFERRABLE"


_LOGGER = logging.getLogger("pgtransaction")


class Atomic(transaction.Atomic):
    def __init__(
        self,
        using: str | None,
        savepoint: bool,
        durable: bool,
        isolation_level: Literal["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"] | None,
        retry: int | None,
        *,
        read_mode: Literal["READ WRITE", "READ ONLY"] | None,
        deferrable: Literal["DEFERRABLE", "NOT DEFERRABLE"] | None,
    ):
        if django.VERSION >= (3, 2):
            super().__init__(using, savepoint, durable)
        else:  # pragma: no cover
            super().__init__(using, savepoint)

        self.isolation_level = isolation_level
        self.read_mode = read_mode
        self.deferrable = deferrable
        self._retry = retry
        self._used_as_context_manager = True

        if self.isolation_level or self.read_mode or self.deferrable:  # pragma: no cover
            if self.connection.vendor != "postgresql":
                raise NotSupportedError(
                    f"pgtransaction.atomic cannot be used with {self.connection.vendor}"
                )

            if self.isolation_level and self.isolation_level.upper() not in (
                READ_COMMITTED,
                REPEATABLE_READ,
                SERIALIZABLE,
            ):
                raise ValueError(f'Invalid isolation level "{self.isolation_level}"')

            if self.read_mode and self.read_mode.upper() not in (
                READ_WRITE,
                READ_ONLY,
            ):
                raise ValueError(f'Invalid read mode "{self.read_mode}"')

            if self.deferrable and self.deferrable.upper() not in (
                DEFERRABLE,
                NOT_DEFERRABLE,
            ):
                raise ValueError(f'Invalid deferrable mode "{self.deferrable}"')

            if self.deferrable == DEFERRABLE and not (
                self.isolation_level == SERIALIZABLE and self.read_mode == READ_ONLY
            ):
                raise ValueError(
                    "DEFFERABLE transactions have no effect unless "
                    "SERIALIZABLE isolation level and "
                    "READ ONLY mode are used."
                )

    @cached_property
    def retry(self) -> int:
        """
        Lazily load the configured retry value

        We do this so that atomic decorators can be instantiated without an
        implicit dependency on Django settings being configured.

        Note that this is not fully thread safe as the cached_property decorator
        can be redundantly called by multiple threads, but there should be no
        adverse effect in this case.
        """
        return self._retry if self._retry is not None else config.retry()

    @property
    def connection(self) -> Any:
        # Don't set this property on the class, otherwise it won't be thread safe
        return transaction.get_connection(self.using)

    def __call__(self, func: _C) -> _C:
        self._used_as_context_manager = False

        @wraps(func)
        def inner(*args: Any, **kwds: Any) -> Any:
            num_retries = 0

            while True:  # pragma: no branch
                try:
                    with self._recreate_cm():
                        return func(*args, **kwds)
                except Error as error:
                    if (
                        error.__cause__.__class__ not in config.retry_exceptions()
                        or num_retries >= self.retry
                    ):
                        raise

                num_retries += 1

        return inner  # type: ignore - we only care about accuracy for the outer method

    # `typing_extensions` is supported under `TYPE_CHECKING` even if not installed.

    if TYPE_CHECKING:
        from typing_extensions import deprecated

        @deprecated(
            "`execute_set_isolation_level` is deprecated and to be removed. "
            "Use `execute_set_transaction_modes` instead."
        )
        def execute_set_isolation_level(self) -> None: ...
    else:

        def execute_set_isolation_level(self) -> None:  # pragma: no cover
            _LOGGER.warning(
                "`execute_set_isolation_level` is deprecated. "
                "Use `execute_set_transaction_modes` instead."
            )
            self.execute_set_transaction_modes()

    def execute_set_transaction_modes(self) -> None:
        with self.connection.cursor() as cursor:
            transaction_modes: list[str] = []

            if self.isolation_level:
                transaction_modes.append(f"ISOLATION LEVEL {self.isolation_level.upper()}")

            # Only set non-default values.
            if self.read_mode:  # pragma: no branch
                transaction_modes.append(self.read_mode.upper())

            if self.deferrable:  # pragma: no branch
                transaction_modes.append(self.deferrable.upper())

            if transaction_modes:  # pragma: no branch
                cursor.execute(f"SET TRANSACTION {' '.join(transaction_modes)}")

    def __enter__(self) -> None:
        in_nested_atomic_block = self.connection.in_atomic_block

        if in_nested_atomic_block and self.retry:
            raise RuntimeError("Retries are not permitted within a nested atomic transaction")

        if self.retry and self._used_as_context_manager:
            raise RuntimeError(
                "Cannot use pgtransaction.atomic as a context manager "
                "when retry is non-zero. Use as a decorator instead."
            )

        # If we're already in a nested atomic block, try setting the transaction modes
        # before any check points are made when entering the atomic decorator.
        # This helps avoid errors and allow people to still nest transaction modes
        # when applicable
        if in_nested_atomic_block and (self.isolation_level or self.read_mode or self.deferrable):
            self.execute_set_transaction_modes()

        super().__enter__()

        # If we weren't in a nested atomic block, set the transaction modes for the first
        # time after the transaction has been started
        if not in_nested_atomic_block and (
            self.isolation_level or self.read_mode or self.deferrable
        ):
            self.execute_set_transaction_modes()


@overload
def atomic(using: _C) -> _C: ...


# Deferrable only has effect when used with SERIALIZABLE isolation level
# and READ ONLY mode.
@overload
def atomic(
    using: str | None = None,
    savepoint: bool = True,
    durable: bool = False,
    isolation_level: Literal["SERIALIZABLE"] = ...,
    retry: int | None = None,
    *,
    read_mode: Literal["READ ONLY"] | None = None,
    deferrable: Literal["DEFERRABLE"] | None = None,
) -> Atomic: ...


@overload
def atomic(
    using: str | None = None,
    savepoint: bool = True,
    durable: bool = False,
    isolation_level: Literal["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"] | None = None,
    retry: int | None = None,
    *,
    read_mode: Literal["READ WRITE", "READ ONLY"] | None = None,
    deferrable: Literal["DEFERRABLE", "NOT DEFERRABLE"] | None = None,
) -> Atomic: ...


def atomic(
    using: str | None | _C = None,
    savepoint: bool = True,
    durable: bool = False,
    isolation_level: Literal["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"] | None = None,
    retry: int | None = None,
    *,
    read_mode: Literal["READ WRITE", "READ ONLY"] | None = None,
    deferrable: Literal["DEFERRABLE", "NOT DEFERRABLE"] | None = None,
) -> Atomic | _C:
    """
    Extends `django.db.transaction.atomic` with PostgreSQL functionality.

    Allows one to dynamically set transaction characteristics when opening a transaction,
    including isolation level, read mode, and deferrability. Also supports specifying
    a retry policy for when an operation in that transaction results in a Postgres
    locking exception.

    Args:
        using: The database to use.
        savepoint: If `True`, create a savepoint to roll back.
        durable: If `True`, raise a `RuntimeError` if nested within another atomic block.
        isolation_level: The isolation level we wish to be
            used for the duration of the transaction. If passed in
            as None, the current isolation level is used. Otherwise,
            we must choose from `pgtransaction.READ_COMMITTED`,
            `pgtransaction.REPEATABLE_READ` or `pgtransaction.SERIALIZABLE`.
            Note that the default isolation for a Django project is
            "READ COMMITTED". It is not permitted to pass this value
            as anything but None when using [pgtransaction.atomic][]
            is used as a nested atomic block - in that scenario,
            the isolation level is inherited from the parent transaction.
        retry: An integer specifying the number of attempts
            we want to retry the entire transaction upon encountering
            the settings-specified psycogp2 exceptions. If passed in as
            None, we default to using the settings-specified retry
            policy defined by `settings.PGTRANSACTION_RETRY_EXCEPTIONS` and
            `settings.PGTRANSACTION_RETRY`. Note that it is not possible
            to specify a non-zero value of retry when [pgtransaction.atomic][]
            is used in a nested atomic block or when used as a context manager.
        read_mode: The read mode for the transaction. Must be one of
            `pgtransaction.READ_WRITE` or `pgtransaction.READ_ONLY`.
        deferrable: Whether the transaction is deferrable. Must be one of
            `pgtransaction.DEFERRABLE` or `pgtransaction.NOT_DEFERRABLE`.
            DEFERRABLE only has effect when used with SERIALIZABLE isolation level
            and READ ONLY mode. In this case, it allows the transaction to be
            deferred until it can be executed without causing serialization
            anomalies.

    Example:
        Since [pgtransaction.atomic][] inherits from `django.db.transaction.atomic`, it
        can be used in exactly the same manner. Additionally, when used as a
        context manager or a decorator, one can use it to specify transaction
        characteristics. For example:

            import pgtransaction

            with pgtransaction.atomic(isolation_level=pgtransaction.REPEATABLE_READ):
                # Transaction is now REPEATABLE READ for the duration of the block
                ...

            # Use READ ONLY mode with SERIALIZABLE isolation
            with pgtransaction.atomic(
                isolation_level=pgtransaction.SERIALIZABLE,
                read_mode=pgtransaction.READ_ONLY
            ):
                # Transaction is now SERIALIZABLE and READ ONLY
                ...

            # Use DEFERRABLE with SERIALIZABLE and READ ONLY
            with pgtransaction.atomic(
                isolation_level=pgtransaction.SERIALIZABLE,
                read_mode=pgtransaction.READ_ONLY,
                deferrable=pgtransaction.DEFERRABLE
            ):
                # Transaction is now SERIALIZABLE, READ ONLY, and DEFERRABLE
                ...

        Note that setting transaction modes in a nested atomic block is permitted as long
        as no queries have been made.

    Example:
        When used as a decorator, one can also specify a `retry` argument. This
        defines the number of times the transaction will be retried upon encountering
        the exceptions referenced by `settings.PGTRANSACTION_RETRY_EXCEPTIONS`,
        which defaults to
        `(psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected)`.
        For example:

            @pgtransaction.atomic(retry=3)
            def update():
                # will retry update function up to 3 times
                # whenever any exception in settings.PGTRANSACTION_RETRY_EXCEPTIONS
                # is encountered. Each retry will open a new transaction (after
                # rollback the previous one).

        Attempting to set a non-zero value for `retry` when using [pgtransaction.atomic][]
        as a context manager will result in a `RuntimeError`.
    """
    # Copies structure of django.db.transaction.atomic
    if callable(using):
        return Atomic(
            using=DEFAULT_DB_ALIAS,
            savepoint=savepoint,
            durable=durable,
            isolation_level=isolation_level,
            retry=retry,
            read_mode=read_mode,
            deferrable=deferrable,
        )(using)
    else:
        return Atomic(
            using=using,
            savepoint=savepoint,
            durable=durable,
            isolation_level=isolation_level,
            retry=retry,
            read_mode=read_mode,
            deferrable=deferrable,
        )
