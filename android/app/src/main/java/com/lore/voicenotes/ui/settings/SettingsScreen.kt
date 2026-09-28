package com.lore.voicenotes.ui.settings

import android.content.ClipboardManager
import android.content.Context
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ContentPaste
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle

@Composable
fun SettingsScreen(viewModel: SettingsViewModel = hiltViewModel()) {
    val encodedKey by viewModel.encodedKey.collectAsStateWithLifecycle()
    val decoded by viewModel.decoded.collectAsStateWithLifecycle()
    val target by viewModel.target.collectAsStateWithLifecycle()
    val targetError by viewModel.targetError.collectAsStateWithLifecycle()
    val context = LocalContext.current

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineMedium)

        Spacer(Modifier.height(24.dp))

        Text("API Key", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(8.dp))

        OutlinedTextField(
            value = encodedKey,
            onValueChange = { viewModel.updateKey(it) },
            label = { Text("Encoded API key") },
            placeholder = { Text("Paste base64-encoded key...") },
            trailingIcon = {
                IconButton(onClick = {
                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    val clip = clipboard.primaryClip
                    if (clip != null && clip.itemCount > 0) {
                        val text = clip.getItemAt(0).text?.toString() ?: return@IconButton
                        viewModel.updateKey(text)
                    }
                }) {
                    Icon(Icons.Default.ContentPaste, contentDescription = "Paste")
                }
            },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        Spacer(Modifier.height(16.dp))

        if (decoded != null) {
            val host = try {
                java.net.URI(decoded!!.url).host ?: decoded!!.url
            } catch (_: Exception) {
                decoded!!.url
            }
            Text(
                "Connected to: $host",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.tertiary,
            )
            val info = target
            if (info != null) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Project: ${info.projectName ?: "—"}",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                Text(
                    "Document: ${info.documentTitle ?: "—"}",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                )
            } else if (targetError != null) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Could not verify key: $targetError",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        } else if (encodedKey.isNotBlank()) {
            Text(
                "Invalid key format",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.error,
            )
        }
    }
}
