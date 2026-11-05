package com.lore.voicenotes.data.remote

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

@JsonClass(generateAdapter = true)
data class UploadResponse(
    @Json(name = "reference_id") val referenceId: String,
    val status: String,
)

@JsonClass(generateAdapter = true)
data class StatusResponse(
    @Json(name = "reference_id") val referenceId: String,
    val status: String?,
    val content: String?,
)

@JsonClass(generateAdapter = true)
data class WidgetInfoResponse(
    @Json(name = "project_id") val projectId: String?,
    @Json(name = "project_name") val projectName: String?,
    @Json(name = "document_id") val documentId: String?,
    @Json(name = "document_title") val documentTitle: String?,
)
