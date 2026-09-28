package com.lore.voicenotes.audio

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File
import java.nio.ByteBuffer

/**
 * Encodes PCM 16-bit mono samples to M4A (AAC-LC) via MediaCodec + MediaMuxer.
 * Call [addSamples] from the recording loop, then [finish] to finalize the file.
 */
class AudioEncoder(private val outputFile: File) {

    private val sampleRate = MicRecorder.SAMPLE_RATE
    private val bitRate = 128_000

    private val codec: MediaCodec
    private val muxer: MediaMuxer
    private var trackIndex = -1
    private var muxerStarted = false
    private var presentationTimeUs = 0L

    private val bufferInfo = MediaCodec.BufferInfo()

    init {
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, sampleRate, 1).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, bitRate)
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
        }
        codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        codec.start()

        muxer = MediaMuxer(outputFile.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
    }

    fun addSamples(pcm: ShortArray) {
        // ARCH: Native byte order is critical — MediaCodec expects little-endian PCM.
        val bytes = ByteBuffer.allocateDirect(pcm.size * 2).order(java.nio.ByteOrder.nativeOrder())
        bytes.asShortBuffer().put(pcm)
        bytes.limit(pcm.size * 2)

        var samplesQueued = 0
        // Feed all data — loop until the entire chunk is consumed
        while (bytes.hasRemaining()) {
            val inputIndex = codec.dequeueInputBuffer(10_000)
            if (inputIndex < 0) {
                // Codec busy — drain outputs and retry
                drainEncoder(false)
                continue
            }
            val inputBuffer = codec.getInputBuffer(inputIndex) ?: break
            inputBuffer.clear()
            val toCopy = minOf(bytes.remaining(), inputBuffer.remaining())
            val slice = ByteArray(toCopy)
            bytes.get(slice)
            inputBuffer.put(slice)
            val samplesInChunk = toCopy / 2
            codec.queueInputBuffer(inputIndex, 0, toCopy, presentationTimeUs, 0)
            presentationTimeUs += (samplesInChunk.toLong() * 1_000_000L) / sampleRate
            samplesQueued += samplesInChunk
            drainEncoder(false)
        }
    }

    fun finish() {
        // Signal end of stream
        val inputIndex = codec.dequeueInputBuffer(10_000)
        if (inputIndex >= 0) {
            codec.queueInputBuffer(inputIndex, 0, 0, presentationTimeUs, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
        }
        drainEncoder(true)
        codec.stop()
        codec.release()
        if (muxerStarted) {
            muxer.stop()
        }
        muxer.release()
    }

    private fun drainEncoder(endOfStream: Boolean) {
        while (true) {
            val outputIndex = codec.dequeueOutputBuffer(bufferInfo, if (endOfStream) 10_000 else 0)
            when {
                outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    trackIndex = muxer.addTrack(codec.outputFormat)
                    muxer.start()
                    muxerStarted = true
                }
                outputIndex >= 0 -> {
                    val outputBuffer = codec.getOutputBuffer(outputIndex) ?: continue
                    if (bufferInfo.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                        bufferInfo.size = 0
                    }
                    if (bufferInfo.size > 0 && muxerStarted) {
                        outputBuffer.position(bufferInfo.offset)
                        outputBuffer.limit(bufferInfo.offset + bufferInfo.size)
                        muxer.writeSampleData(trackIndex, outputBuffer, bufferInfo)
                    }
                    codec.releaseOutputBuffer(outputIndex, false)
                    if (bufferInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
                }
                else -> break
            }
        }
    }
}
