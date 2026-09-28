package com.lore.voicenotes.ui.record

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.ContentResolver
import android.net.Uri
import android.media.projection.MediaProjectionManager
import android.util.Log
import com.lore.voicenotes.BuildConfig
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize

import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.UploadFile
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lore.voicenotes.audio.AudioCaptureService
import com.lore.voicenotes.ui.theme.LoreRecording

@Composable
fun RecordScreen(viewModel: RecordViewModel = hiltViewModel()) {
    val context = LocalContext.current
    val config by viewModel.config.collectAsStateWithLifecycle()
    val elapsedSeconds by viewModel.elapsedSeconds.collectAsStateWithLifecycle()
    val uploading by viewModel.uploading.collectAsStateWithLifecycle()
    val error by viewModel.error.collectAsStateWithLifecycle()

    val isRecording by AudioCaptureService.isRecording.collectAsStateWithLifecycle()
    var includeSystemAudio by remember { mutableStateOf(false) }

    // MediaProjection launcher
    val projectionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            val intent = Intent(context, AudioCaptureService::class.java).apply {
                action = AudioCaptureService.ACTION_START_WITH_PROJECTION
                putExtra(AudioCaptureService.EXTRA_RESULT_CODE, result.resultCode)
                putExtra(AudioCaptureService.EXTRA_RESULT_DATA, result.data)
            }
            context.startForegroundService(intent)
            viewModel.onRecordingStarted()
        } else {
            // Denied — fall back to mic only
            startMicOnly(context)
            viewModel.onRecordingStarted()
        }
    }

    // Permission launcher — all start-recording logic lives here
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        if (!isGranted) {
            viewModel.setError("Microphone permission required")
            return@rememberLauncherForActivityResult
        }
        if (includeSystemAudio) {
            val mpm = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            projectionLauncher.launch(mpm.createScreenCaptureIntent())
        } else {
            startMicOnly(context)
            viewModel.onRecordingStarted()
        }
    }

    // File picker launcher
    val filePickerLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        val cr = context.contentResolver
        val filename = cr.query(uri, null, null, null, null)?.use { cursor ->
            val nameIdx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (cursor.moveToFirst() && nameIdx >= 0) cursor.getString(nameIdx) else null
        } ?: "upload.m4a"
        val mime = resolveMime(cr, uri, filename)
        val bytes = cr.openInputStream(uri)?.readBytes() ?: return@rememberLauncherForActivityResult
        if (BuildConfig.DEBUG) Log.i("Upload", "file=$filename mime=$mime size=${bytes.size}")
        viewModel.uploadFile(bytes, filename, mime)
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp),
    ) {
        if (config == null) {
            Text(
                "Configure API key in Settings first",
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier.align(Alignment.Center),
            )
            return@Box
        }

        // Main content — centered
        Column(
            modifier = Modifier.align(Alignment.Center),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            // Timer — only during recording
            if (isRecording) {
                Text(
                    text = formatTime(elapsedSeconds),
                    style = MaterialTheme.typography.displayLarge.copy(fontSize = 48.sp),
                    color = LoreRecording,
                )
                Spacer(Modifier.height(32.dp))
            }

            // Audio source chips — always visible
            Row(verticalAlignment = Alignment.CenterVertically) {
                FilterChip(
                    selected = true,
                    onClick = { },
                    label = { Text("Mic") },
                    enabled = !isRecording,
                )
                Spacer(Modifier.width(8.dp))
                FilterChip(
                    selected = includeSystemAudio,
                    onClick = { if (!isRecording) includeSystemAudio = !includeSystemAudio },
                    label = { Text("System Audio") },
                    enabled = !isRecording,
                )
            }

            Spacer(Modifier.height(24.dp))

            // Cancel (left) + Record/Stop (center). Cancel discards without saving/uploading.
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (isRecording) {
                    FilledIconButton(
                        onClick = {
                            val intent = Intent(context, AudioCaptureService::class.java).apply {
                                action = AudioCaptureService.ACTION_CANCEL
                            }
                            context.startService(intent)
                            viewModel.onRecordingStopped()
                        },
                        modifier = Modifier.size(56.dp),
                        colors = IconButtonDefaults.filledIconButtonColors(
                            containerColor = MaterialTheme.colorScheme.surfaceVariant,
                        ),
                    ) {
                        Icon(
                            Icons.Default.Close,
                            contentDescription = "Cancel",
                            modifier = Modifier.size(28.dp),
                        )
                    }
                    Spacer(Modifier.width(24.dp))
                }

                Button(
                    onClick = {
                        if (isRecording) {
                            val intent = Intent(context, AudioCaptureService::class.java).apply {
                                action = AudioCaptureService.ACTION_STOP
                            }
                            context.startService(intent)
                            viewModel.onRecordingStopped()
                        } else {
                            permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                        }
                    },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = if (isRecording) LoreRecording else MaterialTheme.colorScheme.primary
                    ),
                    modifier = Modifier.size(80.dp),
                    enabled = !uploading,
                ) {
                    Icon(
                        if (isRecording) Icons.Default.Stop else Icons.Default.Mic,
                        contentDescription = if (isRecording) "Stop" else "Record",
                        modifier = Modifier.size(36.dp),
                    )
                }
            }

            // Status
            if (uploading) {
                Spacer(Modifier.height(16.dp))
                CircularProgressIndicator(modifier = Modifier.size(24.dp))
                Text("Uploading...", style = MaterialTheme.typography.bodySmall)
            }

            if (error != null) {
                Spacer(Modifier.height(16.dp))
                Text(error!!, color = MaterialTheme.colorScheme.error)
            }
        }

        // Upload file — bottom, above tab bar. Hidden during recording.
        if (!isRecording && !uploading) {
            Row(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(bottom = 16.dp)
                    .clickable { filePickerLauncher.launch(arrayOf("audio/*")) },
                horizontalArrangement = Arrangement.Center,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(
                    Icons.Default.UploadFile,
                    contentDescription = "Upload file",
                    modifier = Modifier.size(24.dp),
                    tint = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    "Upload audio file",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

private fun startMicOnly(context: Context) {
    val intent = Intent(context, AudioCaptureService::class.java).apply {
        action = AudioCaptureService.ACTION_START_MIC
    }
    context.startForegroundService(intent)
}

private val ALLOWED_AUDIO_MIMES = setOf(
    "audio/webm", "audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3",
    "audio/wav", "audio/x-wav", "audio/mp4", "audio/m4a", "audio/x-m4a",
    "audio/aac", "audio/3gpp", "audio/3gpp2",
)

private fun resolveMime(cr: ContentResolver, uri: Uri, filename: String): String {
    // WHY: extension is authoritative for known audio containers. Some Android providers
    // misreport .m4a/.3gp files as audio/mpeg, which mismatches server magic-byte validation
    // (ftyp container vs MP3 sync) and produces 400 on /api/widget/upload.
    val byExtension = guessMimeFromExtension(filename)
    if (byExtension != null) return byExtension
    val reported = cr.getType(uri)
    if (reported != null && reported in ALLOWED_AUDIO_MIMES) return reported
    return "application/octet-stream"
}

private fun guessMimeFromExtension(filename: String): String? = when (
    filename.substringAfterLast('.', "").lowercase()
) {
    "m4a", "mp4" -> "audio/mp4"
    "mp3" -> "audio/mpeg"
    "wav" -> "audio/wav"
    "ogg", "opus" -> "audio/ogg"
    "webm" -> "audio/webm"
    "aac" -> "audio/aac"
    "3gp", "3gpp" -> "audio/3gpp"
    else -> null
}

private fun formatTime(seconds: Long): String {
    val m = seconds / 60
    val s = seconds % 60
    return "%02d:%02d".format(m, s)
}
