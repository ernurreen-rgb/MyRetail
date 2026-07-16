\set ON_ERROR_STOP on

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE myretail_state_owner
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE myretail_state_migrator
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE myretail_api
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

GRANT myretail_state_owner TO myretail_state_migrator;

CREATE DATABASE myretail_state_test;
REVOKE CONNECT ON DATABASE myretail_state_test FROM PUBLIC;
GRANT CONNECT ON DATABASE myretail_state_test
    TO myretail_state_migrator, myretail_api;
GRANT CREATE ON DATABASE myretail_state_test TO myretail_state_owner;

CREATE DATABASE myretail_state_unmigrated_test;
REVOKE CONNECT ON DATABASE myretail_state_unmigrated_test FROM PUBLIC;
GRANT CONNECT ON DATABASE myretail_state_unmigrated_test
    TO myretail_state_migrator, myretail_api;
GRANT CREATE ON DATABASE myretail_state_unmigrated_test TO myretail_state_owner;

\connect myretail_state_test
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CREATE ON SCHEMA public TO myretail_state_owner;

\connect myretail_state_unmigrated_test
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CREATE ON SCHEMA public TO myretail_state_owner;
