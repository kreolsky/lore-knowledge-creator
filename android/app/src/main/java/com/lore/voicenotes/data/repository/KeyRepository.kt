package com.lore.voicenotes.data.repository

import android.content.Context
import androidx.room.withTransaction
import com.lore.voicenotes.data.local.AppDatabase
import com.lore.voicenotes.data.local.DocumentKeyDao
import com.lore.voicenotes.data.local.DocumentKeyEntity
import com.lore.voicenotes.data.local.SettingsDataStore
import com.lore.voicenotes.data.local.TokenHash
import com.lore.voicenotes.data.remote.LoreApiService
import com.lore.voicenotes.data.remote.WidgetInfoResponse
import com.lore.voicenotes.worker.PollTranscriptionWorker
import com.lore.voicenotes.worker.UploadKickWorker
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.firstOrNull
import retrofit2.HttpException
import java.io.IOException
import javax.inject.Inject
import javax.inject.Singleton

sealed interface AddKeyResult {
    data class Added(val key: DocumentKeyEntity) : AddKeyResult
    /** A live key with this token already existed — it was selected, not duplicated. */
    data class Selected(val key: DocumentKeyEntity) : AddKeyResult
    /** Undecodable, or the server refused it (401/403/410). */
    data object Invalid : AddKeyResult
}

/** Key management: the key list, the selection, and the add/delete/reorder rules. */
@Singleton
class KeyRepository @Inject constructor(
    @ApplicationContext private val context: Context,
    private val db: AppDatabase,
    private val keyDao: DocumentKeyDao,
    private val settingsDataStore: SettingsDataStore,
    private val api: LoreApiService,
    private val transcriptionRepository: TranscriptionRepository,
) {
    val liveKeys: Flow<List<DocumentKeyEntity>> = keyDao.observeLive()

    val selectedKeyId: Flow<Long?> = settingsDataStore.selectedKeyIdFlow

    /** The live selected key, falling back to the first by position; null with zero keys. */
    suspend fun getSelected(): DocumentKeyEntity? {
        val live = keyDao.getLive()
        val selectedId = settingsDataStore.selectedKeyIdFlow.firstOrNull()
        return live.firstOrNull { it.id == selectedId } ?: live.firstOrNull()
    }

    suspend fun setSelected(id: Long) {
        val key = keyDao.getById(id) ?: return
        if (key.deletedAt != null) return
        settingsDataStore.setSelectedKeyId(id)
    }

    /**
     * Add a pasted all-in-one key. Undecodable input and info() 401/403/410 are
     * refused with Invalid; IOException, 429 and 5xx still save the key with a
     * null title, refreshed at the next start. A token that matches a deleted
     * key restores that row; a live one selects it instead.
     */
    suspend fun addKey(raw: String): AddKeyResult {
        val encoded = raw.trim()
        val config = SettingsDataStore.decodeApiKey(encoded) ?: return AddKeyResult.Invalid
        val hash = TokenHash.of(config.token)

        val existing = keyDao.getByTokenHash(hash)
        if (existing != null && existing.deletedAt == null) {
            settingsDataStore.setSelectedKeyId(existing.id)
            return AddKeyResult.Selected(existing)
        }

        var info: WidgetInfoResponse? = null
        try {
            info = api.info(config)
        } catch (e: HttpException) {
            when (e.code()) {
                401, 403, 410 -> return AddKeyResult.Invalid
                // 429, other 5xx — transient: add offline, titles arrive at the next start
            }
        } catch (_: IOException) {
            // Offline — save with a null title, refreshed at the next start
        } catch (_: Exception) {
            return AddKeyResult.Invalid
        }

        val hadLiveKeys = keyDao.getLive().isNotEmpty()
        val position = nextPosition()
        val id: Long
        if (existing != null) {
            // Re-added token of a deleted key: same id, rows keep their keyId.
            // Its rows live again without any rebinding: re-send the red ones,
            // poll the queued ones.
            keyDao.restore(existing.id, encoded, position)
            id = existing.id
            UploadKickWorker.enqueue(context)
            PollTranscriptionWorker.enqueue(context)
        } else {
            id = keyDao.insert(
                DocumentKeyEntity(
                    position = position,
                    encodedKey = encoded,
                    tokenHash = hash,
                    documentTitle = info?.documentTitle,
                    projectName = info?.projectName,
                )
            )
        }
        settingsDataStore.setSelectedKeyId(id)
        // Zero-key binding: the first live key (fresh or restored) adopts every
        // keyless row and all unsent rows are enqueued.
        if (!hadLiveKeys) {
            db.transcriptionDao().bindKeylessRows(id)
            transcriptionRepository.enqueuePendingUploads()
        }
        val key = keyDao.getById(id) ?: return AddKeyResult.Invalid
        return AddKeyResult.Added(key)
    }

    /**
     * Soft-delete: the token leaves the phone, hash and titles stay; unsent rows
     * turn upload_failed in the same transaction; the selection falls back to
     * the first remaining key.
     */
    suspend fun deleteKey(id: Long) {
        val key = keyDao.getById(id) ?: return
        if (key.deletedAt != null) return
        db.withTransaction {
            keyDao.softDelete(id, System.currentTimeMillis())
            db.transcriptionDao().markPendingUploadFailed(id)
        }
        renumber()
        val selectedId = settingsDataStore.selectedKeyIdFlow.firstOrNull()
        if (selectedId == id) {
            val first = keyDao.getLive().firstOrNull()
            if (first != null) settingsDataStore.setSelectedKeyId(first.id)
            else settingsDataStore.clearSelectedKeyId()
        }
    }

    suspend fun moveUp(id: Long) = move(id, -1)

    suspend fun moveDown(id: Long) = move(id, +1)

    private suspend fun move(id: Long, delta: Int) {
        val live = keyDao.getLive()
        val index = live.indexOfFirst { it.id == id }
        if (index < 0) return
        val target = index + delta
        if (target < 0 || target >= live.size) return
        val reordered = live.toMutableList().apply { add(target, removeAt(index)) }
        keyDao.renumber(reordered.map { it.id })
    }

    private suspend fun renumber() {
        keyDao.renumber(keyDao.getLive().map { it.id })
    }

    private suspend fun nextPosition(): Int =
        (keyDao.getLive().maxOfOrNull { it.position ?: 0 } ?: 0) + 1

    /**
     * v3 one-time migration: the legacy DataStore key becomes the first
     * document_keys row (unless its token already exists — a crash between the
     * two writes is safe on re-run), then the DataStore key is cleared.
     */
    suspend fun migrateDataStoreKey() {
        val encoded = settingsDataStore.encodedKeyFlow.firstOrNull() ?: ""
        if (encoded.isNotBlank()) {
            val config = SettingsDataStore.decodeApiKey(encoded)
            if (config != null) {
                val hash = TokenHash.of(config.token)
                val existing = keyDao.getByTokenHash(hash)
                when {
                    existing == null -> {
                        val id = keyDao.insert(
                            DocumentKeyEntity(position = 1, encodedKey = encoded, tokenHash = hash)
                        )
                        settingsDataStore.setSelectedKeyId(id)
                    }
                    existing.deletedAt == null ->
                        settingsDataStore.setSelectedKeyId(existing.id)
                    else -> {
                        keyDao.restore(existing.id, encoded, nextPosition())
                        settingsDataStore.setSelectedKeyId(existing.id)
                    }
                }
            }
            // Blank/undecodable leftovers are dropped as well
            settingsDataStore.clearEncodedKey()
        }
        normalizeSelection()
    }

    /** A stale selected id (deleted key) falls back to the first live key. */
    suspend fun normalizeSelection() {
        val live = keyDao.getLive()
        if (live.isEmpty()) {
            settingsDataStore.clearSelectedKeyId()
            return
        }
        val selectedId = settingsDataStore.selectedKeyIdFlow.firstOrNull()
        if (live.none { it.id == selectedId }) {
            settingsDataStore.setSelectedKeyId(live.first().id)
        }
    }

    /** Best-effort title refresh through info(); failures keep the stored titles. */
    suspend fun refreshTitles() {
        for (key in keyDao.getLive()) {
            val encoded = key.encodedKey ?: continue
            val config = SettingsDataStore.decodeApiKey(encoded) ?: continue
            try {
                val info = api.info(config)
                keyDao.updateTitles(key.id, info.documentTitle, info.projectName)
            } catch (_: Exception) {
                // Best effort — the next start retries
            }
        }
    }
}
