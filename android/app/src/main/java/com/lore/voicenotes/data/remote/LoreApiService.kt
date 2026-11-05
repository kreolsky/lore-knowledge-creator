package com.lore.voicenotes.data.remote

import com.lore.voicenotes.data.local.DecodedConfig
import okhttp3.MultipartBody
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path
import retrofit2.http.Tag

/**
 * Every call carries its key's [DecodedConfig] as a Retrofit @Tag; KeyInterceptor
 * turns the tag into the base URL and the Bearer header. Callers check for a
 * live key before calling — a missing tag is a programming error.
 */
interface LoreApiService {

    @Multipart
    @POST("/api/widget/upload")
    suspend fun upload(@Tag config: DecodedConfig, @Part file: MultipartBody.Part): UploadResponse

    @GET("/api/widget/status/{referenceId}")
    suspend fun status(@Tag config: DecodedConfig, @Path("referenceId") referenceId: String): StatusResponse

    @GET("/api/widget/info")
    suspend fun info(@Tag config: DecodedConfig): WidgetInfoResponse
}
