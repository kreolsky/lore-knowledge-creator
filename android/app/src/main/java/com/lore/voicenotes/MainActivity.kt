package com.lore.voicenotes

import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.lore.voicenotes.audio.PlayerController
import com.lore.voicenotes.ui.theme.LoreTheme
import com.lore.voicenotes.worker.PollTranscriptionWorker
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject

@AndroidEntryPoint
class MainActivity : ComponentActivity() {

    @Inject lateinit var playerController: PlayerController

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // WHY explicit dark: the default style follows the phone's light/dark setting,
        // and this app is dark only. On a light phone that paints a white scrim over
        // the navigation bar and dark status icons over a dark screen.
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        setContent {
            LoreTheme {
                LoreNavigation()
            }
        }
    }

    override fun onStart() {
        super.onStart()
        // REPLACE-enqueue: opening the app cancels any outstanding poll backoff
        // and refreshes pending transcriptions at once.
        PollTranscriptionWorker.enqueue(this)
    }

    override fun onStop() {
        super.onStop()
        // Playback pauses with the activity; background playback is not done.
        playerController.pause()
    }
}
