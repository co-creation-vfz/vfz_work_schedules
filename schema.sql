-- VFZ Work Schedule integrations -- MySQL schema.
--
-- Generated from shared/database_helpers.TABLE_REGISTRY, which is what the
-- services actually run at startup (idempotent, so they create these
-- themselves on a fresh environment). This file is for a DBA who would rather
-- create them ahead of time, or review them first.
--
--   mysql -h vfz-wrike-integration.cj6hvdyvczrr.eu-north-1.rds.amazonaws.com \
--         -u admin -p vfz-wrike-workschedule-v1 < schema.sql
--
-- Order matters: parents before children, because a foreign key cannot
-- reference a table that does not exist yet.
--
-- | Table                              | Written by | Read by |
-- |------------------------------------|------------|---------|
-- | work_schedule_non_working_days     | DB Resync  | both    |
-- | work_schedule_resync_runs          | DB Resync  | DB Resync |
-- | approval_notifications             | Emails     | Emails  |
-- | approval_notifications_non_working | Emails     | Emails  |
-- | approval_notifications_comments    | Emails     | Emails  |
-- | baselined_approvals                | Emails     | Emails  |

CREATE TABLE IF NOT EXISTS `work_schedule_non_working_days` (
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

CREATE TABLE IF NOT EXISTS `work_schedule_resync_runs` (
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

CREATE TABLE IF NOT EXISTS `approval_notifications` (
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

CREATE TABLE IF NOT EXISTS `approval_notifications_non_working` (
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
    CONSTRAINT `fk_approval_notifications_non_working_notification`
        FOREIGN KEY (notification_id)
        REFERENCES `approval_notifications` (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `approval_notifications_comments` (
    id              INT          NOT NULL AUTO_INCREMENT,
    notification_id INT          NOT NULL,
    comment_id      VARCHAR(64)  NOT NULL,
    posted_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- Deliberately NOT unique: a corrected re-notification adds a second
    -- comment to the same record, and a retraction must delete both.
    KEY idx_notification (notification_id),
    CONSTRAINT `fk_approval_notifications_comments_notification`
        FOREIGN KEY (notification_id)
        REFERENCES `approval_notifications` (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `baselined_approvals` (
    id           INT          NOT NULL AUTO_INCREMENT,
    approval_id  VARCHAR(64)  NOT NULL,
    task_id      VARCHAR(64)      NULL,
    baselined_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uniq_baseline_approval (approval_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
