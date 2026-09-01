package win.moviora.udl

import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView

private val BackgroundDark = Color(0xFF090B10)
private val AccentIndigo = Color(0xFF6366F1)
private val TextLight = Color(0xFFE5E7EB)
private val TextMuted = Color(0xFF9CA3AF)

private sealed class ServerState {
    data object Connecting : ServerState()
    data object Starting : ServerState()
    data class Failed(val message: String) : ServerState()
    data object Ready : ServerState()
}

/**
 * Zajistí, že Termux server běží (spustí ho přes [TermuxController], pokud
 * ještě neběží), a pak zobrazí WebView se stejným index.html, co používá
 * desktopová appka a web verze - žádné vlastní Android UI navíc, jedno
 * sdílené rozhraní pro všechny platformy. Než se to podaří, appka ukazuje
 * (přes Compose) jednoduchou, ale vzhledově sladěnou stavovou obrazovku.
 */
class MainActivity : ComponentActivity() {

    private var pendingSharedUrl by mutableStateOf<String?>(null)
    private var state by mutableStateOf<ServerState>(ServerState.Connecting)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        pendingSharedUrl = intent?.getStringExtra("shared_url")

        setContent {
            Box(modifier = Modifier.fillMaxSize().background(BackgroundDark)) {
                when (val current = state) {
                    is ServerState.Ready -> UdlWebView(pendingSharedUrl)
                    is ServerState.Failed -> ErrorScreen(current.message)
                    else -> LoadingScreen(current)
                }
            }
        }

        ensureServer()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        pendingSharedUrl = intent.getStringExtra("shared_url")
        if (state !is ServerState.Ready) {
            ensureServer()
        }
        // Pokud uz server bezi, samotna zmena pendingSharedUrl vyvola
        // rekompozici a UdlWebView si nove URL nacte sam (viz update {} nize).
    }

    private fun ensureServer() {
        if (!TermuxController.isTermuxInstalled(this)) {
            state = ServerState.Failed(
                "Termux není nainstalovaný.\n\nNainstaluj ho podle návodu v README a zkus to znovu."
            )
            return
        }

        state = ServerState.Connecting
        TermuxController.pingServer { alreadyRunning ->
            if (alreadyRunning) {
                state = ServerState.Ready
            } else {
                state = ServerState.Starting
                // Bezi jen jako bonus pro ty, co si RUN_COMMAND opravneni
                // nastavili rucne (napr. pres adb) - normalne to nic
                // nedela/tise selze, protoze Android tohle opravneni
                // neumoznuje bezne udelit. Hlavni cesta je server bezici
                // uz na pozadi (viz setup.sh - auto-start pri otevreni Termuxu).
                TermuxController.startServer(this)
                TermuxController.waitForServer { success ->
                    state = if (success) {
                        ServerState.Ready
                    } else {
                        ServerState.Failed(
                            "Server v Termuxu neběží.\n\n" +
                                "Otevři appku Termux (stačí ji nechat na pozadí) - server " +
                                "se po jednorázovém setupu (viz README) spouští sám " +
                                "automaticky při každém otevření Termuxu.\n\n" +
                                "Detail chyby:\n" + (TermuxController.lastPingError ?: "(žádný)")
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun LoadingScreen(state: ServerState) {
    Column(
        modifier = Modifier.fillMaxSize().padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Box(
            modifier = Modifier
                .size(64.dp)
                .background(AccentIndigo.copy(alpha = 0.15f), shape = CircleShape),
            contentAlignment = Alignment.Center,
        ) {
            Text("⚡", fontSize = 28.sp)
        }
        Spacer(modifier = Modifier.height(24.dp))
        Text("UniversalDownloader", color = TextLight, fontSize = 20.sp, fontWeight = FontWeight.Bold)
        Spacer(modifier = Modifier.height(28.dp))
        CircularProgressIndicator(color = AccentIndigo, strokeWidth = 3.dp, modifier = Modifier.size(28.dp))
        Spacer(modifier = Modifier.height(20.dp))
        val message = when (state) {
            is ServerState.Starting -> "Spouštím lokální server v Termuxu…"
            else -> "Připojuji se k Termuxu…"
        }
        Text(message, color = TextMuted, fontSize = 14.sp, textAlign = TextAlign.Center)
    }
}

@Composable
private fun ErrorScreen(message: String) {
    Column(
        modifier = Modifier.fillMaxSize().padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Box(
            modifier = Modifier
                .size(64.dp)
                .background(Color(0x33F59E0B), shape = CircleShape),
            contentAlignment = Alignment.Center,
        ) {
            Text("⚠", fontSize = 28.sp)
        }
        Spacer(modifier = Modifier.height(20.dp))
        Text(
            message,
            color = TextLight,
            fontSize = 14.sp,
            textAlign = TextAlign.Center,
            lineHeight = 20.sp,
        )
    }
}

@Composable
private fun UdlWebView(sharedUrl: String?) {
    AndroidView(
        modifier = Modifier.fillMaxSize(),
        factory = { context ->
            WebView(context).apply {
                settings.javaScriptEnabled = true
                settings.domStorageEnabled = true
                webViewClient = WebViewClient()
                // navigator.clipboard neni v Android WebView pouzitelne, takze
                // ctenim schranky poverime nativni stranu - viz index.html,
                // funkce paste() zkousi window.UdlAndroid.readClipboard().
                addJavascriptInterface(ClipboardBridge(context), "UdlAndroid")
            }
        },
        update = { webView ->
            var target = "$LOCAL_SERVER_BASE/"
            if (!sharedUrl.isNullOrEmpty()) {
                target += "?url=" + Uri.encode(sharedUrl)
            }
            webView.loadUrl(target)
        },
    )
}

/**
 * Zpristupnuje systemovou schranku JavaScriptu ve WebView. Vystavena je
 * zamerne jen tahle jedna metoda (cteni textu) - nic jineho stranka
 * potrebovat nema a nic jineho ji tedy nedavame.
 */
private class ClipboardBridge(private val context: Context) {
    @JavascriptInterface
    fun readClipboard(): String {
        val manager = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            ?: return ""
        val clip = manager.primaryClip ?: return ""
        if (clip.itemCount == 0) return ""
        return clip.getItemAt(0).coerceToText(context)?.toString() ?: ""
    }
}
