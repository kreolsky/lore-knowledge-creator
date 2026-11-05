package com.lore.voicenotes.data.repository

import android.content.Context
import com.lore.voicenotes.data.local.DocumentKeyDao
import com.lore.voicenotes.data.local.DocumentKeyEntity
import com.lore.voicenotes.data.local.SettingsDataStore
import com.lore.voicenotes.data.local.TranscriptionDao
import com.lore.voicenotes.data.local.TranscriptionEntity
import com.lore.voicenotes.data.remote.LoreApiService
import com.lore.voicenotes.worker.PollTranscriptionWorker
import com.lore.voicenotes.worker.UploadKickWorker
import com.lore.voicenotes.worker.UploadWorker
import androidx.work.WorkManager
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import retrofit2.HttpException
import java.io.File
import java.io.IOException
import javax.inject.Inject
import javax.inject.Singleton

/** Result of one UploadWorker run for a row. */
sealed interface UploadOutcome {
    /** Uploaded: the row is rebound to the server reference, status queued. */
    data object Success : UploadOutcome
    /** Nothing to do: row gone, soft-deleted, keyId NULL (zero keys), or a stale run. */
    data object Skipped : UploadOutcome
    /** Key deleted while the row was unsent: marked upload_failed, exit success. */
    data object KeyDeleted : UploadOutcome
    /** IOException, 408, 429 or 5xx — WorkManager retries with backoff. */
    data object Retryable : UploadOutcome
    /** Any other 4xx: the code is stored and the row is never auto-enqueued again. */
    data class Permanent(val httpCode: Int?) : UploadOutcome
    /** Local audio missing or empty: nothing to send, ever. */
    data object NoFile : UploadOutcome
}

@Singleton
class TranscriptionRepository @Inject constructor(
    @ApplicationContext private val context: Context,
    private val dao: TranscriptionDao,
    private val keyDao: DocumentKeyDao,
    private val api: LoreApiService,
) {
    val all: Flow<List<TranscriptionEntity>> = dao.getAll()

    /** Red rows not soft-deleted — the History tab count and the record-screen fail line. */
    val uploadFailedCount: Flow<Int> = dao.observeUploadFailedCount()

    /** Any unsent row with a live key — the accent dot on the History tab. */
    val hasPendingUpload: Flow<Boolean> = dao.observeHasPendingUpload()

    suspend fun getById(id: Long): TranscriptionEntity? = dao.getById(id)

    /** Reactive single row for the detail screen. */
    fun observeById(id: Long): Flow<TranscriptionEntity?> = dao.observeById(id)

    /** Live config of a key row, or null for deleted/undecodable keys. */
    private fun decode(key: DocumentKeyEntity) =
        key.encodedKey?.let { SettingsDataStore.decodeApiKey(it) }

    /**
     * Persist a finished recording and enqueue its upload (KEEP).
     *
     * INVARIANT: the row is inserted with status pending_upload BEFORE any
     * upload runs. Why: an upload failure must leave the audio re-sendable
     * from history rather than lost. A null keyId (zero keys on the device)
     * leaves the row pending_upload with keyId NULL — nothing is enqueued and
     * the first added key adopts it. A keyId whose key is gone keeps the keyId
     * (rows of a deleted key are never rebound) and writes upload_failed:
     * a recording can finish after its key was deleted.
     */
    suspend fun save(file: File, title: String, mimeType: String, keyId: Long?, durationMs: Long?): Long {
        val keyLive = keyId != null && keyDao.getById(keyId)?.deletedAt == null
        val initialStatus = if (keyId != null && !keyLive) {
            TranscriptionEntity.STATUS_UPLOAD_FAILED
        } else {
            TranscriptionEntity.STATUS_PENDING_UPLOAD
        }
        val id = dao.insert(
            TranscriptionEntity(
                referenceId = "",
                title = title,
                status = initialStatus,
                localFilePath = file.absolutePath,
                mimeType = mimeType,
                keyId = keyId,
                durationMs = durationMs,
            )
        )
        if (keyLive) UploadWorker.enqueueKeep(context, id)
        return id
    }

    /**
     * Re-upload an existing entry's local audio (allowed for any status, incl. ready).
     * Uses the row's own key; throws if the local file is missing or the key is gone.
     * The row goes pending_upload, keeps its content and clears any permanent
     * code until the new upload succeeds.
     */
    suspend fun resend(id: Long) {
        val entry = dao.getById(id) ?: return
        val file = entry.localFilePath?.let(::File)?.takeIf { it.exists() && it.length() > 0L }
            ?: throw IllegalStateException("No local audio")
        val key = entry.keyId?.let { keyDao.getById(it) }
        if (key == null || key.deletedAt != null) throw IllegalStateException("Key deleted")
        dao.markPendingUpload(id)
        UploadWorker.enqueueReplace(context, id)
    }

    /**
     * One upload attempt for [id], called by UploadWorker with its runAttemptCount.
     *
     * upload_failed timing: a failure writes it from attempt 1 on (attempt 0
     * stays amber through the first 30 s backoff), and every run from attempt
     * 2 on writes it at start — a worker killed mid-upload never reaches its
     * failure branch. A success clears it. An IOException that writes it also
     * enqueues UploadKickWorker for the network return.
     *
     * The response's status VALUE is not read: for audio the server answers
     * "applied" while the reference itself is only queued for transcription
     * (same contract the desktop widget client relies on), so any 2xx stores
     * queued and the row stays pollable.
     */
    suspend fun uploadRow(id: Long, attempt: Int): UploadOutcome {
        val row = dao.getById(id) ?: return UploadOutcome.Skipped
        if (row.deletedAt != null) return UploadOutcome.Skipped
        if (row.keyId == null) return UploadOutcome.Skipped
        // Stale run: a newer REPLACE-enqueued run owns this row, or it already
        // moved on (queued/ready via another path). Re-uploading would mint a
        // duplicate server reference.
        if (row.status != TranscriptionEntity.STATUS_PENDING_UPLOAD &&
            row.status != TranscriptionEntity.STATUS_UPLOAD_FAILED
        ) return UploadOutcome.Skipped
        val key = keyDao.getById(row.keyId)
        if (key == null || key.deletedAt != null) {
            dao.markUploadFailed(id, null)
            return UploadOutcome.KeyDeleted
        }
        if (attempt >= 2) dao.markUploadFailed(id, null)
        val file = row.localFilePath?.let(::File)?.takeIf { it.exists() && it.length() > 0L }
        if (file == null) {
            dao.markUploadFailed(id, null)
            return UploadOutcome.NoFile
        }
        val config = decode(key)
        if (config == null) {
            dao.markUploadFailed(id, null)
            return UploadOutcome.Permanent(null)
        }
        return try {
            // Streaming, not readBytes: a long recording must not sit in memory.
            val body = file.asRequestBody((row.mimeType ?: "audio/mp4").toMediaType())
            val part = MultipartBody.Part.createFormData("file", row.title, body)
            val response = api.upload(config, part)
            dao.updateReference(id, response.referenceId, TranscriptionEntity.STATUS_QUEUED)
            UploadOutcome.Success
        } catch (e: HttpException) {
            val code = e.code()
            if (code in 400..499 && code != 408 && code != 429) {
                dao.markUploadFailed(id, code)
                UploadOutcome.Permanent(code)
            } else {
                // IOException-class: 408, 429, 5xx (and any odd non-4xx)
                if (attempt >= 1) dao.markUploadFailed(id, null)
                UploadOutcome.Retryable
            }
        } catch (_: IOException) {
            if (attempt >= 1) {
                dao.markUploadFailed(id, null)
                UploadKickWorker.enqueue(context)
            }
            UploadOutcome.Retryable
        }
    }

    /** Startup / first-key: enqueue every unsent row that belongs to a live key. */
    suspend fun enqueuePendingUploads() {
        for (id in dao.getUploadableIds()) UploadWorker.enqueueKeep(context, id)
    }

    /** UploadKickWorker: REPLACE upload work for red rows still worth retrying. */
    suspend fun kickRetryableUploads() {
        for (id in dao.getRetryableUploadFailedIds()) UploadWorker.enqueueReplace(context, id)
    }

    /**
     * Poll all pending transcriptions and update local status. Each row uses its
     * own key; getPending already excludes soft-deleted rows and rows whose key
     * is gone.
     *
     * Returns true while rows remain pending — the worker retries on it.
     * 429, IOException and 5xx are transient and leave the row pending; any
     * other 4xx (e.g. 404, the reference deleted on the server) is permanent
     * and writes failed, so such a row does not poll forever.
     */
    suspend fun pollAndUpdate(): Boolean {
        val pending = dao.getPending()
        for (entry in pending) {
            val key = entry.keyId?.let { keyDao.getById(it) } ?: continue
            val config = decode(key) ?: continue
            try {
                val status = api.status(config, entry.referenceId)
                dao.updateStatus(entry.id, status.status ?: TranscriptionEntity.STATUS_FAILED, status.content)
            } catch (e: HttpException) {
                if (e.code() != 429 && e.code() in 400..499) {
                    dao.updateStatus(entry.id, TranscriptionEntity.STATUS_FAILED, null)
                }
            } catch (_: IOException) {
                // Offline — will retry on next poll cycle
            } catch (_: Exception) {
                // Unexpected — keep the row pending rather than killing the chain
            }
        }
        return dao.getPending().isNotEmpty()
    }

    /**
     * History delete: soft — the row hides at once, file and content stay for
     * the 4 s undo window, then [purgeById] removes them for good.
     */
    suspend fun softDelete(id: Long) {
        dao.softDelete(id, System.currentTimeMillis())
    }

    /**
     * Undo: the row returns with its pre-delete status. An unsent row is
     * enqueued again (its work exited on the deletedAt check); a pollable one
     * re-enters the poll cycle.
     */
    suspend fun undoDelete(id: Long) {
        val row = dao.getById(id) ?: return
        if (row.deletedAt == null) return
        dao.clearDeletedAt(id)
        when (row.status) {
            TranscriptionEntity.STATUS_PENDING_UPLOAD -> UploadWorker.enqueueKeep(context, id)
            TranscriptionEntity.STATUS_QUEUED, TranscriptionEntity.STATUS_PROCESSING ->
                PollTranscriptionWorker.enqueue(context)
        }
    }

    /** Purge removes row, file and work — after the undo window or a second delete. */
    suspend fun purgeById(id: Long) {
        cancelRowWork(id)
        dao.getById(id)?.localFilePath?.let { runCatching { File(it).delete() } }
        dao.delete(id)
    }

    private fun cancelRowWork(id: Long) {
        runCatching {
            WorkManager.getInstance(context).cancelUniqueWork(UploadWorker.workName(id))
        }
    }

    /** Purge soft-deleted rows: row, on-device file and work (the undo window has passed). */
    suspend fun purgeDeleted() {
        for (entry in dao.getSoftDeleted()) {
            cancelRowWork(entry.id)
            entry.localFilePath?.let { runCatching { File(it).delete() } }
            dao.delete(entry.id)
        }
    }

    /** Zero-key binding at startup: keyless rows adopt the first live key, if any. */
    suspend fun bindKeylessRowsToFirstKey() {
        val first = keyDao.getLive().firstOrNull() ?: return
        dao.bindKeylessRows(first.id)
    }
}
