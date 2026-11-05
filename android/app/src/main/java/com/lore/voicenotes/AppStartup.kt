package com.lore.voicenotes

import android.content.Context
import com.lore.voicenotes.data.repository.KeyRepository
import com.lore.voicenotes.data.repository.TranscriptionRepository
import com.lore.voicenotes.worker.PollTranscriptionWorker
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * One ordered startup coroutine, launched from LoreApp on the @ApplicationScope.
 * Each step is idempotent, so a crash between steps is safe on re-run.
 *
 * Order: (1) legacy DataStore key becomes a document_keys row and the DataStore
 * key is cleared; (2) purge soft-deleted rows; (3) bind keyless rows to the
 * first key, if there is one; (4) enqueue unsent uploads (KEEP, rows with a
 * live key); (5) enqueue poll; (6) refresh key titles, best effort.
 */
@Singleton
class AppStartup @Inject constructor(
    @ApplicationContext private val context: Context,
    private val keyRepository: KeyRepository,
    private val transcriptionRepository: TranscriptionRepository,
) {
    suspend fun run() {
        keyRepository.migrateDataStoreKey()
        transcriptionRepository.purgeDeleted()
        transcriptionRepository.bindKeylessRowsToFirstKey()
        transcriptionRepository.enqueuePendingUploads()
        PollTranscriptionWorker.enqueue(context)
        keyRepository.refreshTitles()
    }
}
