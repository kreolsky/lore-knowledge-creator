package com.lore.voicenotes.data.repository

import android.content.Context
import android.util.Log
import java.io.File
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

private const val FILE_NAME = "upload-log.txt"
private const val MAX_LINES = 100
private const val TAG = "LoreUpload"
private val STAMP: DateTimeFormatter = DateTimeFormatter.ofPattern("MM-dd HH:mm:ss")

/** The file's first bytes as hex: the name and the reported type can both be wrong. */
fun fileHeadHex(file: File): String {
    val head = ByteArray(12)
    val read = file.inputStream().use { it.read(head) }
    return head.take(maxOf(read, 0)).joinToString("") { "%02x".format(it) }
}

/**
 * The upload journal the operator reads and copies in Settings.
 *
 * WHY a file and not only Logcat: the phone is not reachable over adb, and the
 * server's refusal text reaches nothing else on the device.
 *
 * INVARIANT(security): a line never carries the key token or a request header.
 * Why: the journal is copied out of the app by hand and pasted elsewhere.
 */
object UploadLog {
    @Synchronized
    fun write(context: Context, line: String) {
        Log.i(TAG, line)
        val file = File(context.filesDir, FILE_NAME)
        val stamped = "${LocalDateTime.now().format(STAMP)} $line"
        val kept = if (file.exists()) file.readLines().takeLast(MAX_LINES - 1) else emptyList()
        file.writeText((kept + stamped).joinToString("\n", postfix = "\n"))
    }

    @Synchronized
    fun read(context: Context): String {
        val file = File(context.filesDir, FILE_NAME)
        return if (file.exists()) file.readText() else ""
    }
}
