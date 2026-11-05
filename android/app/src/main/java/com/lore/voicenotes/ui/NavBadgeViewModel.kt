package com.lore.voicenotes.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.lore.voicenotes.data.repository.TranscriptionRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import javax.inject.Inject

data class NavBadgeState(val failedCount: Int = 0, val sending: Boolean = false)

/**
 * Failure signal for the History tab, visible on every screen: a red count
 * of upload_failed rows (excluding soft-deleted), with an accent dot while
 * an unsent row has a live key. The dot yields to the count.
 */
@HiltViewModel
class NavBadgeViewModel @Inject constructor(
    repository: TranscriptionRepository,
) : ViewModel() {

    val badge: StateFlow<NavBadgeState> = combine(
        repository.uploadFailedCount,
        repository.hasPendingUpload,
    ) { failed, sending -> NavBadgeState(failed, sending) }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), NavBadgeState())
}
