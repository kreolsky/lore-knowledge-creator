package com.lore.voicenotes.ui.history

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.AnimationVector1D
import androidx.compose.animation.core.EaseOut
import androidx.compose.animation.core.keyframes
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.Send
import androidx.compose.material.icons.outlined.Check
import androidx.compose.material.icons.outlined.ContentCopy
import androidx.compose.material.icons.outlined.Delete
import androidx.compose.material.icons.outlined.Pause
import androidx.compose.material.icons.outlined.PlayArrow
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lore.voicenotes.R
import com.lore.voicenotes.audio.PlayerState
import com.lore.voicenotes.data.local.TranscriptionEntity
import com.lore.voicenotes.ui.theme.LoreAccent
import com.lore.voicenotes.ui.theme.LoreAccentText
import com.lore.voicenotes.ui.theme.LoreAmber
import com.lore.voicenotes.ui.theme.LoreBg
import com.lore.voicenotes.ui.theme.LoreBlue
import com.lore.voicenotes.ui.theme.LoreBorder
import com.lore.voicenotes.ui.theme.LoreDim
import com.lore.voicenotes.ui.theme.LoreGreen
import com.lore.voicenotes.ui.theme.LoreMuted
import com.lore.voicenotes.ui.theme.LorePurpleTint
import com.lore.voicenotes.ui.theme.LoreRed
import com.lore.voicenotes.ui.theme.LoreStrong
import com.lore.voicenotes.ui.theme.LoreSurface2
import com.lore.voicenotes.ui.theme.LoreSurface3
import com.lore.voicenotes.ui.theme.LoreText
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.launch

// Swipe thresholds as shares of the row width, from the mock.
private const val SWIPE_DELETE = 0.51f
private const val SWIPE_RESEND = 0.38f
private const val COPIED_MS = 1500L

@Composable
fun HistoryScreen(
    onTranscriptionClick: (Long) -> Unit,
    viewModel: HistoryViewModel = hiltViewModel(),
) {
    val rows by viewModel.rows.collectAsStateWithLifecycle()
    val playerState by viewModel.playerState.collectAsStateWithLifecycle()
    val undo by viewModel.undo.collectAsStateWithLifecycle()

    Box(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize()) {
            Text(
                text = stringResource(R.string.history_title),
                fontSize = 22.sp,
                lineHeight = 28.sp,
                fontWeight = FontWeight.SemiBold,
                letterSpacing = (-0.2).sp,
                color = LoreStrong,
                modifier = Modifier.padding(start = 15.dp, top = 10.dp, bottom = 8.dp),
            )
            if (rows.isEmpty()) {
                Box(
                    Modifier
                        .weight(1f)
                        .fillMaxWidth(),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = stringResource(R.string.history_empty),
                        fontSize = 13.sp,
                        color = LoreMuted,
                    )
                }
            } else {
                LazyColumn(Modifier.weight(1f).fillMaxWidth()) {
                    items(rows, key = { it.entry.id }) { row ->
                        HistoryRow(
                            row = row,
                            playerState = playerState,
                            resendRefused = viewModel.resendRefused,
                            onClick = { onTranscriptionClick(row.entry.id) },
                            onDelete = { viewModel.delete(row.entry.id) },
                            onResend = { viewModel.resend(row.entry.id) },
                            onPlayPause = {
                                viewModel.playPause(
                                    row.entry.id,
                                    row.entry.localFilePath,
                                    row.entry.durationMs,
                                )
                            },
                        )
                    }
                    // The last row closes the list with its bottom border.
                    item {
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .height(1.dp)
                                .background(LoreBorder)
                        )
                    }
                }
            }
        }

        // Delete keeps a way back: the row returns until the bar leaves.
        if (undo != null) {
            UndoBar(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(10.dp),
                onUndo = viewModel::undoDelete,
            )
        }
    }
}

/** Snack at Android scale, not the mock's: on a phone 40 dp read as a thin strip. */
@Composable
private fun UndoBar(modifier: Modifier = Modifier, onUndo: () -> Unit) {
    Row(
        modifier
            .fillMaxWidth()
            .height(56.dp)
            .shadow(8.dp, RoundedCornerShape(12.dp))
            .clip(RoundedCornerShape(12.dp))
            .background(LoreSurface3)
            .padding(start = 16.dp, end = 6.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = stringResource(R.string.history_deleted),
            fontSize = 14.sp,
            color = LoreStrong,
        )
        Text(
            text = stringResource(R.string.history_undo),
            fontSize = 14.sp,
            fontWeight = FontWeight.W800,
            color = LoreAccentText,
            modifier = Modifier
                .clip(CircleShape)
                .clickable(onClick = onUndo)
                .padding(horizontal = 12.dp, vertical = 12.dp),
        )
    }
}

/**
 * One row with the custom swipe: delete past 51 % of the row width, re-send
 * past 38 %, position only — starting anywhere on the row, buttons included,
 * because the drag detector sits on the row itself while taps stay taps.
 */
@Composable
private fun HistoryRow(
    row: HistoryRow,
    playerState: PlayerState,
    resendRefused: SharedFlow<Long>,
    onClick: () -> Unit,
    onDelete: () -> Unit,
    onResend: () -> Unit,
    onPlayPause: () -> Unit,
) {
    val entry = row.entry
    val scope = rememberCoroutineScope()
    val offsetX = remember { Animatable(0f) }
    val nudgeAnim = remember { Animatable(0f) }
    var rowWidthPx by remember { mutableStateOf(1f) }
    var copied by remember { mutableStateOf(false) }
    val context = LocalContext.current

    // A refused re-send (key gone, zero keys, no file) nudges the row.
    LaunchedEffect(Unit) {
        resendRefused.collect { if (it == entry.id) nudgeAnim.nudge() }
    }
    LaunchedEffect(copied) {
        if (copied) {
            delay(COPIED_MS)
            copied = false
        }
    }

    Box(
        Modifier
            .fillMaxWidth()
            .onSizeChanged { rowWidthPx = it.width.toFloat() }
            .drawWithContent {
                drawContent()
                // Row top border, full bleed as in the mock. Over the content:
                // the opaque row would hide it from behind.
                drawRect(
                    color = LoreBorder,
                    topLeft = Offset(0f, 0f),
                    size = Size(size.width, 1.dp.toPx()),
                )
            }
            .graphicsLayer { translationX = nudgeAnim.value.dp.toPx() }
            .pointerInput(Unit) {
                detectHorizontalDragGestures(
                    onHorizontalDrag = { change, amount ->
                        change.consume()
                        val target = (offsetX.value + amount).coerceIn(-rowWidthPx, rowWidthPx)
                        scope.launch { offsetX.snapTo(target) }
                    },
                    onDragEnd = {
                        val dx = offsetX.value
                        val w = rowWidthPx
                        scope.launch {
                            when {
                                dx < 0 && -dx > w * SWIPE_DELETE -> {
                                    offsetX.animateTo(-w, tween(180, easing = EaseOut))
                                    onDelete()
                                }
                                dx > 0 && dx > w * SWIPE_RESEND -> {
                                    offsetX.animateTo(0f, tween(180, easing = EaseOut))
                                    onResend()
                                }
                                else -> offsetX.animateTo(0f, tween(180, easing = EaseOut))
                            }
                        }
                    },
                    onDragCancel = {
                        scope.launch { offsetX.animateTo(0f, tween(180, easing = EaseOut)) }
                    },
                )
            },
    ) {
        SwipeUnderlay(offsetX = offsetX.value, modifier = Modifier.matchParentSize())
        Row(
            Modifier
                .fillMaxWidth()
                .graphicsLayer { translationX = offsetX.value }
                .background(LoreBg)
                .padding(start = 12.dp, end = 6.dp, top = 9.dp, bottom = 9.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            PlayButton(
                active = playerState.entryId == entry.id,
                playing = playerState.playing,
                enabled = entry.localFilePath != null,
                onClick = onPlayPause,
            )
            Column(
                Modifier
                    .weight(1f)
                    .clickable(onClick = onClick),
            ) {
                Text(
                    text = formatWhen(entry.createdAt),
                    fontSize = 13.sp,
                    lineHeight = 18.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = if (playerState.entryId == entry.id) LoreAccentText else LoreStrong,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                RowMeta(entry = entry, docPosition = row.docPosition)
                RowLine(entry = entry)
            }
            if (entry.status == TranscriptionEntity.STATUS_READY) {
                CopyButton(
                    copied = copied,
                    enabled = !entry.content.isNullOrBlank(),
                    onCopy = {
                        copyText(context, entry.content.orEmpty())
                        copied = true
                    },
                )
            }
        }
    }
}

/** Red delete underlay to the right, purple-tint re-send underlay to the left. */
@Composable
private fun SwipeUnderlay(offsetX: Float, modifier: Modifier = Modifier) {
    val deleting = offsetX < 0
    Box(
        modifier
            .background(
                when {
                    offsetX < 0 -> LoreRed.copy(alpha = 0.14f)
                    offsetX > 0 -> LorePurpleTint
                    else -> LoreBg
                }
            ),
        contentAlignment = if (deleting) Alignment.CenterEnd else Alignment.CenterStart,
    ) {
        if (offsetX != 0f) {
            Row(
                Modifier.padding(horizontal = 18.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                val color = if (deleting) LoreRed else LoreAccentText
                val icon: ImageVector = if (deleting) {
                    Icons.Outlined.Delete
                } else {
                    Icons.AutoMirrored.Outlined.Send
                }
                val label = stringResource(
                    if (deleting) R.string.history_delete else R.string.history_resend
                )
                if (!deleting) {
                    Icon(icon, contentDescription = label, tint = color, modifier = Modifier.size(17.dp))
                }
                Text(label, fontSize = 12.sp, fontWeight = FontWeight.W700, color = color)
                if (deleting) {
                    Icon(icon, contentDescription = label, tint = color, modifier = Modifier.size(17.dp))
                }
            }
        }
    }
}

/** 38 dp round play; accent while its row plays, dim and inert without a file. */
@Composable
private fun PlayButton(
    active: Boolean,
    playing: Boolean,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val on = active && playing
    Box(
        Modifier
            .size(38.dp)
            .clip(CircleShape)
            .background(when {
                !enabled -> Color.Transparent
                on -> LoreAccent
                else -> LoreSurface2
            })
            .border(1.dp, if (enabled && on) LoreAccent else LoreBorder, CircleShape)
            .clickable(enabled = enabled, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            imageVector = if (on) Icons.Outlined.Pause else Icons.Outlined.PlayArrow,
            contentDescription = stringResource(
                if (on) R.string.history_pause else R.string.history_play
            ),
            tint = if (!enabled) LoreDim else if (on) LoreStrong else LoreText,
            modifier = Modifier.size(16.dp),
        )
    }
}

/** Round copy icon; a copy turns it into a green check for 1.5 s. */
@Composable
private fun CopyButton(copied: Boolean, enabled: Boolean, onCopy: () -> Unit) {
    Box(
        Modifier
            .size(38.dp)
            .clip(CircleShape)
            .clickable(enabled = enabled, onClick = onCopy),
        contentAlignment = Alignment.Center,
    ) {
        if (copied) {
            Icon(
                Icons.Outlined.Check,
                contentDescription = stringResource(R.string.history_copy),
                tint = LoreGreen,
                modifier = Modifier.size(17.dp),
            )
        } else {
            Icon(
                Icons.Outlined.ContentCopy,
                contentDescription = stringResource(R.string.history_copy),
                tint = LoreMuted,
                modifier = Modifier.size(17.dp),
            )
        }
    }
}

/** Second line: duration and document number, tabular, muted. */
@Composable
private fun RowMeta(entry: TranscriptionEntity, docPosition: Int?) {
    val meta = buildList {
        entry.durationMs?.let { add(formatDuration(it)) }
        docPosition?.let { add(stringResource(R.string.history_row_document, "%02d".format(it))) }
    }.joinToString(" · ")
    if (meta.isEmpty()) return
    Text(
        text = meta,
        fontSize = 11.sp,
        lineHeight = 15.sp,
        color = LoreMuted,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
    )
}

/** Third line: the text's first line, or the coloured status. */
@Composable
private fun RowLine(entry: TranscriptionEntity) {
    val firstLine = entry.content
        ?.lineSequence()
        ?.firstOrNull { it.isNotBlank() }
        ?.trim()
    if (entry.status == TranscriptionEntity.STATUS_READY && !firstLine.isNullOrEmpty()) {
        Text(
            text = firstLine,
            fontSize = 12.sp,
            lineHeight = 17.sp,
            color = LoreMuted,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    } else {
        val (label, color) = statusStyle(entry.status)
        Text(
            text = label,
            fontSize = 12.sp,
            lineHeight = 17.sp,
            color = color,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/** Status label + colour, mock map; `error` is what the server writes. */
@Composable
internal fun statusStyle(status: String): Pair<String, Color> = when (status) {
    TranscriptionEntity.STATUS_PENDING_UPLOAD ->
        stringResource(R.string.status_on_device) to LoreAmber
    TranscriptionEntity.STATUS_QUEUED ->
        stringResource(R.string.status_queued) to LoreAmber
    TranscriptionEntity.STATUS_PROCESSING ->
        stringResource(R.string.status_processing) to LoreBlue
    TranscriptionEntity.STATUS_READY ->
        stringResource(R.string.status_ready) to LoreGreen
    TranscriptionEntity.STATUS_FAILED, "error" ->
        stringResource(R.string.status_failed) to LoreRed
    TranscriptionEntity.STATUS_UPLOAD_FAILED ->
        stringResource(R.string.status_upload_failed) to LoreRed
    else -> status to LoreMuted
}

private fun copyText(context: Context, text: String) {
    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    clipboard.setPrimaryClip(ClipData.newPlainText("lore", text))
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
