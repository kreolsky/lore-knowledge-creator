package com.lore.voicenotes.data.remote

import com.lore.voicenotes.data.local.DecodedConfig
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.Response
import javax.inject.Inject
import javax.inject.Singleton

/**
 * The one key seam: rewrites the dummy base URL to the tag's server and adds
 * the Bearer token. Throws when the tag is absent — a request without a key is
 * a caller bug, never a silent unauthenticated call.
 */
@Singleton
class KeyInterceptor @Inject constructor() : Interceptor {

    override fun intercept(chain: Interceptor.Chain): Response {
        val config = chain.request().tag(DecodedConfig::class.java)
            ?: throw IllegalStateException("Request carries no DecodedConfig tag")

        val base = config.url.trimEnd('/')
        val original = chain.request().url
        val newUrl = (base + original.encodedPath).toHttpUrlOrNull()
            ?: throw IllegalStateException("Undecodable key URL: ${config.url}")

        val request = chain.request().newBuilder()
            .url(newUrl.newBuilder().query(original.encodedQuery).build())
            .header("Authorization", "Bearer ${config.token}")
            .build()
        return chain.proceed(request)
    }
}
