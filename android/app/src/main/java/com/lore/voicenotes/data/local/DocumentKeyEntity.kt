package com.lore.voicenotes.data.local

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * A widget key bound to one Lore document. One side of the seam: every
 * request carries the row's [DecodedConfig] through a Retrofit @Tag.
 *
 * Deleting is soft: [deletedAt] is set and the token ([encodedKey]) and
 * [position] are cleared so the raw token does not stay on the phone, while
 * [tokenHash] and the titles survive for the re-add-same-token restore.
 */
@Entity(tableName = "document_keys")
data class DocumentKeyEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    /** 1..N over live keys, renumbered on delete and reorder; NULL on deleted rows. */
    val position: Int? = null,
    /** base64(json({url, token})); NULL once soft-deleted. */
    val encodedKey: String? = null,
    val tokenHash: String,
    val documentTitle: String? = null,
    val projectName: String? = null,
    /** NULL = live key. */
    val deletedAt: Long? = null,
)
