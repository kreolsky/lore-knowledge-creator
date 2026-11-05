package com.lore.voicenotes.di

import android.content.Context
import androidx.room.Room
import com.lore.voicenotes.data.local.AppDatabase
import com.lore.voicenotes.data.local.DocumentKeyDao
import com.lore.voicenotes.data.local.TranscriptionDao
import com.lore.voicenotes.data.remote.KeyInterceptor
import com.lore.voicenotes.data.remote.LoreApiService
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import com.lore.voicenotes.BuildConfig
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object AppModule {

    @Provides
    @Singleton
    fun provideDatabase(@ApplicationContext context: Context): AppDatabase =
        Room.databaseBuilder(context, AppDatabase::class.java, "lore_voice.db")
            .addMigrations(AppDatabase.MIGRATION_1_2, AppDatabase.MIGRATION_2_3)
            .build()

    @Provides
    fun provideTranscriptionDao(db: AppDatabase): TranscriptionDao = db.transcriptionDao()

    @Provides
    fun provideDocumentKeyDao(db: AppDatabase): DocumentKeyDao = db.documentKeyDao()

    @Provides
    @Singleton
    @ApplicationScope
    fun provideApplicationScope(): CoroutineScope =
        CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @Provides
    @Singleton
    fun provideOkHttp(keyInterceptor: KeyInterceptor): OkHttpClient = OkHttpClient.Builder()
        .addInterceptor(keyInterceptor)
        .addInterceptor(HttpLoggingInterceptor().apply {
            // BASIC, not BODY: BODY buffers the whole request in memory (voids
            // streaming) and prints the token.
            level = if (BuildConfig.DEBUG) HttpLoggingInterceptor.Level.BASIC else HttpLoggingInterceptor.Level.NONE
        })
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(1800, TimeUnit.SECONDS)
        .writeTimeout(1800, TimeUnit.SECONDS)
        .build()

    @Provides
    @Singleton
    fun provideMoshi(): Moshi = Moshi.Builder()
        .addLast(KotlinJsonAdapterFactory())
        .build()

    @Provides
    @Singleton
    fun provideRetrofit(client: OkHttpClient, moshi: Moshi): Retrofit = Retrofit.Builder()
        .baseUrl("http://localhost/") // Rewritten by KeyInterceptor from the @Tag
        .client(client)
        .addConverterFactory(MoshiConverterFactory.create(moshi))
        .build()

    @Provides
    @Singleton
    fun provideApiService(retrofit: Retrofit): LoreApiService =
        retrofit.create(LoreApiService::class.java)
}
