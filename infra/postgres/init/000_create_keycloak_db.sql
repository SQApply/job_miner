-- Separate DB for Keycloak internal tables. The Job Miner app must never query Keycloak internals directly.
SELECT 'CREATE DATABASE keycloak WITH OWNER job_miner_app'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec
