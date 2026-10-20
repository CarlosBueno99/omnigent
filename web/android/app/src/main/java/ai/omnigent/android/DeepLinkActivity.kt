package ai.omnigent.android

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle

/** Opens a [DeepLink] in [MainActivity], switching to or connecting its server as needed. */
class DeepLinkActivity : Activity() {
    private var consent: AlertDialog? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val link = DeepLink.parse(intent?.dataString)
        if (link == null) {
            openApp(path = null)
            return
        }
        val store = ServerStore(this)
        if (store.hasServer() && originOf(store.currentServerUrl()) == link.origin) {
            openApp(link.path)
            return
        }
        val known = store.offeredServers().firstOrNull { originOf(it) == link.origin }
        if (known != null) {
            connect(store, known, link.path)
            return
        }
        // Connecting grants the origin the native bridge, so an unknown server needs consent.
        consent =
            AlertDialog
                .Builder(this)
                .setTitle(R.string.deep_link_consent_title)
                .setMessage(getString(R.string.deep_link_consent_message, link.hostLabel))
                .setPositiveButton(R.string.deep_link_consent_open) { _, _ ->
                    connect(store, link.origin, link.path)
                }.setNegativeButton(android.R.string.cancel) { _, _ -> finish() }
                .setOnCancelListener { finish() }
                .show()
    }

    override fun onDestroy() {
        consent?.dismiss()
        super.onDestroy()
    }

    private fun connect(
        store: ServerStore,
        serverUrl: String,
        path: String,
    ) {
        store.connect(serverUrl)
        openApp(path, UserConnectRequests.issue())
    }

    // NEW_TASK + CLEAR_TOP|SINGLE_TOP re-enter the app's own task, reusing a running shell.
    private fun openApp(
        path: String?,
        connectRequest: String? = null,
    ) {
        startActivity(
            Intent(this, MainActivity::class.java).apply {
                addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP,
                )
                path?.let { putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, it) }
                connectRequest?.let { putExtra(MainActivity.EXTRA_CONNECT_REQUEST, it) }
            },
        )
        finish()
    }
}
