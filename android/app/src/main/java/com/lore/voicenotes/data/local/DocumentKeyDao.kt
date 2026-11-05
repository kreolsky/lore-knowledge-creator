package com.lore.voicenotes.data.local

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import androidx.room.Transaction
import kotlinx.coroutines.flow.Flow

@Dao
interface DocumentKeyDao {

    @Query("SELECT * FROM document_keys WHERE deletedAt IS NULL ORDER BY position ASC")
    fun observeLive(): Flow<List<DocumentKeyEntity>>

    @Query("SELECT * FROM document_keys WHERE deletedAt IS NULL ORDER BY position ASC")
    suspend fun getLive(): List<DocumentKeyEntity>

    @Query("SELECT * FROM document_keys WHERE id = :id")
    suspend fun getById(id: Long): DocumentKeyEntity?

    @Query("SELECT * FROM document_keys WHERE tokenHash = :tokenHash LIMIT 1")
    suspend fun getByTokenHash(tokenHash: String): DocumentKeyEntity?

    @Insert
    suspend fun insert(key: DocumentKeyEntity): Long

    @Query("UPDATE document_keys SET documentTitle = :documentTitle, projectName = :projectName WHERE id = :id")
    suspend fun updateTitles(id: Long, documentTitle: String?, projectName: String?)

    /** Soft-delete: drop the token and the slot, keep hash and titles for a restore. */
    @Query("UPDATE document_keys SET deletedAt = :now, encodedKey = NULL, position = NULL WHERE id = :id")
    suspend fun softDelete(id: Long, now: Long)

    /** Restore a soft-deleted row when the same token is re-added. */
    @Query("UPDATE document_keys SET encodedKey = :encodedKey, position = :position, deletedAt = NULL WHERE id = :id")
    suspend fun restore(id: Long, encodedKey: String, position: Int)

    @Query("UPDATE document_keys SET position = :position WHERE id = :id")
    suspend fun updatePosition(id: Long, position: Int)

    /** Rewrite live positions 1..N in the given id order. */
    @Transaction
    suspend fun renumber(order: List<Long>) {
        order.forEachIndexed { index, id -> updatePosition(id, index + 1) }
    }
}
