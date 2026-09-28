package com.lore.voicenotes.ui.history

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Text
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lore.voicenotes.data.local.TranscriptionEntity
import com.lore.voicenotes.ui.theme.LoreFailed
import com.lore.voicenotes.ui.theme.LoreProcessing
import com.lore.voicenotes.ui.theme.LoreQueued
import com.lore.voicenotes.ui.theme.LoreReady
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun HistoryScreen(
    onTranscriptionClick: (Long) -> Unit,
    viewModel: HistoryViewModel = hiltViewModel(),
) {
    val items by viewModel.transcriptions.collectAsStateWithLifecycle()
    val snackbarHostState = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        viewModel.messages.collect { msg -> snackbarHostState.showSnackbar(msg) }
    }

    Box(Modifier.fillMaxSize()) {
        if (items.isEmpty()) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("No transcriptions yet", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        } else {
            LazyColumn(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(horizontal = 16.dp),
            ) {
                item { Spacer(Modifier.height(16.dp)) }
                item {
                    Text("History", style = MaterialTheme.typography.headlineMedium)
                    Spacer(Modifier.height(16.dp))
                }
                items(items, key = { it.id }) { entry ->
                    TranscriptionRow(
                        entry = entry,
                        onClick = { onTranscriptionClick(entry.id) },
                        onDelete = {
                            viewModel.delete(entry.id)
                            scope.launch {
                                val result = snackbarHostState.showSnackbar(
                                    message = "Deleted: ${entry.title}",
                                    actionLabel = "Undo",
                                    duration = SnackbarDuration.Short,
                                )
                                if (result == SnackbarResult.ActionPerformed) {
                                    // Undo not implemented — would need soft-delete
                                }
                            }
                        },
                        onResend = { viewModel.resend(entry.id) },
                    )
                }
            }
        }
        SnackbarHost(snackbarHostState, modifier = Modifier.align(Alignment.BottomCenter))
    }
}

@Composable
private fun TranscriptionRow(
    entry: TranscriptionEntity,
    onClick: () -> Unit,
    onDelete: () -> Unit,
    onResend: () -> Unit,
) {
    val dismissState = rememberSwipeToDismissBoxState(
        confirmValueChange = { value ->
            when (value) {
                // Swipe left → delete (dismiss the row).
                SwipeToDismissBoxValue.EndToStart -> { onDelete(); true }
                // Swipe right → re-send local audio; snap back (don't dismiss).
                SwipeToDismissBoxValue.StartToEnd -> { onResend(); false }
                else -> false
            }
        }
    )

    SwipeToDismissBox(
        state = dismissState,
        backgroundContent = {
            val deleting = dismissState.dismissDirection == SwipeToDismissBoxValue.EndToStart
            Box(
                Modifier
                    .fillMaxSize()
                    .padding(vertical = 4.dp),
                contentAlignment = if (deleting) Alignment.CenterEnd else Alignment.CenterStart,
            ) {
                Text(
                    if (deleting) "Delete" else "Re-send",
                    color = if (deleting) LoreFailed else LoreReady,
                    modifier = Modifier.padding(
                        start = if (deleting) 0.dp else 16.dp,
                        end = if (deleting) 16.dp else 0.dp,
                    ),
                )
            }
        },
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable(onClick = onClick)
                .padding(vertical = 12.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(entry.title, style = MaterialTheme.typography.bodyLarge)
                Text(
                    formatDate(entry.createdAt),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            StatusChip(entry.status)
        }
    }
}

@Composable
private fun StatusChip(status: String) {
    val (label, color) = when (status) {
        TranscriptionEntity.STATUS_PENDING_UPLOAD -> "On device" to LoreQueued
        TranscriptionEntity.STATUS_QUEUED -> "Queued" to LoreQueued
        TranscriptionEntity.STATUS_PROCESSING -> "Processing" to LoreProcessing
        TranscriptionEntity.STATUS_READY -> "Recognized" to LoreReady
        TranscriptionEntity.STATUS_FAILED, "error" -> "Not recognized" to LoreFailed
        else -> status to Color.Gray
    }
    Text(
        text = label,
        style = MaterialTheme.typography.labelSmall,
        color = color,
    )
}

private fun formatDate(millis: Long): String {
    val sdf = SimpleDateFormat("dd MMM yyyy, HH:mm", Locale.getDefault())
    return sdf.format(Date(millis))
}
