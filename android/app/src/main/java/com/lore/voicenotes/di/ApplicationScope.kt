package com.lore.voicenotes.di

import javax.inject.Qualifier

/** Application-lifetime coroutine scope: survives screen/service teardowns. */
@Qualifier
@Retention(AnnotationRetention.BINARY)
annotation class ApplicationScope
