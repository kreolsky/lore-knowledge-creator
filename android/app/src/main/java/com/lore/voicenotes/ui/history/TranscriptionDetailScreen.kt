package com.lore.voicenotes.ui.history

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.automirrored.outlined.Send
import androidx.compose.material.icons.outlined.Check
import androidx.compose.material.icons.outlined.ContentCopy
import androidx.compose.material.icons.outlined.Pause
import androidx.compose.material.icons.outlined.PlayArrow
import androidx.compose.material.icons.outlined.Share
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lore.voicenotes.R
import com.lore.voicenotes.data.local.TranscriptionEntity
import com.lore.voicenotes.ui.theme.LoreAccent
import com.lore.voicenotes.ui.theme.LoreBorder
import com.lore.voicenotes.ui.theme.LoreDim
import com.lore.voicenotes.ui.theme.LoreGreen
import com.lore.voicenotes.ui.theme.LoreMuted
import com.lore.voicenotes.ui.theme.LoreStrong
import com.lore.voicenotes.ui.theme.LoreSurface
import com.lore.voicenotes.ui.theme.LoreSurface2
import com.lore.voicenotes.ui.theme.LoreSurface3
import com.lore.voicenotes.ui.theme.LoreText
import kotlinx.coroutines.delay

private const val COPIED_MS = 1500L

/** 11 dp tabular time, as the mock's .time. */
private val TabularTimeStyle = TextStyle(
    fontSize = 11.sp,
    lineHeight = 15.sp,
    fontFeatureSettings = "tnum",
)

/**
 * Detail screen from history-v1: head with back, player card with its own
 * scrubber, selectable text; copy and share float small and round bottom
 * right over the text, which keeps 60 dp of void under its end. Navigation
 * stays visible — the screen lives inside the tab frame.
 */
@Composable
fun TranscriptionDetailScreen(
    transcriptionId: Long,
    onBack: () -> Unit,
    viewModel: HistoryViewModel = hiltViewModel(),
) {
    val context = LocalContext.current
    val entry by remember(transcriptionId) {
        viewModel.observeEntry(transcriptionId)
    }.collectAsStateWithLifecycle(initialValue = null)
    val keys by viewModel.keys.collectAsStateWithLifecycle()
    val playerState by viewModel.playerState.collectAsStateWithLifecycle()
    var copied by remember { mutableStateOf(false) }

    LaunchedEffect(copied) {
        if (copied) {
            delay(COPIED_MS)
            copied = false
        }
    }

    // The row was deleted and its undo window passed while open: leave.
    var sawRow by remember { mutableStateOf(false) }
    LaunchedEffect(entry) {
        if (entry != null) sawRow = true
        else if (sawRow) onBack()
    }

    val e = entry
    if (e == null) return

    val ready = e.status == TranscriptionEntity.STATUS_READY && !e.content.isNullOrBlank()
    val docNumber = keys.firstOrNull { it.id == e.keyId }?.position

    Column(Modifier.fillMaxSize()) {
        // Head: back arrow, when, document number.
        Row(
            Modifier
                .fillMaxWidth()
                .padding(start = 6.dp, end = 15.dp, top = 6.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Box(
                Modifier
                    .size(38.dp)
                    .clip(CircleShape)
                    .clickable(onClick = onBack),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    Icons.AutoMirrored.Outlined.ArrowBack,
                    contentDescription = stringResource(R.string.detail_back),
                    tint = LoreText,
                    modifier = Modifier.size(17.dp),
                )
            }
            Column {
                Text(
                    text = formatWhen(e.createdAt),
                    fontSize = 13.sp,
                    lineHeight = 18.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = LoreStrong,
                )
                docNumber?.let {
                    Text(
                        text = stringResource(R.string.detail_document, "%02d".format(it)),
                        fontSize = 11.sp,
                        lineHeight = 15.sp,
                        color = LoreMuted,
                    )
                }
            }
        }

        // Player card: play, scrubber, times.
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 15.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(LoreSurface)
                .border(1.dp, LoreBorder, RoundedCornerShape(12.dp))
                .padding(start = 12.dp, end = 12.dp, top = 10.dp, bottom = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            val playable = e.localFilePath != null
            val on = playerState.entryId == e.id && playerState.playing
            Box(
                Modifier
                    .size(46.dp)
                    .clip(CircleShape)
                    .background(if (playable && on) LoreAccent else LoreSurface2)
                    .border(
                        1.dp,
                        if (playable && on) LoreAccent else LoreBorder,
                        CircleShape,
                    )
                    .clickable(enabled = playable) {
                        viewModel.playPause(e.id, e.localFilePath, e.durationMs)
                    },
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    imageVector = if (on) Icons.Outlined.Pause else Icons.Outlined.PlayArrow,
                    contentDescription = stringResource(
                        if (on) R.string.history_pause else R.string.history_play
                    ),
                    tint = if (!playable) LoreDim else LoreStrong,
                    modifier = Modifier.size(20.dp),
                )
            }
            val active = playerState.entryId == e.id
            val duration = if (active) {
                maxOf(playerState.durationMs, e.durationMs ?: 0L)
            } else {
                e.durationMs ?: 0L
            }
            Column(Modifier.weight(1f)) {
                val progress = if (active && duration > 0) {
                    playerState.positionMs.toFloat() / duration
                } else 0f
                Scrubber(
                    progress = progress,
                    // Seeking targets the controller's active item, so the
                    // card scrubber works only while this row plays.
                    enabled = playable && active && duration > 0,
                    onSeek = { viewModel.seekTo((it * duration).toLong()) },
                )
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    val now = if (playerState.entryId == e.id) playerState.positionMs else 0L
                    Text(
                        text = formatDuration(now),
                        style = TabularTimeStyle,
                        color = LoreMuted,
                    )
                    Text(
                        text = formatDuration(duration),
                        style = TabularTimeStyle,
                        color = LoreMuted,
                    )
                }
            }
        }

        // Text with the floating actions over it.
        Box(Modifier.weight(1f).fillMaxWidth()) {
            Column(
                Modifier
                    .fillMaxSize()
                    .verticalScroll(rememberScrollState())
                    .padding(
                        start = 15.dp,
                        end = 15.dp,
                        top = 12.dp,
                        // 60 dp of void under the text end, so the last line
                        // scrolls clear above the floating buttons.
                        bottom = if (ready) 60.dp else 12.dp,
                    ),
            ) {
                if (ready) {
                    SelectionContainer {
                        Text(
                            text = e.content.orEmpty(),
                            fontSize = 14.sp,
                            lineHeight = 21.sp,
                            color = LoreText,
                        )
                    }
                } else {
                    val (label, color) = statusStyle(e.status)
                    Text(label, fontSize = 14.sp, lineHeight = 21.sp, color = color)
                }
            }
            if (ready) {
                Row(
                    Modifier
                        .align(Alignment.BottomEnd)
                        .padding(end = 12.dp, bottom = 10.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    ActionButton(
                        icon = if (copied) Icons.Outlined.Check else Icons.Outlined.ContentCopy,
                        contentDescription = stringResource(R.string.history_copy),
                        tint = if (copied) LoreGreen else LoreText,
                        border = if (copied) LoreGreen else LoreSurface3,
                        onClick = {
                            copyText(context, e.content.orEmpty())
                            copied = true
                        },
                    )
                    ActionButton(
                        icon = Icons.Outlined.Share,
                        contentDescription = stringResource(R.string.detail_share),
                        tint = LoreText,
                        border = LoreSurface3,
                        onClick = {
                            val intent = Intent(Intent.ACTION_SEND).apply {
                                type = "text/plain"
                                putExtra(Intent.EXTRA_TEXT, e.content.orEmpty())
                            }
                            context.startActivity(
                                Intent.createChooser(
                                    intent,
                                    context.getString(R.string.detail_share),
                                )
                            )
                        },
                    )
                }
            }
        }
    }
}

/** Small round floating action: surface2, surface3 border, shadow. */
@Composable
private fun ActionButton(
    icon: ImageVector,
    contentDescription: String,
    tint: Color,
    border: Color,
    onClick: () -> Unit,
) {
    Box(
        Modifier
            .size(38.dp)
            .shadow(4.dp, CircleShape, clip = false)
            .clip(CircleShape)
            .background(LoreSurface2)
            .border(1.dp, border, CircleShape)
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(icon, contentDescription, tint = tint, modifier = Modifier.size(17.dp))
    }
}

private fun copyText(context: Context, text: String) {
    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    clipboard.setPrimaryClip(ClipData.newPlainText("lore", text))
}
