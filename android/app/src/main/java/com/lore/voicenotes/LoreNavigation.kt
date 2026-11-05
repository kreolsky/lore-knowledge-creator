package com.lore.voicenotes

import androidx.annotation.StringRes
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavDestination.Companion.hasRoute
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.toRoute
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.lore.voicenotes.ui.NavBadgeState
import com.lore.voicenotes.ui.NavBadgeViewModel
import com.lore.voicenotes.ui.history.HistoryScreen
import com.lore.voicenotes.ui.history.PlayerBar
import com.lore.voicenotes.ui.history.PlayerBarViewModel
import com.lore.voicenotes.ui.history.TranscriptionDetailScreen
import com.lore.voicenotes.ui.record.RecordScreen
import com.lore.voicenotes.ui.settings.SettingsScreen
import com.lore.voicenotes.ui.theme.LoreAccentText
import com.lore.voicenotes.ui.theme.LoreBg
import com.lore.voicenotes.ui.theme.LoreRed
import kotlinx.serialization.Serializable

// Type-safe navigation routes
@Serializable object RecordRoute
@Serializable object HistoryRoute
@Serializable object SettingsRoute
@Serializable data class DetailRoute(val transcriptionId: Long)

private data class BottomNavItem(
    @StringRes val labelRes: Int,
    val icon: ImageVector,
    val route: Any,
)

private val bottomNavItems = listOf(
    BottomNavItem(R.string.nav_record, Icons.Default.Mic, RecordRoute),
    BottomNavItem(R.string.nav_history, Icons.Default.History, HistoryRoute),
    BottomNavItem(R.string.nav_settings, Icons.Default.Settings, SettingsRoute),
)

@Composable
fun LoreNavigation() {
    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentDestination = backStackEntry?.destination

    val badgeViewModel: NavBadgeViewModel = hiltViewModel()
    val badge by badgeViewModel.badge.collectAsStateWithLifecycle()

    fun navigateTo(route: Any) {
        navController.navigate(route) {
            popUpTo(navController.graph.findStartDestination().id) {
                saveState = true
            }
            launchSingleTop = true
            restoreState = true
        }
    }

    // Navigation stays visible on the detail screen too; the player bar above
    // it hides only there when that row is what it plays.
    val detailEntryId = backStackEntry
        ?.takeIf { it.destination.hasRoute<DetailRoute>() }
        ?.toRoute<DetailRoute>()
        ?.transcriptionId

    Scaffold(
        bottomBar = {
            Column {
                PlayerBarHost(hiddenOnDetailId = detailEntryId)
                NavigationBar {
                    bottomNavItems.forEach { item ->
                        val selected = when (item.route) {
                            is RecordRoute -> currentDestination?.hasRoute<RecordRoute>() == true
                            is HistoryRoute -> currentDestination?.hasRoute<HistoryRoute>() == true
                            is SettingsRoute -> currentDestination?.hasRoute<SettingsRoute>() == true
                            else -> false
                        }
                        val label = stringResource(item.labelRes)
                        NavigationBarItem(
                            selected = selected,
                            onClick = { navigateTo(item.route) },
                            icon = {
                                if (item.route is HistoryRoute) {
                                    BadgedBox(
                                        badge = { HistoryTabBadge(badge) },
                                    ) {
                                        Icon(item.icon, contentDescription = label)
                                    }
                                } else {
                                    Icon(item.icon, contentDescription = label)
                                }
                            },
                            label = { Text(label) },
                        )
                    }
                }
            }
        }
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = RecordRoute,
            modifier = Modifier.padding(innerPadding),
        ) {
            composable<RecordRoute> {
                RecordScreen(
                    onOpenHistory = { navigateTo(HistoryRoute) },
                )
            }
            composable<HistoryRoute> {
                HistoryScreen(
                    onTranscriptionClick = { id ->
                        navController.navigate(DetailRoute(id))
                    }
                )
            }
            composable<SettingsRoute> { SettingsScreen() }
            composable<DetailRoute> { backStack ->
                val route = backStack.toRoute<DetailRoute>()
                TranscriptionDetailScreen(
                    transcriptionId = route.transcriptionId,
                    onBack = { navController.popBackStack() },
                )
            }
        }
    }
}

/**
 * The player bar over navigation on every tab. Scoped to the activity, so it
 * survives tab switches; it hides only on the detail screen of its own row.
 */
@Composable
private fun PlayerBarHost(hiddenOnDetailId: Long?) {
    val viewModel: PlayerBarViewModel = hiltViewModel()
    val state by viewModel.state.collectAsStateWithLifecycle()
    if (state.entryId != null && state.entryId != hiddenOnDetailId) {
        PlayerBar(
            state = state,
            onPlayPause = viewModel::playPause,
            onSeek = viewModel::seek,
            onClose = viewModel::close,
        )
    }
}

/**
 * History tab marker: a red count while rows are upload_failed, else an
 * accent dot while an unsent row has a live key. Uploads otherwise run
 * invisibly in the background.
 */
@Composable
private fun HistoryTabBadge(state: NavBadgeState) {
    if (state.failedCount > 0) {
        Box(
            Modifier
                .offset(x = (-2).dp, y = (-4).dp)
                .defaultMinSize(minWidth = 16.dp, minHeight = 16.dp)
                .background(LoreRed, CircleShape)
                .border(2.dp, MaterialTheme.colorScheme.surface, CircleShape)
                .padding(horizontal = 4.dp),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                state.failedCount.toString(),
                fontSize = 10.sp,
                fontWeight = FontWeight.W800,
                color = LoreBg,
            )
        }
    } else if (state.sending) {
        Box(
            Modifier
                .offset(x = (-1).dp, y = (-3).dp)
                .size(7.dp)
                .background(LoreAccentText, CircleShape)
                .border(2.dp, MaterialTheme.colorScheme.surface, CircleShape)
        )
    }
}
