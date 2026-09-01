package win.moviora.udl

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import java.net.HttpURLConnection
import java.net.URL

private const val TERMUX_PACKAGE = "com.termux"
private const val TERMUX_HOME = "/data/data/com.termux/files/home"
private const val SERVER_SCRIPT_PATH = "$TERMUX_HOME/udl/server.py"
private const val SERVER_WORKDIR = "$TERMUX_HOME/udl"
const val LOCAL_SERVER_BASE = "http://127.0.0.1:47831"

/**
 * Obsluhuje komunikaci s Termuxem - appka sama nemá zabudovaný Python ani
 * ffmpeg (na rozdíl od dřívějšího Chaquopy pokusu), místo toho jen řekne
 * Termuxu (přes oficiální RUN_COMMAND intent), ať spustí náš server.py.
 * Termux má skutečný, plnohodnotný yt-dlp i ffmpeg - stejné jako na PC.
 */
object TermuxController {

    fun isTermuxInstalled(context: Context): Boolean {
        return try {
            context.packageManager.getPackageInfo(TERMUX_PACKAGE, 0)
            true
        } catch (e: PackageManager.NameNotFoundException) {
            false
        }
    }

    /**
     * Pošle Termuxu příkaz, ať spustí server.py na pozadí. Nic nevrací -
     * jestli to reálně naběhlo, zjistíme až polling na [pingServer].
     * EXTRA_BACKGROUND=true znamená, že Termux běží příkaz jako svou
     * vlastní service na pozadí (žádné vyskakovací terminálové okno,
     * žádná potřeba "Zobrazit přes jiné aplikace" oprávnění).
     */
    fun startServer(context: Context) {
        val intent = Intent().apply {
            setClassName(TERMUX_PACKAGE, "com.termux.app.RunCommandService")
            action = "com.termux.RUN_COMMAND"
            putExtra("com.termux.RUN_COMMAND_PATH", SERVER_SCRIPT_PATH)
            putExtra("com.termux.RUN_COMMAND_ARGUMENTS", arrayOf<String>())
            putExtra("com.termux.RUN_COMMAND_WORKDIR", SERVER_WORKDIR)
            putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
            putExtra("com.termux.RUN_COMMAND_COMMAND_LABEL", "UniversalDownloader server")
        }
        try {
            context.startService(intent)
        } catch (e: Exception) {
            // Nejcastejsi pricina: chybi RUN_COMMAND opravneni, nebo neni
            // zapnute allow-external-apps v termux.properties - obojí
            // resi README (jednorazove nastaveni pri instalaci).
        }
    }

    /** Posledni chyba z pingServer() - pro diagnostiku v UI, protoze jinak
     *  bychom videli jen "nefunguje to" bez jakekoliv informace proc. */
    @Volatile
    var lastPingError: String? = null
        private set

    /**
     * Zeptá se lokálního serveru (běžícího uvnitř Termuxu), jestli odpovídá.
     * Volá [callback] na hlavním vlákně s true/false.
     */
    fun pingServer(timeoutMs: Int = 3000, callback: (Boolean) -> Unit) {
        val mainHandler = Handler(Looper.getMainLooper())
        Thread {
            val reachable = try {
                val url = URL("$LOCAL_SERVER_BASE/api/ping")
                val connection = url.openConnection() as HttpURLConnection
                connection.connectTimeout = timeoutMs
                connection.readTimeout = timeoutMs
                connection.requestMethod = "GET"
                val code = connection.responseCode
                connection.disconnect()
                if (code != 200) {
                    lastPingError = "HTTP $code"
                } else {
                    lastPingError = null
                }
                code == 200
            } catch (e: Exception) {
                lastPingError = "${e.javaClass.simpleName}: ${e.message}"
                false
            }
            mainHandler.post { callback(reachable) }
        }.start()
    }

    /**
     * Opakovaně zkouší [pingServer], dokud server neodpoví nebo nevyprší
     * časový limit - používá se po [startServer], protože Python/Flask
     * potřebuje pár set milisekund na naběhnutí.
     */
    fun waitForServer(maxAttempts: Int = 20, intervalMs: Long = 500, onResult: (Boolean) -> Unit) {
        val mainHandler = Handler(Looper.getMainLooper())
        var attempt = 0

        fun attemptPing() {
            pingServer { reachable ->
                if (reachable) {
                    onResult(true)
                } else {
                    attempt++
                    if (attempt >= maxAttempts) {
                        onResult(false)
                    } else {
                        mainHandler.postDelayed({ attemptPing() }, intervalMs)
                    }
                }
            }
        }
        attemptPing()
    }
}
