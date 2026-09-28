package com.lore.voicenotes.audio

import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

/**
 * Mixes mic and system audio PCM streams sample-by-sample with clamping.
 * Each source submits chunks independently; mixer outputs when either arrives,
 * mixing with the latest available chunk from the other source.
 */
class AudioMixer {

    private val outputChannel = Channel<ShortArray>(Channel.BUFFERED)

    private var lastMic: ShortArray? = null
    private var lastSys: ShortArray? = null

    fun submitMic(samples: ShortArray) {
        lastMic = samples
        mix()
    }

    fun submitSystem(samples: ShortArray) {
        lastSys = samples
        mix()
    }

    private fun mix() {
        val mic = lastMic ?: return
        val sys = lastSys

        if (sys == null) {
            outputChannel.trySend(mic)
            return
        }

        val len = maxOf(mic.size, sys.size)
        val result = ShortArray(len)
        for (i in 0 until len) {
            val m = if (i < mic.size) mic[i].toInt() else 0
            val s = if (i < sys.size) sys[i].toInt() else 0
            result[i] = (m + s).coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt()).toShort()
        }
        outputChannel.trySend(result)
    }

    fun mixed(): Flow<ShortArray> = flow {
        for (chunk in outputChannel) {
            emit(chunk)
        }
    }
}
