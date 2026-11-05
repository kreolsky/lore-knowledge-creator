package com.lore.voicenotes.audio

import android.media.projection.MediaProjection
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.io.File
import kotlin.math.log10
import kotlin.math.sqrt

/** What a recording captures: mic, system audio, or both. */
enum class CaptureMode {
    MIC, SYSTEM, BOTH
}

/**
 * One recording's resources held together: recorders, encoder, capture job,
 * projection, output file and the key it was started under.
 *
 * WHY an object per recording: service-level fields conflate a fast stop+start —
 * a new start used to overwrite the encoder while the previous file was still
 * being finalized, so the first recording was never persisted. The stop tail
 * works on the session it captured, immune to whatever the service does next.
 *
 * ARCH: SYSTEM mode bypasses the mixer and feeds the encoder directly — the
 * mixer is mic-clocked and emits nothing without mic chunks. BOTH is the only
 * mode that mixes.
 */
class RecordingSession(
    val file: File,
    val keyId: Long?,
    val mode: CaptureMode,
    projection: MediaProjection?,
    private val scope: CoroutineScope,
    private val onLevel: (Float) -> Unit,
) {
    init {
        if (mode != CaptureMode.MIC) {
            requireNotNull(projection) { "System capture requires a MediaProjection" }
        }
    }

    private val encoder = AudioEncoder(file)
    private val micRecorder = if (mode != CaptureMode.SYSTEM) MicRecorder() else null
    private val systemRecorder = if (mode != CaptureMode.MIC) {
        SystemAudioRecorder(requireNotNull(projection))
    } else null
    private var activeProjection = projection
    private var job: Job? = null
    private var systemJob: Job? = null
    @Volatile private var droppedSystem = false

    /** Encoded duration so far, derived by the encoder from the sample count. */
    val durationMs: Long get() = encoder.durationMs

    fun start() {
        job = scope.launch {
            when (mode) {
                CaptureMode.MIC ->
                    micRecorder!!.start().collect { write(it) }
                CaptureMode.SYSTEM ->
                    systemRecorder!!.start().collect { write(it) }
                CaptureMode.BOTH -> {
                    val mixer = AudioMixer()
                    launch { micRecorder!!.start().collect { mixer.submitMic(it) } }
                    // Guard the launch race: the projection can already be dead
                    // by the time this body runs (dropSystem came first).
                    if (!droppedSystem) {
                        systemJob = launch {
                            systemRecorder!!.start().collect { mixer.submitSystem(it) }
                        }
                    }
                    launch { mixer.mixed().collect { write(it) } }
                }
            }
        }
    }

    /** The one exit of every mode: what is encoded is what the equalizer shows. */
    private fun write(samples: ShortArray) {
        encoder.addSamples(samples)
        onLevel(levelOf(samples))
    }

    /**
     * BOTH lost its projection mid-recording: stop only the system side and
     * keep recording mic. The mixer zero-fills from here on, so the mic
     * timeline continues uninterrupted.
     */
    fun dropSystem() {
        if (droppedSystem) return
        droppedSystem = true
        systemJob?.cancel()
        runCatching { systemRecorder?.stop() }
    }

    /**
     * Stop tail: cancel the capture job, stop the recorders, wait for the job,
     * finalize the encoder. Recorders stop BEFORE the join — their blocking
     * read() must be broken or the join can hang. stop() races release() in the
     * flow's finally, hence runCatching.
     */
    suspend fun stop() {
        job?.cancel()
        runCatching { micRecorder?.stop() }
        runCatching { systemRecorder?.stop() }
        runCatching { activeProjection?.stop() }
        activeProjection = null
        job?.join()
        encoder.finish()
    }
}

private const val LEVEL_FLOOR_DB = -50.0
private const val LEVEL_RANGE_DB = 40.0

/** Chunk loudness as 0..1 on a dB scale: room noise sits near 0, loud speech near 1. */
private fun levelOf(samples: ShortArray): Float {
    if (samples.isEmpty()) return 0f
    var sum = 0.0
    for (sample in samples) {
        sum += sample.toDouble() * sample
    }
    val rms = sqrt(sum / samples.size) / Short.MAX_VALUE
    if (rms <= 0.0) return 0f
    return ((20 * log10(rms) - LEVEL_FLOOR_DB) / LEVEL_RANGE_DB).coerceIn(0.0, 1.0).toFloat()
}
