package com.lore.voicenotes.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.lore.voicenotes.data.local.DecodedConfig
import com.lore.voicenotes.data.local.SettingsDataStore
import com.lore.voicenotes.data.remote.LoreApiService
import com.lore.voicenotes.data.remote.WidgetInfoResponse
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

/** Settings UI state. Fetches the key's target project/document for display. */
@HiltViewModel
class SettingsViewModel @Inject constructor(
    private val settingsDataStore: SettingsDataStore,
    private val api: LoreApiService,
) : ViewModel() {

    val encodedKey: StateFlow<String> = settingsDataStore.encodedKeyFlow
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), "")

    private val _decoded = MutableStateFlow<DecodedConfig?>(null)
    val decoded: StateFlow<DecodedConfig?> = _decoded.asStateFlow()

    private val _target = MutableStateFlow<WidgetInfoResponse?>(null)
    val target: StateFlow<WidgetInfoResponse?> = _target.asStateFlow()

    /** null = idle/ok, non-null = could not verify the key against the server. */
    private val _targetError = MutableStateFlow<String?>(null)
    val targetError: StateFlow<String?> = _targetError.asStateFlow()

    init {
        viewModelScope.launch {
            settingsDataStore.configFlow.collect { config ->
                _decoded.value = config
                // Re-fetch the target each time a valid config arrives; clear on invalid key.
                if (config == null) {
                    _target.value = null
                    _targetError.value = null
                } else {
                    fetchTarget()
                }
            }
        }
    }

    private fun fetchTarget() {
        viewModelScope.launch {
            try {
                _target.value = api.info()
                _targetError.value = null
            } catch (e: Exception) {
                _target.value = null
                _targetError.value = e.message ?: "Could not verify key"
            }
        }
    }

    fun updateKey(encoded: String) {
        viewModelScope.launch {
            settingsDataStore.saveEncodedKey(encoded.trim())
        }
    }
}
