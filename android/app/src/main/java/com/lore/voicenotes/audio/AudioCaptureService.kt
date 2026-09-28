package com.lore.voicenotes.audio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.IBinder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Foreground service for audio capture. Supports mic-only or mic+system audio.
 *
 * Start with ACTION_START_MIC or ACTION_START_WITH_PROJECTION.
 * Stop with ACTION_STOP (finalizes the file → triggers upload).
 * Cancel with ACTION_CANCEL (discards the file → no upload).
 */
class AudioCaptureService : Service() {

    companion object {
        const val ACTION_START_MIC = "com.lore.voicenotes.START_MIC"
        const val ACTION_START_WITH_PROJECTION = "com.lore.voicenotes.START_PROJECTION"
        const val ACTION_STOP = "com.lore.voicenotes.STOP"
        const val ACTION_CANCEL = "com.lore.voicenotes.CANCEL"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"

        const val CHANNEL_ID = "lore_recording"
        const val NOTIFICATION_ID = 1

        private val _lastOutputFile = MutableStateFlow<File?>(null)
        val lastOutputFile: StateFlow<File?> = _lastOutputFile.asStateFlow()

        private val _isRecording = MutableStateFlow(false)
        val isRecording: StateFlow<Boolean> = _isRecording.asStateFlow()
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var micRecorder: MicRecorder? = null
    private var systemRecorder: SystemAudioRecorder? = null
    private var encoder: AudioEncoder? = null
    private var recordJob: Job? = null
    private var mediaProjection: MediaProjection? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START_MIC -> startRecording(projectionResultCode = null, projectionData = null)
            ACTION_START_WITH_PROJECTION -> {
                val code = intent.getIntExtra(EXTRA_RESULT_CODE, -1)
                val data = intent.getParcelableExtra<Intent>(EXTRA_RESULT_DATA)
                startRecording(projectionResultCode = code, projectionData = data)
            }
            ACTION_STOP -> stopRecording()
            ACTION_CANCEL -> stopRecording(discard = true)
        }
        return START_NOT_STICKY
    }

    private fun startRecording(projectionResultCode: Int?, projectionData: Intent?) {
        val notification = buildNotification()

        val foregroundTypes = if (projectionResultCode != null && projectionData != null) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        } else {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        }
        startForeground(NOTIFICATION_ID, notification, foregroundTypes)

        val outputDir = File(filesDir, "recordings").apply { mkdirs() }
        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val outputFile = File(outputDir, "recording_$timestamp.m4a")
        _lastOutputFile.value = outputFile

        val enc = AudioEncoder(outputFile)
        encoder = enc

        val mic = MicRecorder()
        micRecorder = mic

        // If we have MediaProjection, set up system audio too
        if (projectionResultCode != null && projectionData != null) {
            val mpm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val projection = mpm.getMediaProjection(projectionResultCode, projectionData)
            mediaProjection = projection
            val sysRec = SystemAudioRecorder(projection)
            systemRecorder = sysRec

            recordJob = scope.launch {
                val mixer = AudioMixer()
                // Collect both streams and mix
                launch {
                    mic.start().collect { samples -> mixer.submitMic(samples) }
                }
                launch {
                    sysRec.start().collect { samples -> mixer.submitSystem(samples) }
                }
                launch {
                    mixer.mixed().collect { mixed -> enc.addSamples(mixed) }
                }
            }
        } else {
            recordJob = scope.launch {
                mic.start().collect { samples -> enc.addSamples(samples) }
            }
        }

        _isRecording.value = true
    }

    private fun stopRecording(discard: Boolean = false) {
        recordJob?.cancel()
        recordJob = null
        micRecorder?.stop()
        micRecorder = null
        systemRecorder?.stop()
        systemRecorder = null
        mediaProjection?.stop()
        mediaProjection = null
        encoder?.finish()
        encoder = null
        if (discard) {
            // Cancel: delete the file and clear the output so the isRecording false
            // transition does not trigger an upload in RecordViewModel.
            _lastOutputFile.value?.let { runCatching { it.delete() } }
            _lastOutputFile.value = null
        }
        // Signal after encoder finalized — file has valid moov atom now
        _isRecording.value = false
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        stopRecording()
        scope.cancel()
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "Recording", NotificationManager.IMPORTANCE_LOW
        )
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Lore Voice")
            .setContentText("Recording...")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()
}
