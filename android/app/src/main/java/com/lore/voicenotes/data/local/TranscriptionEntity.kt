package com.lore.voicenotes.data.local

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "transcriptions")
data class TranscriptionEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val referenceId: String,
    val title: String,
    /** pending_upload | queued | processing | ready | failed | upload_failed */
    val status: String,
    val content: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    // INVARIANT: localFilePath points at the on-device M4A and is the source for re-send.
    // Why: audio must survive an upload failure so the user can re-send from history;
    // the file is deleted only when its row is deleted.
    val localFilePath: String? = null,
    val mimeType: String? = null,
    /** The key the recording was made under; NULL rows wait for the first added key. */
    val keyId: Long? = null,
    val durationMs: Long? = null,
    /** Soft delete: hides the row until its undo window passes, then it is purged. */
    val deletedAt: Long? = null,
    /** Last permanent upload 4xx; such a row is never auto-enqueued again. */
    val uploadErrorCode: Int? = null,
) {
    companion object {
        const val STATUS_PENDING_UPLOAD = "pending_upload"
        const val STATUS_QUEUED = "queued"
        const val STATUS_PROCESSING = "processing"
        const val STATUS_READY = "ready"
        const val STATUS_FAILED = "failed"
        const val STATUS_UPLOAD_FAILED = "upload_failed"
    }
}
