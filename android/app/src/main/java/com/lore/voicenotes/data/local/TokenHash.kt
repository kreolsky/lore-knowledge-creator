package com.lore.voicenotes.data.local

import java.security.MessageDigest

/** SHA-256 of a widget token — the survivor of a soft-deleted key row. */
object TokenHash {
    fun of(token: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(token.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
}
