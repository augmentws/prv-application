SELECT 'CREATE DATABASE "pvr-artifact"'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'pvr-artifact')\gexec
