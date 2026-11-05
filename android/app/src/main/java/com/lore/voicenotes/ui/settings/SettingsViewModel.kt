package com.lore.voicenotes.ui.settings

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.lore.voicenotes.R
import com.lore.voicenotes.data.local.DocumentKeyEntity
import com.lore.voicenotes.data.repository.AddKeyResult
import com.lore.voicenotes.data.repository.KeyRepository
import com.lore.voicenotes.worker.PollTranscriptionWorker
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

data class KeyRow(
    val id: Long,
    val position: Int,
    val title: String?,
    val project: String?,
    val selected: Boolean,
)

data class UiMessage(val text: String, val isError: Boolean)

/** Settings UI state: the live key list, the selection, and the add-key flow. */
@HiltViewModel
class SettingsViewModel @Inject constructor(
    private val keyRepository: KeyRepository,
    @ApplicationContext private val appContext: Context,
) : ViewModel() {

    val keys: StateFlow<List<KeyRow>> = combine(
        keyRepository.liveKeys,
        keyRepository.selectedKeyId,
    ) { live, selectedId ->
        val effectiveSelected = if (live.any { it.id == selectedId }) selectedId else live.firstOrNull()?.id
        live.map { it.toRow(effectiveSelected) }
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    private val _addField = MutableStateFlow("")
    val addField: StateFlow<String> = _addField.asStateFlow()

    private val _message = MutableStateFlow<UiMessage?>(null)
    val message: StateFlow<UiMessage?> = _message.asStateFlow()

    fun updateAddField(value: String) {
        _addField.value = value
        _message.value = null
    }

    fun clearMessage() {
        _message.value = null
    }

    fun addKey() {
        val encoded = _addField.value
        if (encoded.isBlank()) return
        viewModelScope.launch {
            when (val result = keyRepository.addKey(encoded)) {
                is AddKeyResult.Added -> {
                    _addField.value = ""
                    _message.value = UiMessage(appContext.getString(R.string.settings_key_added), false)
                    // A restored key's queued rows are pollable again
                    PollTranscriptionWorker.enqueue(appContext)
                }
                is AddKeyResult.Selected ->
                    _message.value = UiMessage(appContext.getString(R.string.settings_key_duplicate), false)
                AddKeyResult.Invalid ->
                    _message.value = UiMessage(appContext.getString(R.string.settings_key_invalid), true)
            }
        }
    }

    fun select(id: Long) {
        viewModelScope.launch { keyRepository.setSelected(id) }
    }

    fun delete(id: Long) {
        viewModelScope.launch { keyRepository.deleteKey(id) }
    }

    fun moveUp(id: Long) {
        viewModelScope.launch { keyRepository.moveUp(id) }
    }

    fun moveDown(id: Long) {
        viewModelScope.launch { keyRepository.moveDown(id) }
    }

    private fun DocumentKeyEntity.toRow(selectedId: Long?) = KeyRow(
        id = id,
        position = position ?: 0,
        title = documentTitle,
        project = projectName,
        selected = id == selectedId,
    )
}
