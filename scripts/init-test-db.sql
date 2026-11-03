-- Disposable database for the integration tests (they drop and recreate the pipeline schemas).
CREATE DATABASE orders_test;
ALTER AUTHORIZATION ON DATABASE::orders_test TO pipeline;
