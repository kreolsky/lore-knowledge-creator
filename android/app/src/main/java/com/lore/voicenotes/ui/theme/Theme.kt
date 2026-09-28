package com.lore.voicenotes.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

private val DarkColorScheme = darkColorScheme(
    primary = LorePrimary,
    secondary = LoreSecondary,
    tertiary = LoreAccent,
    background = LoreDark,
    surface = LoreSurface,
    surfaceVariant = LoreSurfaceVariant,
    error = LoreError,
    onPrimary = LoreDark,
    onSecondary = LoreDark,
    onBackground = LorePrimary,
    onSurface = LorePrimary,
    onSurfaceVariant = LoreSecondary,
    onError = LoreDark,
)

@Composable
fun LoreTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DarkColorScheme,
        shapes = LoreShapes,
        content = content,
    )
}
