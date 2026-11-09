#!/bin/sh
(
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<'SQL'
getenv app_password APP_DB_PASSWORD
SELECT format('CREATE ROLE finance_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'app_password') gexec
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO finance_app;
SELECT format('GRANT CONNECT ON DATABASE %I TO finance_app', current_database()) gexec
SQL
)
