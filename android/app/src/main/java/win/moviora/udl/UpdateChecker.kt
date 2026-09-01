package win.moviora.udl

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

private const val RELEASES_API =
    "https://api.github.com/repos/LukasJef/UniversalDownloader/releases/latest"

/** Vysledek kontroly aktualizaci, predavany zpet do UI vrstvy. */
sealed class UpdateResult {
    /** Uz mame nejnovejsi verzi. */
    data class UpToDate(val version: String) : UpdateResult()
    /** Je k dispozici novejsi verze; instalace uz byla spustena. */
    data class Installing(val version: String) : UpdateResult()
    /** Cokoliv se pokazilo - sit, chybejici APK v release, opravneni... */
    data class Failed(val message: String) : UpdateResult()
}

/**
 * Zjisti, jestli je na GitHubu novejsi release nez nainstalovana verze, a
 * pokud ano, stahne jeho APK a preda ho systemovemu instalatoru.
 *
 * Poznamka k opravnenim: Android nedovoli appce nainstalovat jinou appku bez
 * REQUEST_INSTALL_PACKAGES, ktere musi uzivatel rucne povolit v nastaveni.
 * Kdyz chybi, otevreme rovnou tu spravnou systemovou obrazovku - stejne to
 * resi treba F-Droid.
 */
object UpdateChecker {

    private val mainHandler = Handler(Looper.getMainLooper())

    fun installedVersion(context: Context): String {
        return try {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "0"
        } catch (e: Exception) {
            "0"
        }
    }

    fun checkAndInstall(context: Context, onResult: (UpdateResult) -> Unit) {
        Thread {
            val result = try {
                doCheckAndInstall(context)
            } catch (e: Exception) {
                UpdateResult.Failed("${e.javaClass.simpleName}: ${e.message}")
            }
            mainHandler.post { onResult(result) }
        }.start()
    }

    private fun doCheckAndInstall(context: Context): UpdateResult {
        val json = httpGet(RELEASES_API) ?: return UpdateResult.Failed("Couldn't reach GitHub.")
        val release = JSONObject(json)

        // Tag byva "v1.1.1" - cislo verze je vsechno za pripadnym "v".
        val latest = release.optString("tag_name").removePrefix("v").trim()
        if (latest.isEmpty()) return UpdateResult.Failed("No release tag found.")

        val current = installedVersion(context)
        if (compareVersions(latest, current) <= 0) {
            return UpdateResult.UpToDate(current)
        }

        val assets = release.optJSONArray("assets")
            ?: return UpdateResult.Failed("Release $latest has no files attached.")
        var apkUrl: String? = null
        for (i in 0 until assets.length()) {
            val asset = assets.optJSONObject(i) ?: continue
            val name = asset.optString("name")
            if (name.startsWith("UniversalDownloader-") && name.endsWith(".apk")) {
                apkUrl = asset.optString("browser_download_url")
                break
            }
        }
        if (apkUrl.isNullOrEmpty()) {
            return UpdateResult.Failed("Release $latest doesn't include an APK.")
        }

        val apkFile = downloadApk(context, apkUrl, latest)
            ?: return UpdateResult.Failed("Downloading the APK failed.")

        // Bez tohohle opravneni by systemovy instalator jen tise nic neudelal,
        // tak uzivatele rovnou posleme na spravnou obrazovku.
        if (!context.packageManager.canRequestPackageInstalls()) {
            val settingsIntent = Intent(
                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:${context.packageName}"),
            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(settingsIntent)
            return UpdateResult.Failed(
                "Allow installing apps from UniversalDownloader, then press Update app again."
            )
        }

        val apkUri = FileProvider.getUriForFile(
            context, "${context.packageName}.fileprovider", apkFile
        )
        val installIntent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(apkUri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(installIntent)
        return UpdateResult.Installing(latest)
    }

    private fun httpGet(url: String): String? {
        val connection = URL(url).openConnection() as HttpURLConnection
        return try {
            connection.connectTimeout = 10_000
            connection.readTimeout = 10_000
            connection.setRequestProperty("Accept", "application/vnd.github+json")
            connection.setRequestProperty("User-Agent", "UniversalDownloader")
            if (connection.responseCode != 200) null
            else connection.inputStream.bufferedReader().use { it.readText() }
        } finally {
            connection.disconnect()
        }
    }

    private fun downloadApk(context: Context, url: String, version: String): File? {
        val dir = File(context.getExternalFilesDir(null), "updates").apply { mkdirs() }
        // Stare stazene APK uz nepotrebujeme - jinak by se tu hromadily.
        dir.listFiles()?.forEach { it.delete() }
        val target = File(dir, "UniversalDownloader-$version.apk")

        val connection = URL(url).openConnection() as HttpURLConnection
        return try {
            connection.connectTimeout = 15_000
            connection.readTimeout = 60_000
            connection.instanceFollowRedirects = true
            connection.setRequestProperty("User-Agent", "UniversalDownloader")
            if (connection.responseCode != 200) return null
            connection.inputStream.use { input ->
                target.outputStream().use { output -> input.copyTo(output) }
            }
            target
        } catch (e: Exception) {
            null
        } finally {
            connection.disconnect()
        }
    }

    /** Vrati >0 kdyz je [a] novejsi nez [b], 0 pri shode, <0 kdyz starsi. */
    private fun compareVersions(a: String, b: String): Int {
        val partsA = a.split(".").map { it.filter(Char::isDigit).toIntOrNull() ?: 0 }
        val partsB = b.split(".").map { it.filter(Char::isDigit).toIntOrNull() ?: 0 }
        for (i in 0 until maxOf(partsA.size, partsB.size)) {
            val diff = (partsA.getOrElse(i) { 0 }) - (partsB.getOrElse(i) { 0 })
            if (diff != 0) return diff
        }
        return 0
    }
}
