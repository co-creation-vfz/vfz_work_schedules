# =============================================================================
# database_helpers.py
# MySQL utilities for the VFZ work-schedule integrations.
# Covers: pooled connection management, table names, idempotent DDL builders.
#
# Both integrations share one database, and one table in it -- the DB Resync
# writes work_schedule_non_working_days and the Emails job reads it. That
# contract is why every CREATE TABLE lives here rather than in each
# integration: two copies of the same DDL would eventually disagree, and the
# way you would find out is the Emails job silently reading a column the
# resync had stopped writing.
#
# IMPORTANT: connection management only. Queries belong with the integration
#            that owns the table -- repository.py in the resync, store.py in
#            the emails job -- so there is one place to look per table.
#            Credentials are passed in by the caller, read from segredo.ini.
# =============================================================================

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Optional
from zoneinfo import ZoneInfo

import mysql.connector
from mysql.connector import pooling

# Raised for every driver-level failure: unreachable host, bad credentials, a
# connection lost mid-query. Callers catch this one name rather than importing
# the driver themselves.
DatabaseError = mysql.connector.Error


# ---------------------------------------------------------------------------
# Table names
# ---------------------------------------------------------------------------

# Written daily by the DB Resync, read by both. The presence of a row means the
# user is NOT working that date; absence means they are working.
NON_WORKING_DAYS_TABLE = "work_schedule_non_working_days"

# Owned by the Emails job. One record per approval per recipient, which is what
# makes "notify once per approval, per approver" hold across runs.
APPROVAL_NOTIFICATIONS_TABLE = "approval_notifications"

# The two child tables of approval_notifications. Named after their parent, so
# pointing the job at a scratch notifications table moves its children with it.
APPROVAL_NOTIFICATIONS_NON_WORKING_TABLE = "approval_notifications_non_working"
APPROVAL_NOTIFICATIONS_COMMENTS_TABLE = "approval_notifications_comments"

# Approvals that already existed at go-live and must never be notified on.
BASELINED_APPROVALS_TABLE = "baselined_approvals"

# One row per resync run, written by the DB Resync at the end of every run.
# The point is that it survives the process: the service keeps its last run in
# memory, which means a cron run -- a different process entirely -- never
# appeared on the dashboard, and "last sync" read as the last time somebody
# clicked the button. This table is what makes the two the same answer.
RESYNC_RUNS_TABLE = "work_schedule_resync_runs"


# ---------------------------------------------------------------------------
# Table definitions -- idempotent, safe to run on every start.
#
# Templates, not finished SQL: segredo.ini can point an integration at a
# differently-named table (a staging notifications table, say), and the child
# tables are named after their parent. Baking the defaults in meant a custom
# parent got DEFAULT-named children, and every query then hit a table that did
# not exist. The names come in as arguments so that cannot happen.
# ---------------------------------------------------------------------------

_NON_WORKING_DAYS_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    id                  INT          NOT NULL AUTO_INCREMENT,
    user_id             VARCHAR(64)  NOT NULL,
    user_name           VARCHAR(255)     NULL,
    work_schedule_title VARCHAR(255)     NULL,
    work_schedule_id    VARCHAR(64)      NULL,
    `date`              DATE         NOT NULL,
    -- "testkit" on a row written by the Emails job's test tooling, NULL on a
    -- row written by the resync. unseed only ever deletes the former, so it
    -- can never remove real data.
    seeded_by           VARCHAR(32)      NULL,
    created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- Stops a duplicate (user, date) at the source. Every writer in both
    -- integrations upserts on exactly this key.
    UNIQUE KEY uniq_user_date (user_id, `date`),
    -- Serves the availability lookup: date + user_id IN (...).
    KEY idx_date_user (`date`, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
"""

_APPROVAL_NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    id                     INT          NOT NULL AUTO_INCREMENT,
    approval_id            VARCHAR(64)  NOT NULL,
    notified_approver_id   VARCHAR(64)  NOT NULL,
    task_id                VARCHAR(64)      NULL,
    notified_approver_name VARCHAR(255)     NULL,
    -- "approver", "lead" or "fallback": who was told, and in what capacity.
    recipient_role         VARCHAR(32)  NOT NULL DEFAULT 'approver',
    first_notified_at      DATETIME     NOT NULL,
    last_notified_at       DATETIME     NOT NULL,
    PRIMARY KEY (id),
    -- The dedup guarantee. Two overlapping runs collide here instead of
    -- silently posting the same comment twice.
    UNIQUE KEY uniq_approval_approver (approval_id, notified_approver_id),
    KEY idx_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
"""

_APPROVAL_NOTIFICATIONS_NON_WORKING_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    id              INT          NOT NULL AUTO_INCREMENT,
    notification_id INT          NOT NULL,
    approver_id     VARCHAR(64)  NOT NULL,
    added_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- A set, not a list: re-covering the same approver is a no-op. A later run
    -- compares today's non-workers against this to tell "already covered" from
    -- "a new non-worker joined this approval".
    UNIQUE KEY uniq_notification_approver (notification_id, approver_id),
    -- Constraint names are global to the schema, so they carry the table name
    -- too: two differently-named notification tables would otherwise collide
    -- on the second CREATE.
    CONSTRAINT `fk_{table}_notification`
        FOREIGN KEY (notification_id)
        REFERENCES `{parent}` (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
"""

_APPROVAL_NOTIFICATIONS_COMMENTS_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    id              INT          NOT NULL AUTO_INCREMENT,
    notification_id INT          NOT NULL,
    comment_id      VARCHAR(64)  NOT NULL,
    posted_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- Deliberately NOT unique: a corrected re-notification adds a second
    -- comment to the same record, and a retraction must delete both.
    KEY idx_notification (notification_id),
    CONSTRAINT `fk_{table}_notification`
        FOREIGN KEY (notification_id)
        REFERENCES `{parent}` (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
"""

_BASELINED_APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    id           INT          NOT NULL AUTO_INCREMENT,
    approval_id  VARCHAR(64)  NOT NULL,
    task_id      VARCHAR(64)      NULL,
    baselined_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uniq_baseline_approval (approval_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
"""

_RESYNC_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    id               INT           NOT NULL AUTO_INCREMENT,
    -- "success", "attention" or "ignored", the same three the run result uses.
    status           VARCHAR(16)   NOT NULL,
    exit_code        INT           NOT NULL,
    -- "cron", "dashboard", "cli": which caller this run came from. The first
    -- question asked when a day looks wrong.
    triggered_by     VARCHAR(32)       NULL,
    message          VARCHAR(1024)     NULL,
    started_at       DATETIME      NOT NULL,
    finished_at      DATETIME      NOT NULL,
    duration_seconds DECIMAL(10,3)     NULL,
    rows_written     INT               NULL,
    dry_run          TINYINT(1)    NOT NULL DEFAULT 0,
    -- The full result dict, so the dashboard can show everything the log line
    -- carries without this table having to grow a column per field.
    result           JSON              NULL,
    created_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- Serves the only query there is: the most recent run, optionally the most
    -- recent successful one.
    KEY idx_finished (finished_at),
    KEY idx_status_finished (status, finished_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
"""


def resync_runs_ddl(table: str = RESYNC_RUNS_TABLE) -> str:
    """CREATE TABLE for the resync run log -- the durable "last sync"."""
    return _RESYNC_RUNS_DDL.format(table=table)



def non_working_days_ddl(table: str = NON_WORKING_DAYS_TABLE) -> str:
    """CREATE TABLE for the shared non-working-day table."""
    return _NON_WORKING_DAYS_DDL.format(table=table)


def notification_ddls(
    notifications_table: str = APPROVAL_NOTIFICATIONS_TABLE,
    baseline_table: str = BASELINED_APPROVALS_TABLE,
) -> list:
    """
    Every CREATE TABLE the Emails job needs, parents before children.

    Child tables are named after their parent, so one argument moves the whole
    family -- which is what makes pointing at a staging table safe.

    :returns: List of (table_name, sql), in creation order.
    """
    return [
        (notifications_table, _APPROVAL_NOTIFICATIONS_DDL.format(
            table=notifications_table
        )),
        (
            f"{notifications_table}_non_working",
            _APPROVAL_NOTIFICATIONS_NON_WORKING_DDL.format(
                table=f"{notifications_table}_non_working",
                parent=notifications_table,
            ),
        ),
        (
            f"{notifications_table}_comments",
            _APPROVAL_NOTIFICATIONS_COMMENTS_DDL.format(
                table=f"{notifications_table}_comments",
                parent=notifications_table,
            ),
        ),
        (baseline_table, _BASELINED_APPROVALS_DDL.format(table=baseline_table)),
    ]


def all_ddls(
    non_working_table: str = NON_WORKING_DAYS_TABLE,
    notifications_table: str = APPROVAL_NOTIFICATIONS_TABLE,
    baseline_table: str = BASELINED_APPROVALS_TABLE,
    runs_table: str = RESYNC_RUNS_TABLE,
) -> list:
    """Every table both integrations use, in creation order."""
    return [
        (non_working_table, non_working_days_ddl(non_working_table)),
        (runs_table, resync_runs_ddl(runs_table)),
        *notification_ddls(notifications_table, baseline_table),
    ]


def is_safe_table_name(table_name: str) -> bool:
    """
    True when a name is safe to interpolate into SQL as an identifier.

    An identifier cannot be a bind parameter, so table names are interpolated.
    These come from segredo.ini, never from a request, but a typo must fail
    loudly at startup rather than become a syntax error mid-run -- and a name
    that got there some other way must not become an injection point.
    """
    return bool(table_name) and table_name.replace("_", "").isalnum()


# ---------------------------------------------------------------------------
# DatabaseConnection class
# ---------------------------------------------------------------------------


def utc_offset(zone_name: str) -> str:
    """The zone's current UTC offset as MySQL wants it, e.g. "+02:00".

    A numeric offset rather than the zone's name, because a named zone needs
    the server's time-zone tables loaded and a name MySQL does not recognise
    fails the connection outright. An offset always works.

    The offset is read once, when the pool is configured. For a zone with no
    DST -- Africa/Johannesburg -- that is the whole story. For one with DST, a
    long-lived service holds the offset it started with until it is restarted;
    cron runs are fresh processes and always compute it afresh.
    """
    offset = datetime.now(ZoneInfo(zone_name)).utcoffset() or timedelta(0)
    n_seconds = int(offset.total_seconds())
    sign = "-" if n_seconds < 0 else "+"
    n_seconds = abs(n_seconds)
    return f"{sign}{n_seconds // 3600:02d}:{(n_seconds % 3600) // 60:02d}"


class DatabaseConnection:
    """
    Manages a pool of MySQL connections and hands out cursors.

    Credentials must be passed in at construction time (read from segredo.ini).
    Nothing is hardcoded here.
    """

    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 3306,
        timeout_seconds: int = 30,
        pool_size: int = 5,
        pool_name: str = "vfz_workschedule",
        zone: str = "",
    ) -> None:
        """
        Store connection parameters. No connection is opened until one is
        actually needed -- see create_db_connection().

        :param host:            MySQL host (AWS RDS endpoint or localhost).
        :param database:        Database name.
        :param user:            MySQL username.
        :param password:        MySQL password.
        :param port:            MySQL port (default 3306).
        :param timeout_seconds: Connection timeout.
        :param pool_size:       Connections to keep. 1 for a cron run, more for
                                a service answering concurrent requests.
        :param pool_name:       Distinguishes pools within one process.
        :param zone:            IANA zone the session should use, from
                                segredo.ini. Every DATETIME the integrations
                                write is then in the same zone they reason in,
                                rather than in whatever the server happens to
                                be set to. Empty leaves the server default
                                alone, which is what the tests want.
        """
        self._config: Dict[str, Any] = {
            "host": host,
            "database": database,
            "user": user,
            "password": password,
            "port": int(port),
            "connection_timeout": int(timeout_seconds),
            # utf8mb4 throughout: Wrike display names carry accented
            # characters, and latin1 would corrupt them on the way in.
            "charset": "utf8mb4",
            "collation": "utf8mb4_general_ci",
            # Both integrations read far more than they write, and every write
            # path commits explicitly, so autocommit keeps read connections
            # from holding an idle transaction open against RDS.
            "autocommit": True,
        }
        # RDS runs in UTC. Without this, CURRENT_TIMESTAMP defaults land two
        # hours behind everything else in a SAST deployment -- the log lines,
        # the run's own startedAt, the dashboard -- and a reader has no way to
        # tell which of the two zones a given column is in. Pinning the session
        # makes every timestamp in the database mean the same thing.
        self._zone = zone or ""
        if self._zone:
            self._config["time_zone"] = utc_offset(self._zone)

        self._pool_size = max(1, int(pool_size))
        self._pool_name = pool_name
        self._pool: Optional[pooling.MySQLConnectionPool] = None

    @property
    def database(self) -> str:
        return self._config["database"]

    @property
    def host(self) -> str:
        return self._config["host"]

    @property
    def zone(self) -> str:
        """The IANA zone this connection's DATETIME columns are written in."""
        return self._zone

    def now(self) -> datetime:
        """Now, in the zone this database's DATETIME columns are written in.

        Naive, because a DATETIME column stores no offset. Every caller writing
        a timestamp goes through this rather than ``datetime.now()`` of its
        own, so a column written by Python and one defaulted by MySQL cannot
        end up in different zones.
        """
        zone = ZoneInfo(self._zone) if self._zone else timezone.utc
        return datetime.now(zone).replace(tzinfo=None)

    def _ensure_pool(self) -> pooling.MySQLConnectionPool:
        """
        Build the pool on first use.

        Deliberately lazy: constructing MySQLConnectionPool opens connections
        immediately, so an eager pool would make an unreachable database fail
        inside __init__ rather than inside the caller's try block. Every caller
        would then have to guard construction as well as use, and the one that
        forgot would report a driver error as an unhandled 500.
        """
        if self._pool is None:
            self._pool = pooling.MySQLConnectionPool(
                pool_name=self._pool_name,
                pool_size=self._pool_size,
                # Hand back a working connection after an RDS idle timeout
                # rather than a dead one from the pool.
                pool_reset_session=True,
                **self._config,
            )
        return self._pool

    @contextmanager
    def create_db_connection(self) -> Iterator[Any]:
        """
        Borrow a connection, returning it to the pool on the way out.

        :returns: Context manager yielding a live connection.
        :raises DatabaseError: if the database cannot be reached.
        """
        conn = self._ensure_pool().get_connection()
        try:
            yield conn
        finally:
            conn.close()  # returns it to the pool; it is not really closed

    @contextmanager
    def cursor(self, dictionary: bool = True, commit: bool = False) -> Iterator[Any]:
        """
        Borrow a cursor. `commit=True` commits on a clean exit only.

        A write that raises half way leaves nothing behind: the rollback runs
        before the connection goes back to the pool, so the next borrower does
        not inherit a partial transaction.

        :param dictionary: Return rows as dicts rather than tuples.
        :param commit:     Wrap the block in a transaction and commit it.
        """
        with self.create_db_connection() as conn:
            if commit:
                conn.autocommit = False
            cur = conn.cursor(dictionary=dictionary)
            try:
                yield cur
                if commit:
                    conn.commit()
            except Exception:
                if commit:
                    conn.rollback()
                raise
            finally:
                cur.close()
                if commit:
                    conn.autocommit = True

    def test_connection(self) -> bool:
        """
        Quick connectivity check -- opens, round-trips and releases.

        :returns: True if the database answered, False otherwise.
        """
        try:
            self.ping()
            return True
        except Exception:
            return False

    def ping(self) -> None:
        """Round-trip the database. Raises DatabaseError if it cannot."""
        with self.cursor(dictionary=False) as cur:
            cur.execute("SELECT 1")
            cur.fetchall()

    def create_table(self, table_name: str, sql: str) -> tuple:
        """
        Run one CREATE TABLE IF NOT EXISTS. Safe to call on every run.

        :param table_name: Name, for the message only.
        :param sql:        Finished DDL, from one of the ddl builders above.
        :returns:          (success: bool, message: str, status_code: int)
        """
        try:
            with self.cursor(dictionary=False, commit=True) as cur:
                cur.execute(sql)
            return True, f"Table '{table_name}' is ready", 200
        except Exception as exc:
            return False, f"Error ensuring table '{table_name}' exists: {exc}", 500

    def create_tables(self, ddls) -> tuple:
        """
        Create several tables in the order given. Parents before children: a
        foreign key cannot reference a table that does not exist yet.

        :param ddls: Iterable of (table_name, sql), from all_ddls() or
                     notification_ddls().
        :returns:    (success, message, status_code) -- the first failure, or
                     success for the whole set.
        """
        # Materialised once: counting a generator after iterating it would
        # report zero tables ready when it had just created five.
        l_ddls = list(ddls)
        for table_name, sql in l_ddls:
            ok, message, status_code = self.create_table(table_name, sql)
            if not ok:
                return ok, message, status_code
        return True, f"{len(l_ddls)} table(s) ready", 200

    def close(self) -> None:
        """Drop the pool. Called at shutdown, or at the end of a cron run."""
        self._pool = None
