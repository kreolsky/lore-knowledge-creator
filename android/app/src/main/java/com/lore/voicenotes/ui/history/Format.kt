package com.lore.voicenotes.ui.history

import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import com.lore.voicenotes.R
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

private val ru = Locale("ru")
private val timeFmt = DateTimeFormatter.ofPattern("HH:mm", ru)
private val sameYearFmt = DateTimeFormatter.ofPattern("d MMM, HH:mm", ru)
private val otherYearFmt = DateTimeFormatter.ofPattern("d MMM yyyy, HH:mm", ru)

/**
 * Row title is the date, never the file name: «Сегодня, 17:12»,
 * «Вчера, 19:03», «2 ноя, 14:22», «28 окт 2024, 16:05».
 */
@Composable
fun formatWhen(millis: Long): String = formatWhen(
    millis = millis,
    todayWord = stringResource(R.string.history_today),
    yesterdayWord = stringResource(R.string.history_yesterday),
)

fun formatWhen(millis: Long, todayWord: String, yesterdayWord: String): String {
    val zone = ZoneId.systemDefault()
    val zoned = Instant.ofEpochMilli(millis).atZone(zone)
    val today = LocalDate.now(zone)
    return when (zoned.toLocalDate()) {
        today -> "$todayWord, ${timeFmt.format(zoned.toLocalTime())}"
        today.minusDays(1) -> "$yesterdayWord, ${timeFmt.format(zoned.toLocalTime())}"
        else -> if (zoned.year == today.year) {
            sameYearFmt.format(zoned)
        } else {
            otherYearFmt.format(zoned)
        }
    }
}

/** mm:ss, minutes unbounded (31:00 for half an hour), as in the mock. */
fun formatDuration(ms: Long): String {
    val totalSeconds = ms / 1000
    return "%02d:%02d".format(totalSeconds / 60, totalSeconds % 60)
}
