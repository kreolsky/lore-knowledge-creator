package com.lore.voicenotes

import androidx.room.testing.MigrationTestHelper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.lore.voicenotes.data.local.AppDatabase
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AppDatabaseMigrationTest {

    @get:Rule
    val helper = MigrationTestHelper(
        InstrumentationRegistry.getInstrumentation(),
        AppDatabase::class.java,
    )

    // TEST-WHY: data-loss — v2→v3 restructures the user's whole history table
    // (keyId/durationMs/deletedAt/uploadErrorCode plus the document_keys table)
    // and un-sticks rows stored as "applied": a hand-written DDL that drifts from
    // the entities makes Room refuse to open the database, and a missed UPDATE
    // strands every pre-fix recording off the poll path forever.
    @Test
    fun migrate2To3_preservesRowsAndUnsticksApplied() {
        val db = helper.createDatabase(TEST_DB, 2)
        db.execSQL(
            "INSERT INTO transcriptions (referenceId, title, status, content, createdAt, localFilePath, mimeType) " +
                "VALUES ('ref-1', 'a.m4a', 'applied', NULL, 111, '/recordings/a.m4a', 'audio/mp4')"
        )
        db.execSQL(
            "INSERT INTO transcriptions (referenceId, title, status, content, createdAt, localFilePath, mimeType) " +
                "VALUES ('ref-2', 'b.m4a', 'ready', 'расшифровка', 222, NULL, NULL)"
        )
        db.close()

        // Also validates the migrated schema against the exported 3.json
        val v3 = helper.runMigrationsAndValidate(TEST_DB, 3, true, AppDatabase.MIGRATION_2_3)

        v3.query(
            "SELECT referenceId, status, content, localFilePath, mimeType, " +
                "keyId, durationMs, deletedAt, uploadErrorCode FROM transcriptions ORDER BY createdAt"
        ).use { cursor ->
            assertEquals(2, cursor.count)

            assertTrue(cursor.moveToFirst())
            assertEquals("ref-1", cursor.getString(0))
            assertEquals("queued", cursor.getString(1)) // un-stuck off "applied"
            assertNull(cursor.getString(2))
            assertEquals("/recordings/a.m4a", cursor.getString(3))
            assertEquals("audio/mp4", cursor.getString(4))
            assertNull(cursor.getString(5)) // keyId — no keys exist yet
            assertNull(cursor.getString(6)) // durationMs
            assertNull(cursor.getString(7)) // deletedAt
            assertNull(cursor.getString(8)) // uploadErrorCode

            assertTrue(cursor.moveToNext())
            assertEquals("ref-2", cursor.getString(0))
            assertEquals("ready", cursor.getString(1)) // non-applied statuses untouched
            assertEquals("расшифровка", cursor.getString(2))
        }

        v3.query("SELECT COUNT(*) FROM document_keys").use { cursor ->
            assertTrue(cursor.moveToFirst())
            assertEquals(0, cursor.getInt(0))
        }

        // The keys table accepts its full declared shape
        v3.execSQL(
            "INSERT INTO document_keys (position, encodedKey, tokenHash, documentTitle, projectName, deletedAt) " +
                "VALUES (1, 'enc', 'hash', 'Документ', 'Проект', NULL)"
        )
        v3.query("SELECT id, position, encodedKey, tokenHash, documentTitle, projectName, deletedAt FROM document_keys").use { cursor ->
            assertTrue(cursor.moveToFirst())
            assertEquals(1L, cursor.getLong(0))
            assertEquals(1, cursor.getInt(1))
            assertEquals("enc", cursor.getString(2))
            assertEquals("hash", cursor.getString(3))
            assertEquals("Документ", cursor.getString(4))
            assertEquals("Проект", cursor.getString(5))
            assertNull(cursor.getString(6))
            assertFalse(cursor.moveToNext())
        }
        v3.close()
    }

    companion object {
        private const val TEST_DB = "migration-test.db"
    }
}
