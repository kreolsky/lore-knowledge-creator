package com.lore.voicenotes.ui.history

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Close
import androidx.compose.material.icons.outlined.Pause
import androidx.compose.material.icons.outlined.PlayArrow
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.lore.voicenotes.R
import com.lore.voicenotes.ui.theme.LoreAccent
import com.lore.voicenotes.ui.theme.LoreBorder
import com.lore.voicenotes.ui.theme.LoreMuted
import com.lore.voicenotes.ui.theme.LoreStrong
import com.lore.voicenotes.ui.theme.LoreSurface
import com.lore.voicenotes.ui.theme.LoreSurface2
import com.lore.voicenotes.ui.theme.LoreText

/** 11 dp tabular time, as the mock's .time. */
private val TabularTimeStyle = TextStyle(
    fontSize = 11.sp,
    lineHeight = 15.sp,
    fontFeatureSettings = "tnum",
)

/**
 * The bar above navigation: play, title, position / duration, the scrubber
 * (it never sits inside a swipeable row) and a close button. Hidden only on
 * the detail screen of the row it plays — that screen carries its own card.
 */
@Composable
fun PlayerBar(
    state: PlayerBarUiState,
    onPlayPause: () -> Unit,
    onSeek: (Float) -> Unit,
    onClose: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val title = state.createdAt?.let { formatWhen(it) } ?: ""
    Row(
        modifier
            .fillMaxWidth()
            .drawBehind {
                drawRect(
                    color = LoreBorder,
                    topLeft = Offset(0f, 0f),
                    size = Size(size.width, 1.dp.toPx()),
                )
            }
            .background(LoreSurface)
            .padding(start = 12.dp, end = 6.dp, top = 7.dp, bottom = 7.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Box(
            Modifier
                .size(34.dp)
                .clip(CircleShape)
                .background(if (state.playing) LoreAccent else LoreSurface2)
                .border(
                    1.dp,
                    if (state.playing) LoreAccent else LoreBorder,
                    CircleShape,
                )
                .clickable(enabled = state.playable, onClick = onPlayPause),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                imageVector = if (state.playing) Icons.Outlined.Pause else Icons.Outlined.PlayArrow,
                contentDescription = stringResource(
                    if (state.playing) R.string.history_pause else R.string.history_play
                ),
                tint = LoreStrong,
                modifier = Modifier.size(15.dp),
            )
        }
        Column(Modifier.weight(1f)) {
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = title,
                    fontSize = 11.sp,
                    lineHeight = 15.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = LoreStrong,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f, fill = false),
                )
                Text(
                    text = "${formatDuration(state.positionMs)} / ${formatDuration(state.durationMs)}",
                    style = TabularTimeStyle,
                    color = LoreMuted,
                )
            }
            Scrubber(
                progress = if (state.durationMs > 0) {
                    state.positionMs.toFloat() / state.durationMs
                } else 0f,
                enabled = state.playable && state.durationMs > 0,
                onSeek = onSeek,
            )
        }
        Box(
            Modifier
                .size(38.dp)
                .clip(CircleShape)
                .clickable(onClick = onClose),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                Icons.Outlined.Close,
                contentDescription = stringResource(R.string.player_close),
                tint = LoreMuted,
                modifier = Modifier.size(17.dp),
            )
        }
    }
}
