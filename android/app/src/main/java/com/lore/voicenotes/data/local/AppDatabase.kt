package com.lore.voicenotes.data.local

import androidx.room.Database
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(entities = [TranscriptionEntity::class, DocumentKeyEntity::class], version = 3)
abstract class AppDatabase : RoomDatabase() {
    abstract fun transcriptionDao(): TranscriptionDao
    abstract fun documentKeyDao(): DocumentKeyDao

    companion object {
        // v2: persist the on-device audio path + mime so recordings survive upload
        // failures and can be re-sent from history.
        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE transcriptions ADD COLUMN localFilePath TEXT")
                db.execSQL("ALTER TABLE transcriptions ADD COLUMN mimeType TEXT")
            }
        }

        // v3: multi-key — document_keys table, per-row key/duration/soft-delete/upload
        // error columns. Rows stored as "applied" (the pre-fix upload answer) become
        // "queued" so they re-enter the poll path and their text arrives.
        val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `document_keys` (" +
                        "`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, " +
                        "`position` INTEGER, " +
                        "`encodedKey` TEXT, " +
                        "`tokenHash` TEXT NOT NULL, " +
                        "`documentTitle` TEXT, " +
                        "`projectName` TEXT, " +
                        "`deletedAt` INTEGER)"
                )
                db.execSQL("ALTER TABLE `transcriptions` ADD COLUMN `keyId` INTEGER")
                db.execSQL("ALTER TABLE `transcriptions` ADD COLUMN `durationMs` INTEGER")
                db.execSQL("ALTER TABLE `transcriptions` ADD COLUMN `deletedAt` INTEGER")
                db.execSQL("ALTER TABLE `transcriptions` ADD COLUMN `uploadErrorCode` INTEGER")
                db.execSQL("UPDATE `transcriptions` SET `status` = 'queued' WHERE `status` = 'applied'")
            }
        }
    }
}
