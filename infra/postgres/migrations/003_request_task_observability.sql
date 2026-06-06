-- Request and Celery task observability hardening.
-- Safe to run repeatedly. Existing init SQL already defines these audit columns;
-- this migration enforces them for environments created from older schemas.

DO $$
DECLARE
    table_record RECORD;
BEGIN
    FOR table_record IN
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = 'job_miner_control'
          AND table_type = 'BASE TABLE'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS created_on TIMESTAMPTZ NOT NULL DEFAULT NOW()',
            table_record.table_schema,
            table_record.table_name
        );
        EXECUTE format(
            'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS created_by UUID',
            table_record.table_schema,
            table_record.table_name
        );
        EXECUTE format(
            'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW()',
            table_record.table_schema,
            table_record.table_name
        );
        EXECUTE format(
            'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS modified_by UUID',
            table_record.table_schema,
            table_record.table_name
        );
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION job_miner_control.set_modified_on()
RETURNS TRIGGER AS $$
BEGIN
    NEW.modified_on = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    table_record RECORD;
    trigger_name TEXT;
BEGIN
    FOR table_record IN
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = 'job_miner_control'
          AND table_type = 'BASE TABLE'
    LOOP
        trigger_name := 'trg_' || table_record.table_name || '_set_modified_on';
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', trigger_name, table_record.table_schema, table_record.table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE ON %I.%I FOR EACH ROW EXECUTE FUNCTION job_miner_control.set_modified_on()',
            trigger_name,
            table_record.table_schema,
            table_record.table_name
        );
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_api_request_logs_method_status_created
ON job_miner_control.api_request_logs (method, status_code, created_on DESC);

CREATE INDEX IF NOT EXISTS idx_celery_tasks_task_uuid_status
ON job_miner_control.celery_tasks (task_uuid, status);

CREATE INDEX IF NOT EXISTS idx_celery_tasks_queue_status_created
ON job_miner_control.celery_tasks (queue_name, status, created_on DESC);

CREATE INDEX IF NOT EXISTS idx_celery_task_events_task_created
ON job_miner_control.celery_task_events (celery_task_id, created_on DESC);

CREATE INDEX IF NOT EXISTS idx_processing_failures_entity_created
ON job_miner_control.processing_failures (entity_type, entity_id, created_on DESC);
