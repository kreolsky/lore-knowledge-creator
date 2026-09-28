package com.lore.voicenotes

import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.navigation.NavDestination.Companion.hasRoute
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.toRoute
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.lore.voicenotes.ui.history.HistoryScreen
import com.lore.voicenotes.ui.history.TranscriptionDetailScreen
import com.lore.voicenotes.ui.record.RecordScreen
import com.lore.voicenotes.ui.settings.SettingsScreen
import kotlinx.serialization.Serializable

// Type-safe navigation routes
@Serializable object RecordRoute
@Serializable object HistoryRoute
@Serializable object SettingsRoute
@Serializable data class DetailRoute(val transcriptionId: Long)

private data class BottomNavItem(
    val label: String,
    val icon: ImageVector,
    val route: Any,
)

private val bottomNavItems = listOf(
    BottomNavItem("Record", Icons.Default.Mic, RecordRoute),
    BottomNavItem("History", Icons.Default.History, HistoryRoute),
    BottomNavItem("Settings", Icons.Default.Settings, SettingsRoute),
)

@Composable
fun LoreNavigation() {
    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentDestination = backStackEntry?.destination

    // Hide bottom bar on detail screen
    val showBottomBar = currentDestination?.hasRoute<DetailRoute>() != true

    Scaffold(
        bottomBar = {
            if (showBottomBar) {
                NavigationBar {
                    bottomNavItems.forEach { item ->
                        val selected = when (item.route) {
                            is RecordRoute -> currentDestination?.hasRoute<RecordRoute>() == true
                            is HistoryRoute -> currentDestination?.hasRoute<HistoryRoute>() == true
                            is SettingsRoute -> currentDestination?.hasRoute<SettingsRoute>() == true
                            else -> false
                        }
                        NavigationBarItem(
                            selected = selected,
                            onClick = {
                                navController.navigate(item.route) {
                                    popUpTo(navController.graph.findStartDestination().id) {
                                        saveState = true
                                    }
                                    launchSingleTop = true
                                    restoreState = true
                                }
                            },
                            icon = { Icon(item.icon, contentDescription = item.label) },
                            label = { Text(item.label) },
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
            composable<RecordRoute> { RecordScreen() }
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
