package com.lore.voicenotes.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.isActive
import kotlin.coroutines.coroutineContext

/** Microphone PCM capture — 44.1kHz mono 16-bit. */
class MicRecorder {

    companion object {
        const val SAMPLE_RATE = 44100
        const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
    }

    private var recorder: AudioRecord? = null

    val bufferSize: Int =
        AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING).coerceAtLeast(4096)

    @SuppressLint("MissingPermission")
    fun start(): Flow<ShortArray> = flow {
        val ar = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            SAMPLE_RATE, CHANNEL, ENCODING, bufferSize
        )
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
