package com.lore.voicenotes.ui.record

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.lore.voicenotes.audio.AudioCaptureService
import com.lore.voicenotes.data.local.DecodedConfig
import com.lore.voicenotes.data.local.SettingsDataStore
import com.lore.voicenotes.data.repository.TranscriptionRepository
import com.lore.voicenotes.worker.PollTranscriptionWorker
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import java.io.File
import javax.inject.Inject

@HiltViewModel
class RecordViewModel @Inject constructor(
    private val repository: TranscriptionRepository,
    settingsDataStore: SettingsDataStore,
    @ApplicationContext private val appContext: Context,
) : ViewModel() {

    val config: StateFlow<DecodedConfig?> = settingsDataStore.configFlow
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), null)

    private val _elapsedSeconds = MutableStateFlow(0L)
    val elapsedSeconds: StateFlow<Long> = _elapsedSeconds.asStateFlow()

    private val _uploading = MutableStateFlow(false)
    val uploading: StateFlow<Boolean> = _uploading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private var timerJob: Job? = null

    init {
        // Observe service recording state: when it transitions true→false,
        // the encoder has finalized the file — safe to read and upload.
        viewModelScope.launch {
            var wasRecording = false
            AudioCaptureService.isRecording.collect { recording ->
                if (wasRecording && !recording) {
                    onServiceStopped()
                }
                wasRecording = recording
            }
        }
    }

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

    /** Called when the service has finished encoding and the file is ready. */
    private fun onServiceStopped() {
        timerJob?.cancel()
        timerJob = null
        _elapsedSeconds.value = 0

        val file = AudioCaptureService.lastOutputFile.value ?: return
        if (!file.exists() || file.length() == 0L) return

        _uploading.value = true
        _error.value = null
        viewModelScope.launch {
            try {
                // saveAndUpload persists the row BEFORE the network call, so an upload
                // failure leaves the recording re-sendable from history (not lost).
                repository.saveAndUpload(file, file.name, "audio/mp4")
                PollTranscriptionWorker.enqueue(appContext)
            } catch (e: Exception) {
                _error.value = e.message ?: "Upload failed — saved on device, re-send from History"
            } finally {
                _uploading.value = false
            }
        }
    }

    /** Upload a file chosen from the system file picker. */
    fun uploadFile(bytes: ByteArray, filename: String, mimeType: String) {
        _uploading.value = true
        _error.value = null
        viewModelScope.launch {
            try {
                // Copy the picked bytes into our recordings dir so the entry is re-sendable too.
                val dir = File(appContext.filesDir, "recordings").apply { mkdirs() }
                val dest = File(dir, "upload_${System.currentTimeMillis()}_$filename")
                dest.writeBytes(bytes)
                repository.saveAndUpload(dest, filename, mimeType)
                PollTranscriptionWorker.enqueue(appContext)
            } catch (e: Exception) {
                _error.value = e.message ?: "Upload failed — saved on device, re-send from History"
            } finally {
                _uploading.value = false
            }
        }
    }

    fun setError(message: String) { _error.value = message }

    fun clearError() { _error.value = null }
}
