package com.lore.voicenotes.data.local

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface TranscriptionDao {

    @Query("SELECT * FROM transcriptions WHERE deletedAt IS NULL ORDER BY createdAt DESC")
    fun getAll(): Flow<List<TranscriptionEntity>>

    @Query("SELECT * FROM transcriptions WHERE id = :id")
    suspend fun getById(id: Long): TranscriptionEntity?

    /** Reactive single row (the detail screen); soft-deleted rows still emit. */
    @Query("SELECT * FROM transcriptions WHERE id = :id")
    fun observeById(id: Long): Flow<TranscriptionEntity?>

    /** History delete: hide the row, keep file and content for the undo window. */
    @Query("UPDATE transcriptions SET deletedAt = :now WHERE id = :id AND deletedAt IS NULL")
    suspend fun softDelete(id: Long, now: Long)

    /** Undo: the row returns with everything it had before the swipe. */
    @Query("UPDATE transcriptions SET deletedAt = NULL WHERE id = :id")
    suspend fun clearDeletedAt(id: Long)

    /**
     * Pollable rows: queued/processing, not soft-deleted, and owned by a LIVE key.
     * Rows of a deleted key keep their colour but are never polled again.
     */
    @Query(
        """
        SELECT t.* FROM transcriptions t
        JOIN document_keys k ON k.id = t.keyId
        WHERE t.status IN ('queued', 'processing')
          AND t.deletedAt IS NULL
          AND k.deletedAt IS NULL
        """
    )
    suspend fun getPending(): List<TranscriptionEntity>

    @Query("SELECT * FROM transcriptions WHERE deletedAt IS NOT NULL")
    suspend fun getSoftDeleted(): List<TranscriptionEntity>

    /** Zero-key binding: rows recorded with no key adopt the first added key. */
    @Query("UPDATE transcriptions SET keyId = :keyId WHERE keyId IS NULL AND deletedAt IS NULL")
    suspend fun bindKeylessRows(keyId: Long)

    /** Deleting a key turns its unsent rows red; same transaction as the soft delete. */
    @Query(
        "UPDATE transcriptions SET status = 'upload_failed' " +
            "WHERE keyId = :keyId AND status = 'pending_upload' AND deletedAt IS NULL"
    )
    suspend fun markPendingUploadFailed(keyId: Long)

    /** Re-send: unsent again, any permanent code cleared; content is kept. */
    @Query("UPDATE transcriptions SET status = 'pending_upload', uploadErrorCode = NULL WHERE id = :id")
    suspend fun markPendingUpload(id: Long)

    /** Red row; a non-null code is permanent — never auto-enqueued again. */
    @Query("UPDATE transcriptions SET status = 'upload_failed', uploadErrorCode = :code WHERE id = :id")
    suspend fun markUploadFailed(id: Long, code: Int?)

    /** Unsent rows owned by a live key — startup and first-key enqueue. */
    @Query(
        """
        SELECT t.id FROM transcriptions t
        JOIN document_keys k ON k.id = t.keyId
        WHERE t.status = 'pending_upload' AND t.deletedAt IS NULL AND k.deletedAt IS NULL
        """
    )
    suspend fun getUploadableIds(): List<Long>

    /** Red rows with a live key and no permanent code — the network-return kick. */
    @Query(
        """
        SELECT t.id FROM transcriptions t
        JOIN document_keys k ON k.id = t.keyId
        WHERE t.status = 'upload_failed' AND t.deletedAt IS NULL
          AND t.uploadErrorCode IS NULL AND k.deletedAt IS NULL
        """
    )
    suspend fun getRetryableUploadFailedIds(): List<Long>

    /** Red rows not soft-deleted — the History tab count and the record-screen fail line. */
    @Query("SELECT COUNT(*) FROM transcriptions WHERE status = 'upload_failed' AND deletedAt IS NULL")
    fun observeUploadFailedCount(): Flow<Int>

    /** Any unsent row with a live key — the accent dot on the History tab. */
    @Query(
        """
        SELECT EXISTS(
            SELECT 1 FROM transcriptions t
            JOIN document_keys k ON k.id = t.keyId
            WHERE t.status = 'pending_upload' AND t.deletedAt IS NULL AND k.deletedAt IS NULL
        )
        """
    )
    fun observeHasPendingUpload(): Flow<Boolean>

    @Insert
    suspend fun insert(entity: TranscriptionEntity): Long

    @Query("UPDATE transcriptions SET status = :status, content = :content WHERE id = :id")
    suspend fun updateStatus(id: Long, status: String, content: String?)

    /** After a (re-)upload succeeds: rebind to the new server reference and reset status. */
    @Query(
        "UPDATE transcriptions SET referenceId = :referenceId, status = :status, " +
            "content = NULL, uploadErrorCode = NULL WHERE id = :id"
    )
    suspend fun updateReference(id: Long, referenceId: String, status: String)

    @Query("DELETE FROM transcriptions WHERE id = :id")
    suspend fun delete(id: Long)
}
