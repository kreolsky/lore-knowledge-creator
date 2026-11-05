package com.lore.voicenotes.audio

import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

/**
 * Mixes mic and system PCM sample-by-sample with clamping.
 *
 * ARCH: mic is the master clock — exactly one output chunk per mic chunk, so
 * the encoded duration tracks the mic timeline however the system side
 * behaves. The old mixer emitted once per arriving chunk from either side,
 * so with both sources on the output carried about twice the samples and the
 * file stretched (PTS counts samples). System samples drain from a bounded
 * FIFO (2 s, oldest dropped) and zero-fill when it runs short; the system
 * side is never awaited, because it may be silent for the whole take.
 * Thread-safe: the two recorders submit from different IO threads. send, not
 * trySend — a silently dropped chunk would cut the timeline short.
 */
class AudioMixer {

    companion object {
        /** 2 s of mono 16-bit PCM at 44.1 kHz. */
        const val FIFO_SAMPLES = MicRecorder.SAMPLE_RATE * 2
        /** Headroom for one recorder chunk beyond the 2 s bound. */
        const val FIFO_CAPACITY = FIFO_SAMPLES + 8192
    }

    /** Ring buffer of system samples; guarded by [fifo] itself. */
    private val fifo = ShortRingBuffer(FIFO_CAPACITY)

    private val outputChannel = Channel<ShortArray>(Channel.BUFFERED)

    fun submitSystem(samples: ShortArray) {
        synchronized(fifo) { fifo.write(samples) }
    }

    suspend fun submitMic(samples: ShortArray) {
        val mixed = synchronized(fifo) {
            val out = ShortArray(samples.size)
            fifo.readInto(out)
            for (i in samples.indices) {
                val sum = samples[i] + out[i]
                out[i] = sum.coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt()).toShort()
            }
            out
        }
        outputChannel.send(mixed)
    }

    fun mixed(): Flow<ShortArray> = flow {
        for (chunk in outputChannel) {
            emit(chunk)
        }
    }

    /** Bounded FIFO of 16-bit samples; writes past capacity drop the oldest. */
    private class ShortRingBuffer(private val capacity: Int) {
        private val data = ShortArray(capacity)
        private var head = 0
        private var count = 0

        fun write(samples: ShortArray) {
            for (s in samples) {
                if (count == capacity) {
                    // Full: drop the oldest sample to keep the newest 2 s.
                    head = (head + 1) % capacity
                    count--
                }
                data[(head + count) % capacity] = s
                count++
            }
        }

        /** Copy up to out.size samples from the head; unwritten tail stays zero. */
        fun readInto(out: ShortArray) {
            val n = minOf(count, out.size)
            for (i in 0 until n) {
                out[i] = data[(head + i) % capacity]
            }
            head = (head + n) % capacity
            count -= n
        }
    }
}
