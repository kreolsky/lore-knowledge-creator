package com.lore.voicenotes.ui.history

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.lore.voicenotes.audio.PlayerController
import com.lore.voicenotes.data.repository.TranscriptionRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import javax.inject.Inject

data class PlayerBarUiState(
    val entryId: Long? = null,
    val playing: Boolean = false,
    val positionMs: Long = 0L,
    val durationMs: Long = 0L,
    /** The row's createdAt; the bar formats it with resources at render time. */
    val createdAt: Long? = null,
    val playable: Boolean = false,
)

/**
 * The bar above navigation shows the active playback item on every tab. The
 * row's own data (title, duration, file) rides along; a row that vanished
 * (deleted — playback of it stopped) clears the bar.
 */
@HiltViewModel
class PlayerBarViewModel @Inject constructor(
    private val player: PlayerController,
    repository: TranscriptionRepository,
) : ViewModel() {

    val state: StateFlow<PlayerBarUiState> = combine(
        player.state,
        repository.all,
    ) { p, rows ->
        val id = p.entryId ?: return@combine PlayerBarUiState()
        val row = rows.firstOrNull { it.id == id } ?: return@combine PlayerBarUiState()
        PlayerBarUiState(
            entryId = id,
            playing = p.playing,
            positionMs = p.positionMs,
            durationMs = if (p.durationMs > 0) p.durationMs else row.durationMs ?: 0L,
            createdAt = row.createdAt,
            playable = row.localFilePath != null,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), PlayerBarUiState())

    fun playPause() = player.toggleCurrent()

    fun seek(fraction: Float) {
        val s = state.value
        val dur = s.durationMs
        if (s.entryId != null && dur > 0) player.seekTo((fraction * dur).toLong())
    }

    fun close() = player.stop()
}
