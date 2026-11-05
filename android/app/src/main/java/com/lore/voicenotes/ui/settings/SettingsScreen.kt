package com.lore.voicenotes.ui.settings

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ContentPaste
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.lore.voicenotes.BuildConfig
import com.lore.voicenotes.R
import com.lore.voicenotes.data.repository.UploadLog
import com.lore.voicenotes.ui.theme.LoreAccent
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/** The upload journal, readable and copyable: the phone has no adb. */
@Composable
private fun UploadJournal() {
    val context = LocalContext.current
    val journal by produceState("") {
        value = withContext(Dispatchers.IO) { UploadLog.read(context) }
    }
    Spacer(Modifier.height(24.dp))
    Text(stringResource(R.string.settings_journal_title), style = MaterialTheme.typography.titleMedium)
    Spacer(Modifier.height(8.dp))
    SelectionContainer {
        Text(
            journal.ifEmpty { stringResource(R.string.settings_journal_empty) },
            style = MaterialTheme.typography.bodySmall,
            fontFamily = FontFamily.Monospace,
        )
    }
    Spacer(Modifier.height(8.dp))
    Button(
        enabled = journal.isNotEmpty(),
        onClick = {
            val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            clipboard.setPrimaryClip(ClipData.newPlainText("Lore Voice upload journal", journal))
        },
    ) {
        Text(stringResource(R.string.settings_journal_copy))
    }
}

@Composable
fun SettingsScreen(viewModel: SettingsViewModel = hiltViewModel()) {
    val keys by viewModel.keys.collectAsStateWithLifecycle()
    val addField by viewModel.addField.collectAsStateWithLifecycle()
    val message by viewModel.message.collectAsStateWithLifecycle()
    val context = LocalContext.current

    LaunchedEffect(message) {
        if (message != null) {
            delay(5000)
            viewModel.clearMessage()
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text(stringResource(R.string.settings_title), style = MaterialTheme.typography.headlineMedium)

        Spacer(Modifier.height(24.dp))

        Text(stringResource(R.string.settings_keys_title), style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(8.dp))

        if (keys.isEmpty()) {
            Text(
                stringResource(R.string.settings_no_keys),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            keys.forEachIndexed { index, row ->
                KeyListRow(
                    row = row,
                    isFirst = index == 0,
                    isLast = index == keys.lastIndex,
                    onClick = { viewModel.select(row.id) },
                    onDelete = { viewModel.delete(row.id) },
                    onMoveUp = { viewModel.moveUp(row.id) },
                    onMoveDown = { viewModel.moveDown(row.id) },
                )
                Spacer(Modifier.height(8.dp))
            }
        }

        Spacer(Modifier.height(16.dp))

        Text(stringResource(R.string.settings_add_title), style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(8.dp))

        OutlinedTextField(
            value = addField,
            onValueChange = { viewModel.updateAddField(it) },
            label = { Text(stringResource(R.string.settings_key_label)) },
            placeholder = { Text(stringResource(R.string.settings_key_placeholder)) },
            trailingIcon = {
                IconButton(onClick = {
                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    val clip = clipboard.primaryClip
                    if (clip != null && clip.itemCount > 0) {
                        val text = clip.getItemAt(0).text?.toString() ?: return@IconButton
                        viewModel.updateAddField(text)
                    }
                }) {
                    Icon(Icons.Default.ContentPaste, contentDescription = stringResource(R.string.settings_paste))
                }
            },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        Spacer(Modifier.height(8.dp))

        Button(
            onClick = { viewModel.addKey() },
            enabled = addField.isNotBlank(),
        ) {
            Text(stringResource(R.string.settings_add_key))
        }

        message?.let {
            Spacer(Modifier.height(12.dp))
            Text(
                it.text,
                style = MaterialTheme.typography.bodyMedium,
                color = if (it.isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        UploadJournal()
        Spacer(Modifier.height(24.dp))
        Text(
            stringResource(R.string.settings_version, BuildConfig.VERSION_NAME),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun KeyListRow(
    row: KeyRow,
    isFirst: Boolean,
    isLast: Boolean,
    onClick: () -> Unit,
    onDelete: () -> Unit,
    onMoveUp: () -> Unit,
    onMoveDown: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .border(
                width = if (row.selected) 1.dp else 0.dp,
                color = if (row.selected) LoreAccent else MaterialTheme.colorScheme.surface,
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            "%02d".format(row.position),
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.primary,
        )
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                row.title ?: stringResource(R.string.settings_no_value),
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurface,
            )
            if (row.project != null) {
                Text(
                    stringResource(R.string.settings_project, row.project),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        IconButton(onClick = onMoveUp, enabled = !isFirst) {
            Icon(Icons.Default.KeyboardArrowUp, contentDescription = stringResource(R.string.settings_move_up))
        }
        IconButton(onClick = onMoveDown, enabled = !isLast) {
            Icon(Icons.Default.KeyboardArrowDown, contentDescription = stringResource(R.string.settings_move_down))
        }
        IconButton(onClick = onDelete) {
            Icon(
                Icons.Default.Delete,
                contentDescription = stringResource(R.string.settings_delete_key),
                tint = MaterialTheme.colorScheme.error,
            )
        }
    }
}
