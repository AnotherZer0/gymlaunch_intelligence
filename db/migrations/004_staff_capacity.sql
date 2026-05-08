-- Staff capacity: max active clients per media buyer and account manager.
-- Update capacity values here when team composition changes.
-- Depends on: 003_asana_schema.sql

BEGIN;

CREATE TABLE IF NOT EXISTS agency_staff_capacity (
    name      TEXT NOT NULL,
    role      TEXT NOT NULL,  -- 'media_buyer' | 'account_manager'
    capacity  INT  NOT NULL,
    PRIMARY KEY (name, role)
);

INSERT INTO agency_staff_capacity (name, role, capacity) VALUES
    ('Devi',     'media_buyer',     20),
    ('James',    'media_buyer',     35),
    ('Amrit',    'media_buyer',     20),
    ('Muhammad', 'media_buyer',     40),
    ('Malik',    'media_buyer',     40),
    ('Wynfred',  'media_buyer',     40),
    ('Ali',      'media_buyer',     40),
    ('Diwa',     'media_buyer',     40),
    ('Rin',      'media_buyer',     40),
    ('Rajiv',    'media_buyer',      5),
    ('Muhammad', 'account_manager', 40),
    ('Malik',    'account_manager', 40),
    ('Wynfred',  'account_manager', 40),
    ('Ali',      'account_manager', 40),
    ('Diwa',     'account_manager', 40),
    ('Rin',      'account_manager', 40),
    ('James',    'account_manager', 35),
    ('Devi',     'account_manager', 20),
    ('Amrit',    'account_manager', 20),
    ('Rajiv',    'account_manager',  5)
ON CONFLICT (name, role) DO UPDATE SET capacity = EXCLUDED.capacity;


CREATE OR REPLACE VIEW staff_availability AS
SELECT
    sc.name,
    sc.role,
    sc.capacity,
    COALESCE(active.cnt, 0)               AS active_count,
    sc.capacity - COALESCE(active.cnt, 0) AS free_slots
FROM agency_staff_capacity sc
LEFT JOIN (
    SELECT ab.media_buyer AS name, 'media_buyer' AS role, COUNT(*) AS cnt
    FROM asana_agency_board_task ab
    JOIN asana_task t ON t.gid = ab.task_gid
    WHERE t.parent_task_gid IS NULL
      AND ab.media_buyer IS NOT NULL
      AND t.section_name IN (
          'Onboarding',
          'Ready To Go Live',
          'Downed Account',
          'Active Accounts'
      )
    GROUP BY ab.media_buyer
) active ON active.name = sc.name AND active.role = sc.role
ORDER BY sc.role, free_slots DESC;

COMMIT;
