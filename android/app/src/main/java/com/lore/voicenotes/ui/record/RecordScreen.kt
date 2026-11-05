package com.lore.voicenotes.ui.record

import android.Manifest
import android.app.Activity
import android.content.ClipboardManager
import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.AnimationVector1D
import androidx.compose.animation.core.EaseOut
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.keyframes
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.VolumeUp
import androidx.compose.material.icons.outlined.Close
import androidx.compose.material.icons.outlined.ContentPaste
import androidx.compose.material.icons.outlined.Mic
import androidx.compose.material.icons.outlined.Upload
import androidx.compose.material.icons.outlined.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.LineHeightStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lore.voicenotes.R
import com.lore.voicenotes.audio.AudioCaptureService
import com.lore.voicenotes.data.local.DocumentKeyEntity
import com.lore.voicenotes.data.repository.AddKeyResult
import com.lore.voicenotes.ui.theme.LoreAccent
import com.lore.voicenotes.ui.theme.LoreAmber
import com.lore.voicenotes.ui.theme.LoreBg
import com.lore.voicenotes.ui.theme.LoreBorder
import com.lore.voicenotes.ui.theme.LoreDim
import com.lore.voicenotes.ui.theme.LoreGreen
import com.lore.voicenotes.ui.theme.LoreMuted
import com.lore.voicenotes.ui.theme.LoreRed
import com.lore.voicenotes.ui.theme.LoreStrong
import com.lore.voicenotes.ui.theme.LoreSurface
import com.lore.voicenotes.ui.theme.LoreSurface2
import com.lore.voicenotes.ui.theme.LoreSurface3
import com.lore.voicenotes.ui.theme.LoreText
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/** What the timer slot shows right after an action; 2 s, then it clears. */
private enum class FlashKind { Saved, Canceled, Notice }

/** After a normal stop the whole dial is green; the ring goes first, then the button. */
private enum class SaveConfirm { None, Ring, Button }

private data class RecordFlash(
    val kind: FlashKind,
    /** Bumped per flash so two equal flashes restart the 2 s timer. */
    val token: Long,
    val seconds: Long = 0L,
    val text: String = "",
)

private const val HOLD_MS = 470
private const val FLASH_MS = 2000L
private const val PEEK_MS = 2000L
private const val SAVE_RING_MS = 1400L
private const val SAVE_BUTTON_TAIL_MS = 300L
private const val EQ_FADE_MS = 700
private const val EQ_BARS = 60
private const val EQ_STEP_MS = 70
private const val EQ_TAIL_BARS = 8f

@Composable
fun RecordScreen(
    onOpenHistory: () -> Unit,
    viewModel: RecordViewModel = hiltViewModel(),
) {
    val context = LocalContext.current
    val elapsedSeconds by viewModel.elapsedSeconds.collectAsStateWithLifecycle()
    val keys by viewModel.liveKeys.collectAsStateWithLifecycle()
    val selectedKeyId by viewModel.selectedKeyId.collectAsStateWithLifecycle()
    val failedCount by viewModel.uploadFailedCount.collectAsStateWithLifecycle()
    val addField by viewModel.addField.collectAsStateWithLifecycle()
    val addErrorRes by viewModel.addErrorRes.collectAsStateWithLifecycle()
    val adding by viewModel.adding.collectAsStateWithLifecycle()

    val isRecording by AudioCaptureService.isRecording.collectAsStateWithLifecycle()
    // Degradation is read as state, not collected as an event: it must show
    // even when it happened while this screen was out of composition.
    val serviceMicOnly by AudioCaptureService.micOnly.collectAsStateWithLifecycle()

    var flash by remember { mutableStateOf<RecordFlash?>(null) }
    var flashToken by remember { mutableLongStateOf(0L) }
    // Refused projection consent with mic on: this take is mic-only by choice
    // of the launcher path, which the service cannot know.
    var consentMicOnly by remember { mutableStateOf(false) }
    val micOnlyNow = consentMicOnly || serviceMicOnly

    // Sources are independent toggles; at least one stays on (a tap that would
    // turn the last one off is refused with a nudge, never a sentence) and
    // both lock during a recording. RECORD_AUDIO is requested in every mode:
    // playback capture needs it too.
    var micOn by remember { mutableStateOf(true) }
    var systemOn by remember { mutableStateOf(false) }
    var micNudge by remember { mutableIntStateOf(0) }
    var sysNudge by remember { mutableIntStateOf(0) }

    // Doc tabs: tap selects and unrolls leftward with the title for 2 s;
    // a recording rolls it back and locks the tabs.
    var openTabId by remember { mutableStateOf<Long?>(null) }
    var showAddKey by remember { mutableStateOf(false) }

    var saveConfirm by remember { mutableStateOf(SaveConfirm.None) }
    var saveConfirmRun by remember { mutableIntStateOf(0) }

    fun clearFlash() {
        flash = null
    }

    fun startSaveConfirm() {
        saveConfirm = SaveConfirm.Ring
        saveConfirmRun++
    }

    fun clearSaveConfirm() {
        saveConfirm = SaveConfirm.None
        saveConfirmRun = 0
    }

    fun showFlash(kind: FlashKind, seconds: Long = 0L, text: String = "") {
        flashToken++
        flash = RecordFlash(kind, flashToken, seconds, text)
    }

    // After stop or cancel the slot holds the state for 2 s, then clears.
    // Starting the next recording or tapping a tab replaces it at once.
    LaunchedEffect(flash) {
        if (flash != null) {
            delay(FLASH_MS)
            flash = null
        }
    }

    LaunchedEffect(saveConfirmRun) {
        if (saveConfirmRun != 0) {
            delay(SAVE_RING_MS)
            saveConfirm = SaveConfirm.Button
            delay(SAVE_BUTTON_TAIL_MS)
            saveConfirm = SaveConfirm.None
        }
    }

    // The rolled-out tab rolls back after its 2 s.
    LaunchedEffect(openTabId) {
        if (openTabId != null) {
            delay(PEEK_MS)
            openTabId = null
        }
    }

    val notSavedText = stringResource(R.string.record_not_saved)
    val noAudioAccessText = stringResource(R.string.record_no_audio_access)
    val micPermissionText = stringResource(R.string.record_mic_permission_required)
    LaunchedEffect(Unit) {
        AudioCaptureService.events.collect { event ->
            when (event) {
                // Saved: the final time stays at full size, green, with the
                // word under it; the dial confirms. Canceled: the word only.
                // A stop tap already showed all that: the persist takes a moment,
                // and only a stop nobody tapped (projection ended) starts here.
                is AudioCaptureService.Event.Saved ->
                    if (flash?.kind != FlashKind.Saved) {
                        showFlash(FlashKind.Saved, seconds = event.durationMs / 1000)
                        if (!isRecording) startSaveConfirm()
                    }
                AudioCaptureService.Event.Canceled -> showFlash(FlashKind.Canceled)
                AudioCaptureService.Event.Empty -> {
                    clearFlash()
                    clearSaveConfirm()
                }
                AudioCaptureService.Event.SaveFailed -> {
                    clearSaveConfirm()
                    showFlash(FlashKind.Notice, text = notSavedText)
                }
            }
        }
    }
    LaunchedEffect(Unit) {
        viewModel.signals.collect { signal ->
            if (signal is RecordSignal.NotSaved) showFlash(FlashKind.Notice, text = notSavedText)
        }
    }

    fun onRecordingStarted() {
        clearFlash()
        clearSaveConfirm()
        consentMicOnly = false
        openTabId = null
        viewModel.onRecordingStarted()
    }

    // MediaProjection launcher — only reached with system audio on.
    val projectionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            startCapture(
                context, viewModel.selectedKeyId.value,
                mic = micOn, system = true,
                resultCode = result.resultCode, resultData = result.data,
            )
            onRecordingStarted()
        } else if (micOn) {
            // Refused consent: mic degrades to mic only, amber word in the slot.
            startCapture(context, viewModel.selectedKeyId.value, mic = true, system = false)
            onRecordingStarted()
            consentMicOnly = true
        } else {
            // System-only with no consent records nothing.
            showFlash(FlashKind.Notice, text = noAudioAccessText)
        }
    }

    // Permission launcher — all start-recording logic lives here
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        if (!isGranted) {
            showFlash(FlashKind.Notice, text = micPermissionText)
            return@rememberLauncherForActivityResult
        }
        if (systemOn) {
            val mpm = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            projectionLauncher.launch(mpm.createScreenCaptureIntent())
        } else {
            startCapture(context, viewModel.selectedKeyId.value, mic = true, system = false)
            onRecordingStarted()
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
        // The stream copy happens off the main thread in the view model.
        viewModel.uploadFile(uri, filename, mime)
    }

    Box(Modifier.fillMaxSize()) {
        // Stage: the dial with the record button and the equalizer.
        Box(
            Modifier
                .align(Alignment.BottomCenter)
                .padding(bottom = 88.dp)
                .size(230.dp)
        ) {
            EqRing(
                live = isRecording,
                confirming = saveConfirm == SaveConfirm.Ring,
                modifier = Modifier.matchParentSize(),
            )
            RecordButton(
                isRecording = isRecording,
                saved = saveConfirm != SaveConfirm.None,
                onClick = {
                    if (isRecording) {
                        val finalSeconds = elapsedSeconds
                        val intent = Intent(context, AudioCaptureService::class.java).apply {
                            action = AudioCaptureService.ACTION_STOP
                        }
                        context.startService(intent)
                        viewModel.onRecordingStopped()
                        showFlash(FlashKind.Saved, seconds = finalSeconds)
                        startSaveConfirm()
                    } else {
                        permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                    }
                },
                modifier = Modifier.align(Alignment.Center),
            )
        }

        // Cancel: only while recording, on the thumb's diagonal down-left of the
        // dial; offset from the dial, not the edge, so the diagonal holds on any
        // width. Fires on a 470 ms hold with the fill, a short tap answers with a nudge.
        if (isRecording) {
            CancelHoldButton(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(bottom = 101.dp)
                    .offset(x = (-100).dp),
                onHoldFired = {
                    val intent = Intent(context, AudioCaptureService::class.java).apply {
                        action = AudioCaptureService.ACTION_CANCEL
                    }
                    context.startService(intent)
                    viewModel.onRecordingStopped()
                },
            )
        }

        // Sources on the left edge, flush; off ones dim and strike out while live.
        Column(
            modifier = Modifier
                .align(Alignment.BottomStart)
                .padding(bottom = 217.dp),
            verticalArrangement = Arrangement.spacedBy(7.dp),
        ) {
            SourceButton(
                on = micOn,
                dimmed = isRecording && !micOn,
                icon = Icons.Outlined.Mic,
                contentDescription = stringResource(R.string.record_source_mic),
                nudgeCount = micNudge,
                enabled = !isRecording,
                onClick = {
                    if (micOn && !systemOn) micNudge++ else micOn = !micOn
                },
            )
            SourceButton(
                on = systemOn,
                dimmed = isRecording && !systemOn,
                icon = Icons.AutoMirrored.Outlined.VolumeUp,
                contentDescription = stringResource(R.string.record_source_system),
                nudgeCount = sysNudge,
                enabled = !isRecording,
                onClick = {
                    if (systemOn && !micOn) sysNudge++ else systemOn = !systemOn
                },
            )
        }

        // Document tabs on the right edge, under the timer line: number only,
        // title unrolls on tap.
        Column(
            modifier = Modifier
                .align(Alignment.TopEnd)
                .padding(top = 78.dp),
            horizontalAlignment = Alignment.End,
            verticalArrangement = Arrangement.spacedBy(7.dp),
        ) {
            keys.forEach { key ->
                DocTab(
                    key = key,
                    selected = key.id == selectedKeyId,
                    open = openTabId == key.id,
                    locked = isRecording,
                    onClick = {
                        clearFlash()
                        viewModel.selectKey(key.id)
                        openTabId = key.id
                    },
                )
            }
            DocTabAdd(
                locked = isRecording,
                onClick = {
                    clearFlash()
                    showAddKey = true
                },
            )
        }

        // The timer slot is the only text above the controls, top-left.
        // The fail line lives in the same slot and
        // yields to the timer and to the saved state.
        Column(
            modifier = Modifier
                .align(Alignment.TopStart)
                .padding(start = 15.dp, top = 10.dp)
        ) {
            when {
                isRecording -> {
                    Text(
                        text = formatTime(elapsedSeconds),
                        style = TimerStyle,
                        color = LoreRed,
                    )
                    if (micOnlyNow) {
                        Spacer(Modifier.height(6.dp))
                        Text(
                            stringResource(R.string.record_mic_only),
                            style = SlotWordStyle,
                            color = LoreAmber,
                        )
                    }
                }

                flash?.kind == FlashKind.Saved -> {
                    Text(
                        text = formatTime(flash?.seconds ?: 0L),
                        style = TimerStyle,
                        color = LoreGreen,
                    )
                    Spacer(Modifier.height(6.dp))
                    Text(
                        stringResource(R.string.record_saved),
                        style = SlotWordStyle,
                        color = LoreGreen,
                    )
                }

                flash?.kind == FlashKind.Canceled -> SlotWord(
                    stringResource(R.string.record_canceled),
                )

                flash?.kind == FlashKind.Notice -> SlotWord(flash?.text.orEmpty())

                failedCount > 0 -> FailLine(count = failedCount, onClick = onOpenHistory)
            }
        }

        // Upload row: dashed, above navigation, hidden (but holding its
        // space) while recording.
        Row(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(start = 15.dp, end = 15.dp, bottom = 9.dp)
                .fillMaxWidth()
                .height(43.dp)
                .graphicsLayer { alpha = if (isRecording) 0f else 1f }
                .drawBehind {
                    drawRoundRect(
                        color = LoreSurface3,
                        cornerRadius = CornerRadius(12.dp.toPx(), 12.dp.toPx()),
                        style = Stroke(
                            width = 1.dp.toPx(),
                            pathEffect = PathEffect.dashPathEffect(
                                floatArrayOf(6.dp.toPx(), 6.dp.toPx())
                            ),
                        ),
                    )
                }
                .clickable(enabled = !isRecording) {
                    filePickerLauncher.launch(arrayOf("audio/*"))
                },
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.Center,
        ) {
            Icon(
                Icons.Outlined.Upload,
                contentDescription = null,
                tint = LoreText,
                modifier = Modifier.size(16.dp),
            )
            Spacer(Modifier.width(8.dp))
            Text(
                stringResource(R.string.record_upload_file),
                fontSize = 11.sp,
                fontWeight = FontWeight.W800,
                color = LoreText,
            )
        }
    }

    if (showAddKey) {
        AddKeyDialog(
            field = addField,
            errorRes = addErrorRes,
            busy = adding,
            onFieldChange = viewModel::updateAddField,
            onPaste = {
                val clipboard =
                    context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                val clip = clipboard.primaryClip
                if (clip != null && clip.itemCount > 0) {
                    clip.getItemAt(0).text?.toString()?.let(viewModel::updateAddField)
                }
            },
            onAdd = {
                viewModel.addKey { result ->
                    showAddKey = false
                    // A token already live selects its tab — unroll it.
                    if (result is AddKeyResult.Selected) openTabId = result.key.id
                }
            },
            onDismiss = {
                showAddKey = false
                viewModel.dismissAddError()
            },
        )
    }
}

/** Amber/green word replacing the timer (cancel, notices). */
@Composable
private fun SlotWord(text: String) {
    Box(
        modifier = Modifier.height(56.dp),
        contentAlignment = Alignment.CenterStart,
    ) {
        Text(text, style = SlotWordStyle, color = LoreAmber)
    }
}

/** Persistent red line «Не отправлено: N»; leads to History. */
@Composable
private fun FailLine(count: Int, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .height(40.dp)
            .clickable(onClick = onClick),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            Icons.Outlined.Warning,
            contentDescription = stringResource(R.string.nav_history),
            tint = LoreRed,
            modifier = Modifier.size(16.dp),
        )
        Spacer(Modifier.width(7.dp))
        Text(
            stringResource(R.string.record_upload_failed_count, count),
            fontSize = 15.sp,
            fontWeight = FontWeight.W600,
            color = LoreRed,
        )
    }
}

private val TimerStyle = TextStyle(
    fontSize = 56.sp,
    lineHeight = 56.sp,
    fontWeight = FontWeight.Normal,
    letterSpacing = (-1.12).sp,
    fontFeatureSettings = "tnum",
    lineHeightStyle = LineHeightStyle(
        alignment = LineHeightStyle.Alignment.Center,
        trim = LineHeightStyle.Trim.Both,
    ),
)

private val SlotWordStyle = TextStyle(
    fontSize = 15.sp,
    lineHeight = 20.sp,
    fontWeight = FontWeight.Medium,
)

/**
 * Running equalizer: every [EQ_STEP_MS] a bar of real loudness enters at
 * 12 o'clock and travels clockwise, so one lap shows the last few seconds of
 * the take. The last bars of the lap shrink away, so the seam where the newest
 * bar enters does not jump. Red while live; on save it stays in place, turns
 * green with the button, and its bars run out silent while it fades.
 * It sits behind the button and blocks nothing.
 */
@Composable
private fun EqRing(live: Boolean, confirming: Boolean, modifier: Modifier = Modifier) {
    // Plain array, not state: the frame clock below is what redraws the ring.
    val levels = remember { FloatArray(EQ_BARS) }
    var stepProgress by remember { mutableFloatStateOf(0f) }
    val fade = remember { Animatable(1f) }
    val liveNow by rememberUpdatedState(live)

    LaunchedEffect(live) {
        if (live) levels.fill(0f)
    }
    LaunchedEffect(confirming) {
        fade.snapTo(1f)
        if (confirming) {
            delay(SAVE_RING_MS - EQ_FADE_MS)
            fade.animateTo(0f, tween(EQ_FADE_MS, easing = EaseOut))
        }
    }
    if (live || confirming) {
        LaunchedEffect(Unit) {
            var last = withFrameNanos { it }
            while (true) {
                withFrameNanos { now ->
                    var progress = stepProgress + (now - last) / (EQ_STEP_MS * 1_000_000f)
                    last = now
                    while (progress >= 1f) {
                        progress -= 1f
                        levels.copyInto(levels, destinationOffset = 1, endIndex = EQ_BARS - 1)
                        levels[0] = if (liveNow) AudioCaptureService.level.value else 0f
                    }
                    stepProgress = progress
                }
            }
        }
    }

    val color = if (live) LoreRed else LoreGreen
    Canvas(
        modifier.graphicsLayer {
            alpha = when {
                live -> 1f
                confirming -> fade.value
                else -> 0f
            }
        }
    ) {
        if (!live && !confirming) return@Canvas
        // The 6 dp halo right outside the button
        drawCircle(
            color = color.copy(alpha = 0.12f),
            radius = 61.dp.toPx(),
            center = center,
            style = Stroke(width = 12.dp.toPx()),
        )
        val inner = 63.dp.toPx()
        val stub = 3.dp.toPx()
        val span = 21.dp.toPx()
        val strokeWidth = 3.dp.toPx()
        val progress = stepProgress
        for (i in 0 until EQ_BARS) {
            val age = i + progress
            val entering = age.coerceAtMost(1f)
            val leaving = ((EQ_BARS - age) / EQ_TAIL_BARS).coerceIn(0f, 1f)
            val outer = inner + stub + span * levels[i] * entering * leaving
            val angle = age * 2f * PI.toFloat() / EQ_BARS
            val sinA = sin(angle)
            val cosA = cos(angle)
            drawLine(
                color = color,
                start = Offset(center.x + sinA * inner, center.y - cosA * inner),
                end = Offset(center.x + sinA * outer, center.y - cosA * outer),
                strokeWidth = strokeWidth,
                cap = StrokeCap.Round,
            )
        }
    }
}

@Composable
private fun RecordButton(
    isRecording: Boolean,
    saved: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val bg by animateColorAsState(
        when {
            isRecording -> LoreRed
            saved -> LoreGreen
            else -> LoreAccent
        },
        animationSpec = tween(180),
        label = "recordBg",
    )
    val description = stringResource(
        if (isRecording) R.string.record_stop else R.string.record_start
    )
    Box(
        modifier
            .size(116.dp)
            .shadow(
                elevation = 12.dp,
                shape = CircleShape,
                spotColor = if (isRecording) LoreRed.copy(alpha = 0.19f)
                else LoreAccent.copy(alpha = 0.25f),
                ambientColor = Color.Transparent,
            )
            .clip(CircleShape)
            .background(bg)
            .semantics { contentDescription = description }
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        // Never disabled by an upload: persisting is the service's tail,
        // so a recording can start again the moment this one stops.
        if (isRecording) {
            // Filled rounded square, bg-colored on red (mock stop icon)
            Canvas(Modifier.size(34.dp)) {
                val side = size.width * 10f / 24f
                val corner = size.width * 2f / 24f
                drawRoundRect(
                    color = LoreBg,
                    topLeft = Offset((size.width - side) / 2f, (size.height - side) / 2f),
                    size = Size(side, side),
                    cornerRadius = CornerRadius(corner, corner),
                )
            }
        } else {
            Icon(
                Icons.Outlined.Mic,
                contentDescription = stringResource(R.string.record_start),
                tint = if (saved) LoreBg else LoreStrong,
                modifier = Modifier.size(34.dp),
            )
        }
    }
}

/** Round hold-to-cancel: red fill grows from the bottom over the 470 ms. */
@Composable
private fun CancelHoldButton(
    modifier: Modifier = Modifier,
    onHoldFired: () -> Unit,
) {
    val fill = remember { Animatable(0f) }
    var holding by remember { mutableStateOf(false) }
    val nudgeAnim = remember { Animatable(0f) }
    val scope = rememberCoroutineScope()
    val density = LocalDensity.current

    Box(
        modifier
            .graphicsLayer {
                translationX = with(density) { nudgeAnim.value.dp.toPx() }
            }
            .size(44.dp)
            .clip(CircleShape)
            .background(LoreSurface2)
            .border(1.dp, if (holding) LoreRed else LoreSurface3, CircleShape)
            .pointerInput(Unit) {
                detectTapGestures(
                    onPress = {
                        holding = true
                        var fired = false
                        val job = scope.launch {
                            fill.animateTo(1f, tween(HOLD_MS, easing = LinearEasing))
                            fired = true
                            holding = false
                            fill.snapTo(0f)
                            onHoldFired()
                        }
                        val released = tryAwaitRelease()
                        if (!fired) {
                            job.cancel()
                            fill.snapTo(0f)
                            holding = false
                            if (released) scope.launch { nudgeAnim.nudge() }
                        }
                    },
                )
            },
        contentAlignment = Alignment.Center,
    ) {
        Canvas(Modifier.matchParentSize()) {
            if (fill.value > 0f) {
                drawRect(
                    color = LoreRed,
                    size = Size(size.width, size.height * fill.value),
                    topLeft = Offset(0f, size.height * (1f - fill.value)),
                )
            }
        }
        Icon(
            Icons.Outlined.Close,
            contentDescription = stringResource(R.string.record_cancel_hold),
            tint = if (holding) LoreBg else LoreText,
            modifier = Modifier.size(17.dp),
        )
    }
}

/** Edge source toggle: solid accent when on, struck-out dim icon when off. */
@Composable
private fun SourceButton(
    on: Boolean,
    dimmed: Boolean,
    icon: ImageVector,
    contentDescription: String,
    nudgeCount: Int,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val nudgeAnim = remember { Animatable(0f) }
    val density = LocalDensity.current
    LaunchedEffect(nudgeCount) {
        if (nudgeCount > 0) nudgeAnim.nudge()
    }
    val shape = RoundedCornerShape(topEnd = 10.dp, bottomEnd = 10.dp)
    Box(
        Modifier
            .graphicsLayer {
                alpha = if (dimmed) 0.45f else 1f
                translationX = with(density) { nudgeAnim.value.dp.toPx() }
            }
            .size(40.dp)
            .clip(shape)
            .background(if (on) LoreAccent else Color.Transparent)
            .border(1.dp, if (on) LoreAccent else LoreBorder, shape)
            .clickable(enabled = enabled, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        val tint = if (on) LoreStrong else LoreDim
        Icon(icon, contentDescription, tint = tint, modifier = Modifier.size(17.dp))
        if (!on) {
            // The strike-through slash of the off state
            Canvas(Modifier.size(17.dp)) {
                drawLine(
                    color = LoreDim,
                    start = Offset(0f, 0f),
                    end = Offset(size.width, size.height),
                    strokeWidth = 2.dp.toPx(),
                    cap = StrokeCap.Round,
                )
            }
        }
    }
}

/** One document tab on the right edge: the number, or the unrolled title. */
@Composable
private fun DocTab(
    key: DocumentKeyEntity,
    selected: Boolean,
    open: Boolean,
    locked: Boolean,
    onClick: () -> Unit,
) {
    val title = key.documentTitle
    // A null title unrolls as the number only: no width to gain
    val unrolled = open && title != null
    val width by animateDpAsState(
        if (unrolled) 210.dp else 42.dp,
        tween(180, easing = EaseOut),
        label = "tabWidth",
    )
    val nameAlpha by animateFloatAsState(
        if (unrolled) 1f else 0f,
        tween(120),
        label = "tabNameAlpha",
    )
    val shape = RoundedCornerShape(topStart = 10.dp, bottomStart = 10.dp)
    Box(
        Modifier
            .graphicsLayer { alpha = if (locked) 0.45f else 1f }
            .shadow(if (unrolled) 8.dp else 0.dp, shape, clip = false)
            .width(width)
            .height(40.dp)
            .clip(shape)
            .background(if (selected) LoreAccent else LoreSurface)
            .border(1.dp, if (selected) LoreAccent else LoreBorder, shape)
            .clickable(enabled = !locked, onClick = onClick)
            .clipToBounds(),
    ) {
        Row(modifier = Modifier.matchParentSize(), verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .weight(1f)
                    .padding(start = 12.dp)
                    .graphicsLayer { alpha = nameAlpha },
                contentAlignment = Alignment.CenterStart,
            ) {
                Text(
                    title.orEmpty(),
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = if (selected) LoreStrong else LoreMuted,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Box(
                modifier = Modifier.width(41.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    "%02d".format(key.position ?: 0),
                    fontSize = 10.sp,
                    fontWeight = FontWeight.W800,
                    color = if (selected) LoreStrong else LoreMuted,
                )
            }
        }
    }
}

/** The «+» tab under the document list — adds a key. */
@Composable
private fun DocTabAdd(locked: Boolean, onClick: () -> Unit) {
    val shape = RoundedCornerShape(topStart = 10.dp, bottomStart = 10.dp)
    val description = stringResource(R.string.record_doc_add)
    Box(
        Modifier
            .graphicsLayer { alpha = if (locked) 0.45f else 1f }
            .size(width = 42.dp, height = 40.dp)
            .clip(shape)
            .background(LoreSurface)
            .border(1.dp, LoreBorder, shape)
            .clickable(enabled = !locked, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            "+",
            fontSize = 14.sp,
            fontWeight = FontWeight.W800,
            color = LoreMuted,
            modifier = Modifier.semantics { contentDescription = description },
        )
    }
}

/** «+» tab dialog: paste field + «Из буфера», same add rules as Settings. */
@Composable
private fun AddKeyDialog(
    field: String,
    errorRes: Int?,
    busy: Boolean,
    onFieldChange: (String) -> Unit,
    onPaste: () -> Unit,
    onAdd: () -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.settings_add_title)) },
        text = {
            Column {
                OutlinedTextField(
                    value = field,
                    onValueChange = onFieldChange,
                    singleLine = true,
                    placeholder = { Text(stringResource(R.string.settings_key_placeholder)) },
                    trailingIcon = {
                        IconButton(onClick = onPaste) {
                            Icon(
                                Icons.Outlined.ContentPaste,
                                contentDescription = stringResource(R.string.settings_paste),
                            )
                        }
                    },
                    isError = errorRes != null,
                    modifier = Modifier.fillMaxWidth(),
                )
                errorRes?.let {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        stringResource(it),
                        color = LoreRed,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        },
        confirmButton = {
            TextButton(
                onClick = onAdd,
                enabled = field.isNotBlank() && !busy,
            ) {
                Text(stringResource(R.string.settings_add_key))
            }
        },
    )
}

/** Mock nudge: ±3 dp shake over 260 ms. */
private suspend fun Animatable<Float, AnimationVector1D>.nudge() {
    snapTo(0f)
    animateTo(
        0f,
        keyframes {
            durationMillis = 260
            0f at 0
            (-3f) at 65
            3f at 195
        },
    )
}

/** One start intent for all three modes: extras pick the sources, projection grant rides along when system is on. */
private fun startCapture(
    context: Context,
    keyId: Long?,
    mic: Boolean,
    system: Boolean,
    resultCode: Int? = null,
    resultData: Intent? = null,
) {
    val intent = Intent(context, AudioCaptureService::class.java).apply {
        action = AudioCaptureService.ACTION_START
        putExtra(AudioCaptureService.EXTRA_SOURCE_MIC, mic)
        putExtra(AudioCaptureService.EXTRA_SOURCE_SYSTEM, system)
        keyId?.let { putExtra(AudioCaptureService.EXTRA_KEY_ID, it) }
        resultCode?.let { putExtra(AudioCaptureService.EXTRA_RESULT_CODE, it) }
        resultData?.let { putExtra(AudioCaptureService.EXTRA_RESULT_DATA, it) }
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
