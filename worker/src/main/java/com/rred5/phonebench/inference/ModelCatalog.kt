package com.rred5.phonebench.inference

import android.content.Context
import org.json.JSONArray

/** A vision model this app can download and run (assets/models.json). */
data class CatalogModel(
    val id: String,
    /** `org/repo` as the GenieX model manager expects. */
    val modelName: String,
    val quant: String?,
    val hub: String,
    val runtime: String,
    val chipset: String?,
    val backends: Set<Backend>,
)

class ModelCatalog(val models: List<CatalogModel>) {
    fun find(id: String): CatalogModel? = models.firstOrNull { it.id == id }

    companion object {
        fun load(context: Context): ModelCatalog {
            val json = JSONArray(context.assets.open("models.json").bufferedReader().use { it.readText() })
            val models =
                (0 until json.length()).map { index ->
                    val item = json.getJSONObject(index)
                    val backends = item.getJSONArray("backends")
                    CatalogModel(
                        id = item.getString("id"),
                        modelName = item.getString("modelName"),
                        quant = item.optString("quant").ifEmpty { null },
                        hub = item.optString("hub", "AUTO"),
                        runtime = item.getString("runtime"),
                        chipset = item.optString("chipset").ifEmpty { null },
                        backends = (0 until backends.length()).map { Backend.valueOf(backends.getString(it)) }.toSet(),
                    )
                }
            return ModelCatalog(models)
        }
    }
}
