package com.lore.voicenotes.worker

import android.content.Context
import androidx.hilt.work.HiltWorker
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.lore.voicenotes.data.repository.TranscriptionRepository
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import java.util.concurrent.TimeUnit

/**
 * Network return: after an IOException wrote upload_failed, this worker waits
 * for connectivity (60 s calm-down, KEEP so repeated offline failures do not
 * stack) and REPLACEs upload work for every red row with a live key and no
 * permanent uploadErrorCode — a backed-off retry can otherwise sleep up to 5 h.
 */
@HiltWorker
class UploadKickWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val repository: TranscriptionRepository,
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        repository.kickRetryableUploads()
        return Result.success()
    }

    companion object {
        private const val WORK_NAME = "upload_kick"

        fun enqueue(context: Context) {
            val request = OneTimeWorkRequestBuilder<UploadKickWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .setInitialDelay(60, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context)
                .enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.KEEP, request)
        }
    }
}
