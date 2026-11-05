package com.lore.voicenotes.audio

import android.media.MediaPlayer
import com.lore.voicenotes.di.ApplicationScope
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import javax.inject.Inject
import javax.inject.Singleton

data class PlayerState(
    val entryId: Long? = null,
    val playing: Boolean = false,
    val positionMs: Long = 0L,
    val durationMs: Long = 0L,
)

/**
 * ARCH: player — the single active playback item, platform MediaPlayer only.
 *
 * One item plays at a time; the StateFlow (entryId, playing, positionMs,
 * durationMs) drives every scrubber (list bar, detail card). Playback stops
 * when a recording starts and when its row is deleted, and pauses in
 * MainActivity.onStop. All MediaPlayer calls are confined to the main
 * dispatcher; MediaPlayer is not thread-safe.
 */
@Singleton
class PlayerController @Inject constructor(
    @ApplicationScope private val scope: CoroutineScope,
) {
    private val _state = MutableStateFlow(PlayerState())
    val state: StateFlow<PlayerState> = _state.asStateFlow()

    private var player: MediaPlayer? = null
    private var prepared = false
    private var durationFallback = 0L
    private var ticker: Job? = null

    init {
        // A recording takes priority: the mic must not fight playback.
        scope.launch {
            AudioCaptureService.isRecording.collect { if (it) stop() }
        }
    }

    /** Play/pause a row; a different row stops the previous item first. */
    fun toggle(entryId: Long, path: String, durationMs: Long) {
        scope.launch(Dispatchers.Main) {
            if (_state.value.entryId == entryId) togglePrepared()
            else prepare(entryId, path, durationMs)
        }
    }

    /** Pause/resume the active item (the bar's play button knows no path). */
    fun toggleCurrent() {
        scope.launch(Dispatchers.Main) { togglePrepared() }
    }

    /** Pause the active item, if any (activity stopped). */
    fun pause() {
        scope.launch(Dispatchers.Main) {
            val mp = player ?: return@launch
            if (!prepared || !_state.value.playing) return@launch
            mp.pause()
            _state.value = _state.value.copy(playing = false)
        }
    }

    /** Seek the active item; position updates at once so the scrubber follows. */
    fun seekTo(positionMs: Long) {
        scope.launch(Dispatchers.Main) {
            val mp = player ?: return@launch
            if (!prepared) return@launch
            val dur = effectiveDuration()
            val clamped = if (dur > 0) positionMs.coerceIn(0L, dur) else positionMs
            mp.seekTo(clamped.toInt())
            _state.value = _state.value.copy(positionMs = clamped)
        }
    }

    /** Stop and clear — the bar closes, the row's play button resets. */
    fun stop() {
        scope.launch(Dispatchers.Main) {
            releasePlayer()
            _state.value = PlayerState()
        }
    }

    /** Stop only if [entryId] is the active item (its row was deleted). */
    fun stopIf(entryId: Long) {
        if (_state.value.entryId == entryId) stop()
    }

    private fun togglePrepared() {
        val mp = player ?: return
        if (!prepared) return
        if (_state.value.playing) {
            mp.pause()
            _state.value = _state.value.copy(playing = false)
        } else {
            val dur = effectiveDuration()
            if (dur > 0 && _state.value.positionMs >= dur) mp.seekTo(0)
            mp.start()
            _state.value = _state.value.copy(playing = true)
            startTicker()
        }
    }

    private fun prepare(entryId: Long, path: String, durationMs: Long) {
        releasePlayer()
        durationFallback = durationMs
        val mp = MediaPlayer()
        player = mp
        mp.setDataSource(path)
        mp.setOnPreparedListener { p ->
            prepared = true
            p.start()
            _state.value = PlayerState(
                entryId = entryId,
                playing = true,
                positionMs = 0L,
                durationMs = effectiveDuration(),
            )
            startTicker()
        }
        mp.setOnCompletionListener {
            // Stay on the finished item: the bar keeps the final position.
            _state.value = _state.value.copy(playing = false, positionMs = effectiveDuration())
        }
        mp.setOnErrorListener { _, _, _ ->
            // No silent degradation: a failed playback clears the player outright.
            scope.launch(Dispatchers.Main) {
                releasePlayer()
                _state.value = PlayerState()
            }
            true
        }
        // The bar appears at once; the scrubber enables when prepared.
        _state.value = PlayerState(entryId = entryId)
        mp.prepareAsync()
    }

    private fun effectiveDuration(): Long {
        val live = player?.duration?.takeIf { it > 0 }?.toLong()
        return live ?: durationFallback
    }

    private fun startTicker() {
        ticker?.cancel()
        ticker = scope.launch(Dispatchers.Main) {
            while (isActive) {
                delay(POSITION_TICK_MS)
                val mp = player ?: break
                if (prepared && _state.value.playing) {
                    val dur = effectiveDuration()
                    val pos = mp.currentPosition.toLong()
                    _state.value = _state.value.copy(
                        positionMs = if (dur > 0) pos.coerceAtMost(dur) else pos,
                    )
                }
            }
        }
    }

    private fun releasePlayer() {
        ticker?.cancel()
        ticker = null
        player?.let { mp ->
            runCatching { mp.stop() }
            runCatching { mp.release() }
        }
        player = null
        prepared = false
    }

    private companion object {
        const val POSITION_TICK_MS = 250L
    }
}
