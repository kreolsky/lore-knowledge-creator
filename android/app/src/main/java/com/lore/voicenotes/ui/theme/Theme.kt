package com.lore.voicenotes.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

private val DarkColorScheme = darkColorScheme(
    primary = LoreAccent,
    onPrimary = LoreStrong,
    secondary = LoreMuted,
    onSecondary = LoreBg,
    tertiary = LoreAccentText,
    onTertiary = LoreBg,
    background = LoreBg,
    onBackground = LoreText,
    surface = LoreSurface,
    onSurface = LoreText,
    surfaceVariant = LoreSurface2,
    onSurfaceVariant = LoreMuted,
    surfaceContainer = LoreSurface,
    surfaceContainerHigh = LoreSurface2,
    surfaceContainerHighest = LoreSurface3,
    outline = LoreBorder,
    outlineVariant = LoreBorder,
    error = LoreRed,
    onError = LoreBg,
)

@Composable
fun LoreTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DarkColorScheme,
        shapes = LoreShapes,
        content = content,
    )
}
