package com.lore.voicenotes.ui.history

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.lore.voicenotes.R
import com.lore.voicenotes.ui.theme.LoreAccentText
import com.lore.voicenotes.ui.theme.LoreStrong
import com.lore.voicenotes.ui.theme.LoreSurface3

/**
 * The scrubber, hand-drawn as in the mock: 3 dp track (accent-text progress
 * over surface3), 12 dp strong thumb. Never lives inside a swipeable row —
 * only in the bar above navigation and on the detail card. Seeking commits on
 * release; while dragging the local value wins over the ticker.
 */
@Composable
fun Scrubber(
    progress: Float,
    enabled: Boolean,
    onSeek: (Float) -> Unit,
    modifier: Modifier = Modifier,
) {
    var dragValue by remember { mutableStateOf<Float?>(null) }
    val value = (dragValue ?: progress).coerceIn(0f, 1f)
    val description = stringResource(R.string.player_seek)

    Canvas(
        modifier
            .fillMaxWidth()
            .height(18.dp)
            .semantics { contentDescription = description }
            .pointerInput(enabled) {
                detectTapGestures { pos ->
                    if (enabled) onSeek((pos.x / size.width).coerceIn(0f, 1f))
                }
            }
            .pointerInput(enabled) {
                detectHorizontalDragGestures(
                    onHorizontalDrag = { change, _ ->
                        if (enabled) {
                            change.consume()
                            dragValue = (change.position.x / size.width).coerceIn(0f, 1f)
                        }
                    },
                    onDragEnd = {
                        dragValue?.let(onSeek)
                        dragValue = null
                    },
                    onDragCancel = { dragValue = null },
                )
            },
    ) {
        val trackHeight = 3.dp.toPx()
        val cy = size.height / 2f
        val half = trackHeight / 2f
        val corner = CornerRadius(half, half)
        drawRoundRect(
            color = LoreSurface3,
            topLeft = Offset(0f, cy - half),
            size = Size(size.width, trackHeight),
            cornerRadius = corner,
        )
        drawRoundRect(
            color = LoreAccentText,
            topLeft = Offset(0f, cy - half),
            size = Size(size.width * value, trackHeight),
            cornerRadius = corner,
        )
        drawCircle(
            color = LoreStrong,
            radius = 6.dp.toPx(),
            center = Offset(size.width * value, cy),
        )
    }
}
