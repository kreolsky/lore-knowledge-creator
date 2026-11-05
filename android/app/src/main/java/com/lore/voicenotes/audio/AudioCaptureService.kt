package com.lore.voicenotes.audio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import com.lore.voicenotes.R
import com.lore.voicenotes.data.repository.TranscriptionRepository
import com.lore.voicenotes.di.ApplicationScope
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import javax.inject.Inject

/**
 * Foreground service for audio capture: mic, system or both.
 *
 * Start with ACTION_START; EXTRA_SOURCE_MIC / EXTRA_SOURCE_SYSTEM pick the
 * mode (at least one), EXTRA_RESULT_CODE + EXTRA_RESULT_DATA carry the
 * projection grant when system audio is on, EXTRA_KEY_ID carries the key the
 * recording belongs to (absent = zero keys on the device). Stop with
 * ACTION_STOP (finalizes the file → row + upload enqueue). Cancel with
 * ACTION_CANCEL (discards the file → no row).
 *
 * ARCH: stop is two parts. The synchronous part (main thread) flips isRecording
 * and drops the session reference, so the next start cannot collide with the
 * one being stopped. The tail runs on the injected application scope and
 * survives service teardown: it finalizes the encoder and persists the row.
 * A StateFlow watched true→false used to conflate a fast stop+start, so the
 * first recording was never uploaded; per-session tails close that hole.
 */
@AndroidEntryPoint
class AudioCaptureService : Service() {

    @Inject lateinit var repository: TranscriptionRepository
    @Inject @ApplicationScope lateinit var applicationScope: CoroutineScope

    sealed interface Event {
        /** Recording persisted and its upload enqueued; durationMs from the encoder. */
        data class Saved(val durationMs: Long) : Event
        data object Canceled : Event
        /** Missing or empty output: deleted, no row written, nothing to show. */
        data object Empty : Event
        /** Persisting the row failed (local storage): no row exists, nothing enqueued. */
        data object SaveFailed : Event
    }

    private var session: RecordingSession? = null
    private var runningTails = 0
    private var lastStartId = -1
    private val mainHandler = Handler(Looper.getMainLooper())

    /**
     * Registered before capture starts (an Android 14+ requirement) and on the
     * main looper: system-only ends the take as a normal save, both falls back
     * to mic only. Our own stop unregisters first, so this fires only for a
     * projection the system or the user killed.
     */
    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() {
            runCatching { activeProjection?.unregisterCallback(this) }
            val current = session ?: return
            when (current.mode) {
                CaptureMode.SYSTEM -> stopRecording(discard = false)
                CaptureMode.BOTH -> {
                    current.dropSystem()
                    // State, not an event: a StateFlow carries the degradation
                    // to a screen that was out of composition when it happened.
                    _micOnly.value = true
                }
                CaptureMode.MIC -> Unit // no projection involved, nothing to do
            }
        }
    }

    /** The service owns callback registration; the session owns the projection. */
    private var activeProjection: MediaProjection? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        lastStartId = startId
        when (intent?.action) {
            ACTION_START -> startRecording(intent)
            ACTION_STOP -> stopRecording(discard = false)
            ACTION_CANCEL -> stopRecording(discard = true)
        }
        return START_NOT_STICKY
    }

    private fun startRecording(intent: Intent) {
        val mic = intent.getBooleanExtra(EXTRA_SOURCE_MIC, false)
        val system = intent.getBooleanExtra(EXTRA_SOURCE_SYSTEM, false)
        val projectionGranted = intent.hasExtra(EXTRA_RESULT_CODE) && intent.hasExtra(EXTRA_RESULT_DATA)
        // Defensive: system without a grant degrades to mic, or to nothing.
        val mode = when {
            mic && system && projectionGranted -> CaptureMode.BOTH
            mic && system -> CaptureMode.MIC.also { Log.w(TAG, "system requested without a grant, mic only") }
            mic -> CaptureMode.MIC
            system && projectionGranted -> CaptureMode.SYSTEM
            else -> {
                Log.w(TAG, "start without a source, ignored")
                stopSelf(lastStartId)
                return
            }
        }

        val notification = buildNotification()
        // Every startForegroundService call must meet its startForeground obligation,
        // including a double start that keeps the first recording running.
        // The types follow the mode: mic needs MICROPHONE, system needs
        // MEDIA_PROJECTION; RECORD_AUDIO is required by both, including capture.
        val foregroundTypes = when (mode) {
            CaptureMode.MIC -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            CaptureMode.SYSTEM -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            CaptureMode.BOTH ->
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        }
        startForeground(NOTIFICATION_ID, notification, foregroundTypes)
        // Double start under a second: keep the first recording, ignore the rest.
        if (session != null) return

        val keyId = if (intent.hasExtra(EXTRA_KEY_ID)) {
            intent.getLongExtra(EXTRA_KEY_ID, Long.MIN_VALUE)
        } else null

        val outputDir = File(filesDir, "recordings").apply { mkdirs() }
        // WHY milliseconds: second precision collides on a fast stop+start.
        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(Date())
        val outputFile = File(outputDir, "recording_$timestamp.m4a")

        val projection = if (mode != CaptureMode.MIC) {
            val mpm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            mpm.getMediaProjection(
                intent.getIntExtra(EXTRA_RESULT_CODE, -1),
                intent.getParcelableExtra(EXTRA_RESULT_DATA)!!,
            ).also {
                // Before capture: a callback must be registered from Android 14 on,
                // and ours decides what a killed projection means for the take.
                activeProjection = it
                it.registerCallback(projectionCallback, mainHandler)
            }
        } else null

        val newSession = RecordingSession(
            outputFile, keyId, mode, projection, applicationScope,
            onLevel = { _level.value = it },
        )
        session = newSession
        _micOnly.value = false
        _level.value = 0f
        newSession.start()
        _isRecording.value = true
    }

    private fun stopRecording(discard: Boolean) {
        val current = session ?: return // a second stop is a no-op
        // Our own teardown: the projection callback must not read this stop as
        // a mid-recording drop.
        runCatching { activeProjection?.unregisterCallback(projectionCallback) }
        activeProjection = null
        session = null
        _isRecording.value = false
        startTail(current, discard)
    }

    private fun startTail(stopped: RecordingSession, discard: Boolean) {
        runningTails++
        applicationScope.launch {
            try {
                stopped.stop()
                if (discard) {
                    runCatching { stopped.file.delete() }
                    _events.emit(Event.Canceled)
                } else if (!stopped.file.exists() || stopped.file.length() == 0L) {
                    runCatching { stopped.file.delete() }
                    _events.emit(Event.Empty)
                } else {
                    val durationMs = stopped.durationMs
                    try {
                        repository.save(
                            stopped.file, stopped.file.name, "audio/mp4", stopped.keyId, durationMs,
                        )
                        _events.emit(Event.Saved(durationMs))
                    } catch (e: Exception) {
                        // save() is a local insert plus a WorkManager enqueue;
                        // failing here means no row exists at all. No silent
                        // degradation: the screen says «Не сохранено».
                        Log.w(TAG, "recording save failed: ${e.message}")
                        _events.emit(Event.SaveFailed)
                    }
                }
            } finally {
                withContext(Dispatchers.Main.immediate) {
                    runningTails--
                    maybeTearDown()
                }
            }
        }
    }

    /**
     * ARCH: foreground teardown runs only with no active session and no running
     * tail — an old tail must never tear down a new recording's foreground
     * service. stopSelf(lastStartId) keeps a start that raced the tail alive.
     */
    private fun maybeTearDown() {
        if (session == null && runningTails == 0) {
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf(lastStartId)
        }
    }

    override fun onDestroy() {
        // Killed while recording: run the same tail so the audio is not lost.
        val current = session
        if (current != null) {
            runCatching { activeProjection?.unregisterCallback(projectionCallback) }
            activeProjection = null
            session = null
            _isRecording.value = false
            startTail(current, discard = false)
        }
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, getString(R.string.notification_recording_channel), NotificationManager.IMPORTANCE_LOW
        )
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_recording))
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()

    companion object {
        private const val TAG = "AudioCaptureService"
        const val ACTION_START = "com.lore.voicenotes.START"
        const val ACTION_STOP = "com.lore.voicenotes.STOP"
        const val ACTION_CANCEL = "com.lore.voicenotes.CANCEL"
        const val EXTRA_SOURCE_MIC = "source_mic"
        const val EXTRA_SOURCE_SYSTEM = "source_system"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        const val EXTRA_KEY_ID = "key_id"

        const val CHANNEL_ID = "lore_recording"
        const val NOTIFICATION_ID = 1

        private val _events = MutableSharedFlow<Event>(extraBufferCapacity = 16)
        val events: SharedFlow<Event> = _events.asSharedFlow()

        private val _isRecording = MutableStateFlow(false)
        val isRecording: StateFlow<Boolean> = _isRecording.asStateFlow()

        /** This take lost its system side (projection stopped): mic continues. */
        private val _micOnly = MutableStateFlow(false)
        val micOnly: StateFlow<Boolean> = _micOnly.asStateFlow()

        /** Loudness of the audio being encoded right now, 0..1. */
        private val _level = MutableStateFlow(0f)
        val level: StateFlow<Float> = _level.asStateFlow()
    }
}
