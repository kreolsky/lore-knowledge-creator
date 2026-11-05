package com.lore.voicenotes.ui.history

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.lore.voicenotes.audio.PlayerController
import com.lore.voicenotes.audio.PlayerState
import com.lore.voicenotes.data.local.DocumentKeyEntity
import com.lore.voicenotes.data.local.TranscriptionEntity
import com.lore.voicenotes.data.repository.KeyRepository
import com.lore.voicenotes.data.repository.TranscriptionRepository
import com.lore.voicenotes.di.ApplicationScope
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

/** One history row with its key's current position; null hides the doc number. */
data class HistoryRow(
    val entry: TranscriptionEntity,
    val docPosition: Int?,
)

/** A delete waiting for its 4 s undo window to pass. */
private data class PendingUndo(val id: Long)

/**
 * ARCH: the 4 s undo timer lives here, not in a rememberCoroutineScope —
 * that dies when the screen leaves composition on navigation to the detail;
 * the view model outlives it inside the back stack. Leaving History for good
 * clears the view model, and onCleared purges the pending row.
 */
@HiltViewModel
class HistoryViewModel @Inject constructor(
    private val repository: TranscriptionRepository,
    private val player: PlayerController,
    keyRepository: KeyRepository,
    @ApplicationScope private val appScope: CoroutineScope,
) : ViewModel() {

    val rows: StateFlow<List<HistoryRow>> = combine(
        repository.all,
        keyRepository.liveKeys,
    ) { entries, keys ->
        val positions = keys.associate { it.id to (it.position ?: 0) }
        // A deleted key keeps no position → its rows show no document number.
        entries.map { HistoryRow(it, it.keyId?.let(positions::get)) }
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    /** Live keys for the detail head («Документ NN»). */
    val keys: StateFlow<List<DocumentKeyEntity>> = keyRepository.liveKeys
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    val playerState: StateFlow<PlayerState> = player.state

    /** A refused re-send (no local file, deleted key, zero keys) nudges the row. */
    private val _resendRefused = MutableSharedFlow<Long>(extraBufferCapacity = 4)
    val resendRefused: SharedFlow<Long> = _resendRefused

    /** The soft-deleted row awaiting its undo window; null = no bar. */
    private val _undo = MutableStateFlow<PendingUndo?>(null)
    val undo: StateFlow<Long?> = _undo
        .map { it?.id }
        .stateIn(viewModelScope, SharingStarted.Eagerly, null)

    private var undoJob: Job? = null

    /** Soft-delete with undo: purge any pending delete, hide the row, arm 4 s. */
    fun delete(id: Long) {
        purgePendingNow()
        viewModelScope.launch {
            repository.softDelete(id)
            player.stopIf(id)
        }
        _undo.value = PendingUndo(id)
        undoJob?.cancel()
        undoJob = viewModelScope.launch {
            delay(UNDO_MS)
            purgePendingNow()
        }
    }

    fun undoDelete() {
        val current = _undo.value ?: return
        undoJob?.cancel()
        _undo.value = null
        viewModelScope.launch { repository.undoDelete(current.id) }
    }

    private fun purgePendingNow() {
        val current = _undo.value ?: return
        undoJob?.cancel()
        _undo.value = null
        viewModelScope.launch { repository.purgeById(current.id) }
    }

    /** Re-upload the row's local audio; a refusal nudges the row, no words. */
    fun resend(id: Long) {
        viewModelScope.launch {
            try {
                repository.resend(id)
            } catch (_: IllegalStateException) {
                _resendRefused.tryEmit(id)
            }
        }
    }

    fun playPause(id: Long, path: String?, durationMs: Long?) {
        if (path == null) return
        player.toggle(id, path, durationMs ?: 0L)
    }

    fun seekTo(positionMs: Long) = player.seekTo(positionMs)

    fun observeEntry(id: Long) = repository.observeById(id)

    override fun onCleared() {
        // Leaving the screen with an undo pending still purges: row, file, work.
        val current = _undo.value
        if (current != null) {
            appScope.launch { repository.purgeById(current.id) }
        }
        super.onCleared()
    }

    private companion object {
        const val UNDO_MS = 4000L
    }
}
