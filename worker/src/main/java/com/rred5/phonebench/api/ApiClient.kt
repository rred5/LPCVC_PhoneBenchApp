package com.rred5.phonebench.api

import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/** The server answered with an error (docs/API.md, "Errors and retries"). */
class ApiException(
    val status: Int,
    val code: String,
    message: String,
) : Exception(message)

/**
 * The phone's side of docs/API.md. Blocking calls: run them on Dispatchers.IO.
 * Network failures surface as IOException so callers can retry with backoff.
 */
class ApiClient(
    baseUrl: String,
    private val deviceKey: String,
    private val appVersion: String,
) {
    private val base = URL(baseUrl.trimEnd('/') + "/")

    fun poll(body: JSONObject): JSONObject = postJson("api/v1/device/poll", body)

    fun reportRun(runId: String, body: JSONObject): JSONObject = postJson("api/v1/device/runs/${segment(runId)}", body)

    fun finish(unitId: String, status: String, error: String?): JSONObject =
        postJson(
            "api/v1/device/units/${segment(unitId)}/finish",
            JSONObject().put("status", status).put("error", error ?: JSONObject.NULL),
        )

    /**
     * Download an image the server named in a unit. Only same-server paths are asked for; a cloud
     * server answers with one redirect to an https file-storage URL, fetched without the device key.
     * The caller checks the SHA-256 from the unit either way.
     */
    fun downloadImage(path: String, destination: File, maxBytes: Long) {
        require(path.startsWith("/api/v1/device/images/") && !path.contains("..")) { "Image URL is not on this server: $path" }
        var connection = open(path.removePrefix("/"), "GET")
        try {
            if (connection.responseCode in REDIRECTS) {
                val location = connection.getHeaderField("Location") ?: throw IOException("Image redirect has no location")
                val target = URL(connection.url, location)
                require(target.protocol == "https") { "Image redirect is not https: ${target.protocol}" }
                connection.disconnect()
                connection = (target.openConnection() as HttpURLConnection).apply {
                    connectTimeout = 15_000
                    readTimeout = 60_000
                    instanceFollowRedirects = false
                }
                if (connection.responseCode in REDIRECTS) throw IOException("Image storage redirected again")
            }
            check(connection)
            connection.inputStream.use { input ->
                destination.outputStream().use { output ->
                    val buffer = ByteArray(64 * 1024)
                    var total = 0L
                    while (true) {
                        val read = input.read(buffer)
                        if (read < 0) break
                        total += read
                        if (total > maxBytes) throw IOException("Image is larger than $maxBytes bytes")
                        output.write(buffer, 0, read)
                    }
                }
            }
        } finally {
            connection.disconnect()
        }
    }

    private companion object {
        val REDIRECTS = setOf(301, 302, 303, 307, 308)
    }

    private fun segment(id: String) = URLEncoder.encode(id, "UTF-8")

    private fun open(path: String, method: String): HttpURLConnection {
        val connection = URL(base, path).openConnection() as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = 15_000
        connection.readTimeout = 60_000
        connection.instanceFollowRedirects = false
        connection.setRequestProperty("Authorization", "Bearer $deviceKey")
        connection.setRequestProperty("PhoneBench-App-Version", appVersion)
        connection.setRequestProperty("Accept", "application/json")
        return connection
    }

    private fun postJson(path: String, body: JSONObject): JSONObject {
        val connection = open(path, "POST")
        try {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            check(connection)
            return JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
        } finally {
            connection.disconnect()
        }
    }

    private fun check(connection: HttpURLConnection) {
        val status = connection.responseCode
        if (status < 400) return
        val text = connection.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
        val error = runCatching { JSONObject(text).getJSONObject("error") }.getOrNull()
        throw ApiException(status, error?.optString("code") ?: "http_$status", error?.optString("message") ?: "HTTP $status")
    }
}
