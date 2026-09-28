package com.lore.voicenotes.ui.history

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import android.content.Context
import com.lore.voicenotes.data.local.TranscriptionEntity
import com.lore.voicenotes.data.repository.TranscriptionRepository
import com.lore.voicenotes.worker.PollTranscriptionWorker
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class HistoryViewModel @Inject constructor(
    private val repository: TranscriptionRepository,
    @ApplicationContext private val appContext: Context,
) : ViewModel() {

    val transcriptions: StateFlow<List<TranscriptionEntity>> = repository.all
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    private val _messages = MutableSharedFlow<String>(extraBufferCapacity = 1)
    val messages: SharedFlow<String> = _messages

    suspend fun getById(id: Long) = repository.getById(id)

    fun delete(id: Long) {
        viewModelScope.launch { repository.delete(id) }
    }

    /** Re-upload the entry's local audio for (re-)transcription. */
    fun resend(id: Long) {
        viewModelScope.launch {
            try {
                repository.resend(id)
                PollTranscriptionWorker.enqueue(appContext)
                _messages.emit("Re-sending for transcription")
            } catch (e: Exception) {
                _messages.emit(e.message ?: "No local audio to re-send")
            }
        }
    }
}
