package win.moviora.udl

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import java.util.regex.Pattern

/**
 * Tohle je to, co se objeví v systémovém "Sdílet přes..." seznamu. Nemá
 * vlastní viditelné UI - jen z předaného textu vytáhne URL a přesměruje do
 * MainActivity, která zajistí Termux server a vyplní URL do formuláře.
 */
class ShareReceiverActivity : Activity() {

    private val urlPattern = Pattern.compile("https?://\\S+")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val sharedText = intent.getStringExtra(Intent.EXTRA_TEXT)
        val url = extractUrl(sharedText)

        val forward = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            if (url != null) putExtra("shared_url", url)
        }
        startActivity(forward)
        finish()
    }

    private fun extractUrl(text: String?): String? {
        if (text == null) return null
        val matcher = urlPattern.matcher(text)
        return if (matcher.find()) matcher.group() else null
    }
}
