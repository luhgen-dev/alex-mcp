"""Upgrade regressions: fresh schema tests alone miss installed CHECK clauses."""
import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_core as core
from media_schema import ensure_original_media_types


class LegacyMediaUpgradeTests(unittest.TestCase):
    def legacy(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
          PRAGMA foreign_keys=ON;
          CREATE TABLE inbound_messages(message_id TEXT PRIMARY KEY);
          CREATE TABLE media_objects(media_id TEXT PRIMARY KEY,source_message_id TEXT,
            media_type TEXT CHECK(media_type IN ('IMAGE','AUDIO','PDF','DOCUMENT')),
            local_path TEXT,sha256 TEXT,transcript_meta_json TEXT,
            FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id));
          CREATE TABLE outbound_messages(outbound_id TEXT PRIMARY KEY,
            kind TEXT CHECK(kind IN ('TEXT','IMAGE','DOCUMENT')),local_path TEXT,
            context_id TEXT,provider_message_id TEXT);
          CREATE TABLE saved_items(item_id TEXT PRIMARY KEY,media_id TEXT REFERENCES media_objects(media_id));
          CREATE INDEX idx_media_source_fixture ON media_objects(source_message_id);
          CREATE TABLE migration_trigger_log(media_id TEXT);
          CREATE TRIGGER archive_insert_fixture AFTER INSERT ON media_objects
            BEGIN INSERT INTO migration_trigger_log VALUES(new.media_id); END;
          CREATE VIEW media_fixture_view AS SELECT media_id,local_path FROM media_objects;
          INSERT INTO inbound_messages VALUES('original-message');
          INSERT INTO media_objects VALUES('original-audio','original-message','AUDIO','/unchanged/original.ogg','original-hash','metadata');
          INSERT INTO saved_items VALUES('saved-original','original-audio');
          INSERT INTO outbound_messages VALUES('queued-document','DOCUMENT','/unchanged/file.pdf','canonical','provider-original');
        """)
        return conn

    def test_upgrade_retains_rows_links_metadata_indexes_triggers_and_repeats(self):
        conn = self.legacy()
        try:
            before = dict(conn.execute("SELECT * FROM media_objects").fetchone())
            self.assertEqual(ensure_original_media_types(conn), ["media_objects", "outbound_messages"])
            self.assertEqual(dict(conn.execute("SELECT * FROM media_objects WHERE media_id='original-audio'").fetchone()), before)
            self.assertEqual(conn.execute("SELECT media_id FROM saved_items").fetchone()[0], "original-audio")
            self.assertEqual(conn.execute("SELECT local_path FROM media_fixture_view").fetchone()[0], "/unchanged/original.ogg")
            self.assertEqual(conn.execute("SELECT provider_message_id FROM outbound_messages").fetchone()[0], "provider-original")
            self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_media_source_fixture'").fetchone())
            conn.execute("INSERT INTO media_objects(media_id,media_type) VALUES('video-new','VIDEO')")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM migration_trigger_log WHERE media_id='video-new'").fetchone()[0], 1)
            for kind in ("AUDIO", "VIDEO"):
                conn.execute("INSERT INTO outbound_messages(outbound_id,kind) VALUES(?,?)", (kind, kind))
            conn.commit()
            self.assertEqual(ensure_original_media_types(conn), [])
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_failed_integrity_upgrade_rolls_back_every_table_and_restores_fk(self):
        conn = self.legacy()
        try:
            before = conn.execute("SELECT sql FROM sqlite_master WHERE name='media_objects'").fetchone()[0]
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO saved_items VALUES('bad-fixture','missing-original')")
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
            with self.assertRaisesRegex(RuntimeError, "foreign-key"):
                ensure_original_media_types(conn)
            self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE name='media_objects'").fetchone()[0], before)
            self.assertEqual(conn.execute("SELECT local_path FROM media_objects").fetchone()[0], "/unchanged/original.ogg")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '_alex_media_upgrade_%'").fetchone())
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO outbound_messages(outbound_id,kind) VALUES('still-old','VIDEO')")
        finally:
            conn.close()

    def test_actual_initialize_upgrades_old_database_before_audio_video_use(self):
        with tempfile.TemporaryDirectory(prefix="alex-media-upgrade-") as root:
            path = str(Path(root) / "legacy.db")
            legacy_schema = Path(core.db.SCHEMA_PATH).read_text().replace(
                "media_type IN ('IMAGE','AUDIO','VIDEO','PDF','DOCUMENT')",
                "media_type IN ('IMAGE','AUDIO','PDF','DOCUMENT')",
            ).replace("kind IN ('TEXT','IMAGE','AUDIO','VIDEO','DOCUMENT')", "kind IN ('TEXT','IMAGE','DOCUMENT')")
            with sqlite3.connect(path) as conn:
                conn.executescript(legacy_schema)
                conn.execute("INSERT INTO inbound_messages(message_id,conversation_id,sender_phone,processing_state) VALUES('legacy-source','synthetic-chat','+60111111111','COMPLETED')")
                conn.execute("INSERT INTO media_objects(media_id,source_message_id,media_type,mime_type,sha256,local_path) VALUES('legacy-original','legacy-source','AUDIO','audio/ogg','unchanged-hash','/unchanged/legacy.ogg')")
            with patch.object(core.db, "DB_PATH", path), patch.object(core.media, "MEDIA_DIR", str(Path(root) / "media")):
                core.db.initialize()
                media_id = core.media.save_media("legacy-source", "VIDEO", "video/mp4", base64.b64encode(b"synthetic video").decode())
                original = core.media.get_media(media_id)
                self.assertEqual(Path(original["local_path"]).read_bytes(), b"synthetic video")
                oid = core.db.queue_outbound("synthetic-chat", "VIDEO", local_path=original["local_path"], mime_type="video/mp4", source_message_id="legacy-source")
                self.assertTrue(oid)
                core.db.initialize()
                with core.db.connect() as conn:
                    self.assertEqual(conn.execute("SELECT local_path FROM media_objects WHERE media_id='legacy-original'").fetchone()[0], "/unchanged/legacy.ogg")
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
