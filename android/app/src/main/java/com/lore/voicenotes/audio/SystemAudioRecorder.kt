package com.lore.voicenotes.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.projection.MediaProjection
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.isActive
import kotlin.coroutines.coroutineContext

/** System audio capture via AudioPlaybackCapture (requires MediaProjection). Min API 29. */
class SystemAudioRecorder(private val projection: MediaProjection) {

    private var recorder: AudioRecord? = null

    private val bufferSize: Int =
        AudioRecord.getMinBufferSize(
            MicRecorder.SAMPLE_RATE, MicRecorder.CHANNEL, MicRecorder.ENCODING
        ).coerceAtLeast(4096)

    @SuppressLint("MissingPermission")
    fun start(): Flow<ShortArray> = flow {
        val config = AudioPlaybackCaptureConfiguration.Builder(projection)
            .addMatchingUsage(android.media.AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(android.media.AudioAttributes.USAGE_GAME)
            .addMatchingUsage(android.media.AudioAttributes.USAGE_UNKNOWN)
            .build()

        val ar = AudioRecord.Builder()
            .setAudioPlaybackCaptureConfig(config)
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(MicRecorder.ENCODING)
                    .setSampleRate(MicRecorder.SAMPLE_RATE)
                    .setChannelMask(MicRecorder.CHANNEL)
                    .build()
            )
            .setBufferSizeInBytes(bufferSize)
            .build()

        recorder = ar
        ar.startRecording()
        try {
            val buffer = ShortArray(bufferSize / 2)
            while (coroutineContext.isActive) {
                val read = ar.read(buffer, 0, buffer.size)
                if (read > 0) emit(buffer.copyOf(read))
            }
        } finally {
            ar.stop()
            ar.release()
            recorder = null
        }
    }.flowOn(Dispatchers.IO)

    fun stop() {
        recorder?.stop()
    }
}
