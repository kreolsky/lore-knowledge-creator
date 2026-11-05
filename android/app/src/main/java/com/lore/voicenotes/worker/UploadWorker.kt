package com.lore.voicenotes.worker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import androidx.hilt.work.HiltWorker
import androidx.work.BackoffPolicy
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.lore.voicenotes.R
import com.lore.voicenotes.data.repository.TranscriptionRepository
import com.lore.voicenotes.data.repository.UploadOutcome
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Uploads one row's audio under the row's own key.
 *
 * ARCH: unique work per row (upload_<rowId>), no network constraint, linear
 * 30 s backoff. KEEP from automatic paths (save, startup, first-key binding);
 * REPLACE from manual re-send and UploadKickWorker, which must cancel a
 * backed-off retry — WorkManager backoff can sleep up to 5 h.
 *
 * Exit rules: a soft-deleted row, a keyId-NULL row (zero keys) and a row whose
 * key is gone all exit success — the last one is first marked upload_failed,
 * because a recording can finish after its key was deleted. On success the
 * repository rebinds the row to queued and the worker enqueues poll.
 */
@HiltWorker
class UploadWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val repository: TranscriptionRepository,
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val id = inputData.getLong(KEY_ROW_ID, -1L)
        if (id < 0) return Result.failure()
        maybeGoForeground(id)
        return when (val outcome = repository.uploadRow(id, runAttemptCount)) {
            UploadOutcome.Success -> {
                PollTranscriptionWorker.enqueue(applicationContext)
                Result.success()
            }
            UploadOutcome.Skipped, UploadOutcome.KeyDeleted -> Result.success()
            UploadOutcome.Retryable -> Result.retry()
            is UploadOutcome.Permanent, UploadOutcome.NoFile -> Result.failure()
        }
    }

    /**
     * Long uploads go foreground (dataSync): a background worker is killed at
     * ~10 minutes. Android 12+ refuses a foreground promotion started from the
     * background — then the worker keeps the 10-minute limit and continues.
     */
    private suspend fun maybeGoForeground(id: Long) {
        val path = repository.getById(id)?.localFilePath ?: return
        val file = File(path)
        if (!file.exists() || file.length() <= FOREGROUND_MIN_BYTES) return
        try {
            setForeground(foregroundInfo(id))
        } catch (_: Exception) {
            // ForegroundServiceStartNotAllowedException on Android 12+ — continue
        }
    }

    private fun foregroundInfo(id: Long): ForegroundInfo {
        val nm = applicationContext.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                applicationContext.getString(R.string.notification_upload),
                NotificationManager.IMPORTANCE_LOW,
            )
        )
        val notification = Notification.Builder(applicationContext, CHANNEL_ID)
            .setContentTitle(applicationContext.getString(R.string.app_name))
            .setContentText(applicationContext.getString(R.string.notification_upload))
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .build()
        return ForegroundInfo(
            NOTIFICATION_ID_BASE + id.toInt(),
            notification,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
        )
    }

    companion object {
        const val KEY_ROW_ID = "row_id"
        private const val FOREGROUND_MIN_BYTES = 10L * 1024 * 1024
        private const val NOTIFICATION_ID_BASE = 200
        const val CHANNEL_ID = "lore_upload"

        fun workName(rowId: Long) = "upload_$rowId"

        fun enqueueKeep(context: Context, rowId: Long) =
            enqueue(context, rowId, ExistingWorkPolicy.KEEP)

        fun enqueueReplace(context: Context, rowId: Long) =
            enqueue(context, rowId, ExistingWorkPolicy.REPLACE)

        private fun enqueue(context: Context, rowId: Long, policy: ExistingWorkPolicy) {
            val request = OneTimeWorkRequestBuilder<UploadWorker>()
                .setInputData(workDataOf(KEY_ROW_ID to rowId))
                .setBackoffCriteria(BackoffPolicy.LINEAR, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(workName(rowId), policy, request)
        }
    }
}
