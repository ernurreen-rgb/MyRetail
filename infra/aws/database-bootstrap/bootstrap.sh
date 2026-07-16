#!/bin/sh
set -eu

require_value() {
  if [ -z "$1" ]; then
    echo "Required database bootstrap input is missing: $2" >&2
    exit 2
  fi
}

require_value "${PGHOST:-}" "PGHOST"
require_value "${PGPORT:-}" "PGPORT"
require_value "${PGDATABASE:-}" "PGDATABASE"
require_value "${PGUSER:-}" "PGUSER"
require_value "${PGPASSWORD:-}" "PGPASSWORD"
require_value "${PGSSLROOTCERT:-}" "PGSSLROOTCERT"
require_value "${MYRETAIL_STATE_APP_PASSWORD:-}" "MYRETAIL_STATE_APP_PASSWORD"
require_value "${MYRETAIL_STATE_MIGRATION_PASSWORD:-}" "MYRETAIL_STATE_MIGRATION_PASSWORD"

if [ "${PGSSLMODE:-}" != "verify-full" ]; then
  echo "Database bootstrap requires PGSSLMODE=verify-full" >&2
  exit 2
fi

exec psql --no-password --file=/usr/local/share/myretail/bootstrap.sql
