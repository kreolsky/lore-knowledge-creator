package com.lore.voicenotes.ui.record

import android.content.Context
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.lore.voicenotes.BuildConfig
import com.lore.voicenotes.data.local.DocumentKeyEntity
import com.lore.voicenotes.data.repository.AddKeyResult
import com.lore.voicenotes.data.repository.KeyRepository
import com.lore.voicenotes.data.repository.TranscriptionRepository
import com.lore.voicenotes.worker.PollTranscriptionWorker
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import javax.inject.Inject

/** One-shot signals for the record screen (word flashes in the timer slot). */
sealed interface RecordSignal {
    /** A picked file could not be copied/saved on the device. */
    data object NotSaved : RecordSignal
}

@HiltViewModel
class RecordViewModel @Inject constructor(
    private val repository: TranscriptionRepository,
    private val keyRepository: KeyRepository,
    @ApplicationContext private val appContext: Context,
) : ViewModel() {

    private val _signals = MutableSharedFlow<RecordSignal>(extraBufferCapacity = 4)
    val signals: SharedFlow<RecordSignal> = _signals.asSharedFlow()

    private val _elapsedSeconds = MutableStateFlow(0L)
    val elapsedSeconds: StateFlow<Long> = _elapsedSeconds.asStateFlow()

    /** Document tabs, ordered by position; zero keys leaves the «+» tab alone. */
    val liveKeys: StateFlow<List<DocumentKeyEntity>> =
        keyRepository.liveKeys.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    /** Red rows — the persistent «Не отправлено: N» line in the timer slot. */
    val uploadFailedCount: StateFlow<Int> =
        repository.uploadFailedCount.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), 0)

    /**
     * The effective selected key id — the key the next recording travels with
     * (same fallback to the first key as KeyRepository.getSelected).
     */
    val selectedKeyId: StateFlow<Long?> = combine(
        keyRepository.liveKeys,
        keyRepository.selectedKeyId,
    ) { live, selectedId ->
        (live.firstOrNull { it.id == selectedId } ?: live.firstOrNull())?.id
    }.stateIn(viewModelScope, SharingStarted.Eagerly, null)

    private val _addField = MutableStateFlow("")
    val addField: StateFlow<String> = _addField.asStateFlow()

    private val _addErrorRes = MutableStateFlow<Int?>(null)
    val addErrorRes: StateFlow<Int?> = _addErrorRes.asStateFlow()

    private val _adding = MutableStateFlow(false)
    val adding: StateFlow<Boolean> = _adding.asStateFlow()

    private var timerJob: Job? = null

    fun onRecordingStarted() {
        _elapsedSeconds.value = 0
        timerJob = viewModelScope.launch {
            while (true) {
                delay(1000)
                _elapsedSeconds.value++
            }
        }
    }

    fun onRecordingStopped() {
        timerJob?.cancel()
        timerJob = null
        _elapsedSeconds.value = 0
    }

    fun selectKey(id: Long) {
        viewModelScope.launch { keyRepository.setSelected(id) }
    }

    fun updateAddField(value: String) {
        _addField.value = value
        _addErrorRes.value = null
    }

    /** Clear the in-dialog error when the dialog closes; the field survives. */
    fun dismissAddError() {
        _addErrorRes.value = null
    }

    /**
     * Add a key from the «+» tab. Same rules as Settings: Invalid keeps the
     * dialog open with «Неверный ключ»; Added/Selected close it (a duplicate
     * also selects its tab, the caller unrolls it).
     */
    fun addKey(onResult: (AddKeyResult) -> Unit) {
        val raw = _addField.value
        if (raw.isBlank() || _adding.value) return
        _adding.value = true
        viewModelScope.launch {
            try {
                val result = keyRepository.addKey(raw)
                when (result) {
                    is AddKeyResult.Added -> {
                        _addField.value = ""
                        _addErrorRes.value = null
                        // A restored key's queued rows are pollable again
                        PollTranscriptionWorker.enqueue(appContext)
                    }
                    is AddKeyResult.Selected -> {
                        _addField.value = ""
                        _addErrorRes.value = null
                    }
                    AddKeyResult.Invalid -> Unit
                }
                onResult(result)
            } finally {
                _adding.value = false
            }
        }
    }

    /**
     * Upload a file chosen from the system file picker: copy the stream into
     * our recordings dir off the main thread (streaming, not buffering, so a
     * long file never sits in memory), then save it like a recording.
     * Persisting and enqueueing a finished recording is the service's job.
     */
    fun uploadFile(uri: Uri, filename: String, mimeType: String) {
        viewModelScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    val dir = File(appContext.filesDir, "recordings").apply { mkdirs() }
                    val dest = File(dir, "upload_${System.currentTimeMillis()}_$filename")
                    appContext.contentResolver.openInputStream(uri)?.use { input ->
                        dest.outputStream().use { input.copyTo(it) }
                    } ?: throw IOException("Cannot open the picked file")
                    if (BuildConfig.DEBUG) Log.i("Upload", "file=$filename mime=$mimeType size=${dest.length()}")
                    // A picked file has no encoder to count samples; the
                    // container metadata is the duration source. A null shows
                    // no duration; there is no backfill.
                    val durationMs = runCatching {
                        MediaMetadataRetriever().use { r ->
                            r.setDataSource(dest.absolutePath)
                            r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull()
                        }
                    }.getOrNull()
                    repository.save(dest, filename, mimeType, keyRepository.getSelected()?.id, durationMs)
                }
            } catch (e: Exception) {
                _signals.tryEmit(RecordSignal.NotSaved)
            }
        }
    }
}
