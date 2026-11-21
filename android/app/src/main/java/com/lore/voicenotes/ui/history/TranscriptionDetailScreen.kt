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
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.outlined.Check
import androidx.compose.material.icons.outlined.ContentCopy
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lore.voicenotes.R
import com.lore.voicenotes.data.local.TranscriptionEntity
import com.lore.voicenotes.ui.theme.LoreGreen
import com.lore.voicenotes.ui.theme.LoreMuted
import com.lore.voicenotes.ui.theme.LoreStrong
import com.lore.voicenotes.ui.theme.LoreSurface2
import com.lore.voicenotes.ui.theme.LoreSurface3
import com.lore.voicenotes.ui.theme.LoreText
import kotlinx.coroutines.delay

private const val COPIED_MS = 1500L

/**
 * Detail screen: head with back, when, document and copy / share on the
 * right; selectable text; the same player bar the other tabs show, pinned
 * above navigation. Navigation stays visible — the screen lives inside the
 * tab frame.
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
        // Head: back arrow, when, document number; copy and share on the right.
        Row(
            Modifier
                .fillMaxWidth()
                .padding(start = 6.dp, end = 12.dp, top = 6.dp, bottom = 8.dp),
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
            Column(Modifier.weight(1f)) {
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
            if (ready) {
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

        // Text.
        Column(
            Modifier
                .weight(1f)
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 15.dp, vertical = 12.dp),
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

        // The same bar the other tabs show above navigation, for this row.
        // Seeking targets the controller's active item, so the scrubber
        // works only while this row is the active one.
        val active = playerState.entryId == e.id
        val playable = e.localFilePath != null
        PlayerBar(
            state = PlayerBarUiState(
                entryId = e.id,
                playing = active && playerState.playing,
                positionMs = if (active) playerState.positionMs else 0L,
                durationMs = if (active) {
                    maxOf(playerState.durationMs, e.durationMs ?: 0L)
                } else {
                    e.durationMs ?: 0L
                },
                createdAt = e.createdAt,
                playable = playable,
            ),
            onPlayPause = { viewModel.playPause(e.id, e.localFilePath, e.durationMs) },
            onSeek = { fraction ->
                val dur = maxOf(playerState.durationMs, e.durationMs ?: 0L)
                if (active && dur > 0) viewModel.seekTo((fraction * dur).toLong())
            },
            onClose = null,
        )
    }
}

/** Small round head action: surface2, surface3 border, shadow. */
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
