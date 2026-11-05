package com.lore.voicenotes.data.local

import android.content.Context
import android.util.Base64
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import org.json.JSONObject
import javax.inject.Inject
import javax.inject.Singleton

private val Context.dataStore by preferencesDataStore(name = "settings")

data class DecodedConfig(val url: String, val token: String)

/**
 * Post-v3 the key material lives in the `document_keys` Room table; this store
 * keeps only the selected key id. The legacy encoded-key preference is consumed
 * once by AppStartup and cleared.
 */
@Singleton
class SettingsDataStore @Inject constructor(
    @ApplicationContext private val context: Context,
) {
    private val keyEncoded = stringPreferencesKey("api_key_encoded")
    private val selectedKeyId = longPreferencesKey("selected_key_id")

    /** Legacy one-key preference; blank once the v3 migration has consumed it. */
    val encodedKeyFlow: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[keyEncoded] ?: ""
    }

    val selectedKeyIdFlow: Flow<Long?> = context.dataStore.data.map { prefs ->
        prefs[selectedKeyId]
    }

    suspend fun setSelectedKeyId(id: Long) {
        context.dataStore.edit { prefs -> prefs[selectedKeyId] = id }
    }

    suspend fun clearSelectedKeyId() {
        context.dataStore.edit { prefs -> prefs.remove(selectedKeyId) }
    }

    suspend fun clearEncodedKey() {
        context.dataStore.edit { prefs -> prefs.remove(keyEncoded) }
    }

    companion object {
        /** Decode all-in-one key: base64(json({url, token})) */
        fun decodeApiKey(encoded: String): DecodedConfig? {
            if (encoded.isBlank()) return null
            return try {
                val json = String(Base64.decode(encoded, Base64.DEFAULT))
                val obj = JSONObject(json)
                val url = obj.getString("url")
                val token = obj.getString("token")
                if (url.isBlank() || token.isBlank()) null
                else DecodedConfig(url, token)
            } catch (_: Exception) {
                null
            }
        }
    }
}
