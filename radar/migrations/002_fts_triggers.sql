-- Fix FTS5 triggers for contentful FTS tables.
--
-- SQLite 3.53 rejects the FTS5 'delete' command against a normal (contentful)
-- FTS5 table; SQL DELETE works for all supported SQLite versions.  Recreate the
-- triggers idempotently for databases created by migration 001.

DROP TRIGGER IF EXISTS opportunities_ad;
DROP TRIGGER IF EXISTS opportunities_au;

CREATE TRIGGER opportunities_ad AFTER DELETE ON opportunities BEGIN
  DELETE FROM opportunities_fts WHERE rowid = old.id;
END;

CREATE TRIGGER opportunities_au AFTER UPDATE ON opportunities BEGIN
  DELETE FROM opportunities_fts WHERE rowid = old.id;
  INSERT INTO opportunities_fts(rowid, title, description, city, contact_value, role_type)
  VALUES (new.id, new.title, COALESCE(new.description, ''), COALESCE(new.city, ''),
          COALESCE(new.contact_value, ''), COALESCE(new.role_type_json, ''));
END;
