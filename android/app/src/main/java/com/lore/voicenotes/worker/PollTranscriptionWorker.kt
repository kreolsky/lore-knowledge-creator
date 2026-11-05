package com.lore.voicenotes.worker

import android.content.Context
import androidx.hilt.work.HiltWorker
import androidx.work.BackoffPolicy
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

@HiltWorker
class PollTranscriptionWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted params: WorkerParameters,
    private val repository: TranscriptionRepository,
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val stillPending = repository.pollAndUpdate()
        return if (stillPending) Result.retry() else Result.success()
    }

    companion object {
        private const val WORK_NAME = "poll_transcriptions"

        /**
         * Enqueue a one-time poll with linear backoff (15 s x attempt). The worker
         * retries while pending rows remain; REPLACE means a fresh enqueue (app
         * start, upload) cancels any outstanding backoff wait at once.
         */
        fun enqueue(context: Context) {
            val request = OneTimeWorkRequestBuilder<PollTranscriptionWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .setInitialDelay(5, TimeUnit.SECONDS)
                .setBackoffCriteria(BackoffPolicy.LINEAR, 15, TimeUnit.SECONDS)
                .build()

            WorkManager.getInstance(context)
                .enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.REPLACE, request)
        }
    }
}
