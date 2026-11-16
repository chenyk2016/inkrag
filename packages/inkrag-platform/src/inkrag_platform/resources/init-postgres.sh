#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set=app_password="$PG_APP_PASSWORD" --set=migration_password="$PG_MIGRATION_PASSWORD" \
  --set=keycloak_password="$KEYCLOAK_DB_PASSWORD" <<'SQL'
CREATE ROLE inkrag_app LOGIN PASSWORD :'app_password';
CREATE ROLE inkrag_migrator LOGIN PASSWORD :'migration_password';
CREATE ROLE keycloak LOGIN PASSWORD :'keycloak_password';
CREATE DATABASE inkrag OWNER inkrag_migrator;
CREATE DATABASE keycloak OWNER keycloak;
REVOKE ALL ON DATABASE inkrag FROM PUBLIC;
GRANT CONNECT ON DATABASE inkrag TO inkrag_app;
SQL
