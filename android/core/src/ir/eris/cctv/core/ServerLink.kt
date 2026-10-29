package ir.eris.cctv.core

import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.util.ArrayDeque
import java.util.concurrent.Executors
import javax.net.ssl.SSLException
import javax.net.ssl.SSLHandshakeException

class AuthError(message: String) : Exception(message)
private class Reconnect(message: String) : Exception(message)

/** One server as configured on the phone (same fields as a server entry of the desktop agent). */
class ServerConfig(
    val address: String,
    val port: Int,
    val username: String,
    val password: String,
    val tlsMode: String = "auto",     // auto | on | off
    val tlsVerify: String = "pin",    // pin | ca | none
    @Volatile var tlsPin: String = "",
)

/** What a ServerLink needs from the app. */
interface LinkHost {
    val agentId: String
    val agentName: String
    val version: String
    fun camerasJson(): List<Map<String, Any?>>
    fun initFor(slot: Int): ByteArray?
    fun onSubscribe(link: ServerLink, slot: Int)
    fun onUnsubscribe(link: ServerLink, slot: Int)
    fun onPinChanged(link: ServerLink, pin: String)
    fun onLinkState(link: ServerLink)
    fun log(message: String)
}

/**
 * Keeps one outgoing connection to one server alive and streams the cameras it asks for
 * (port of eriscctv/link.py: automatic TLS with fallback for servers without TLS, certificate pinning
 * that follows publicly trusted renewals, quick reconnects, stale video skipped per group of pictures).
 */
class ServerLink(val cfg: ServerConfig, private val host: LinkHost) {
    @Volatile var state = "stopped"; private set
    @Volatile var error = ""; private set
    @Volatile var secure = false; private set
    @Volatile var serverTls = false; private set
    @Volatile var serverName = ""; private set
    @Volatile var connectedSince = 0L; private set
    @Volatile var bytesSent = 0L; private set
    @Volatile var dropped = 0; private set

    private class Item(val at: Long, val kind: Int, val slot: Int, val key: Boolean, val framed: ByteArray)

    private val lock = Object()
    private val subscribed = HashSet<Int>()
    private val primed = HashSet<Int>()
    private val queue = ArrayDeque<Item>()
    private var queued = 0L
    private val gapTail = HashSet<Int>()
    @Volatile private var ws: WebSocket? = null
    @Volatile private var running = false
    private var thread: Thread? = null
    private var plainUntil = 0L
    private val keyCache = HashMap<String, ByteArray>()
    private val notifier = Executors.newSingleThreadExecutor { r -> Thread(r, "link-notify").apply { isDaemon = true } }

    val watching: Set<Int> get() = synchronized(lock) { subscribed.toSet() }

    fun start() {
        if (running) return
        running = true
        thread = Thread({ loop() }, "link-${cfg.address}:${cfg.port}").apply { isDaemon = true; start() }
    }

    fun stop() {
        running = false
        ws?.abort()
        thread?.interrupt()
        try { thread?.join(3000) } catch (_: InterruptedException) { }
        thread = null
        state = "stopped"
        notifier.shutdownNow()
    }

    /** Like camerasChanged, but sent before returning - use it right before a new init segment, so the
     *  server knows the new codec string when the viewers' players are set up. */
    fun camerasChangedNow() {
        val w = ws ?: return
        if (state != "connected") return
        try { sendCameras(w) } catch (_: IOException) { }
    }

    /** The camera list or a camera's state changed: tell the server. */
    fun camerasChanged() {
        val w = ws ?: return
        if (state != "connected") return
        try {
            notifier.execute { try { sendCameras(w) } catch (_: IOException) { } }
        } catch (_: Exception) { }
    }

    // ------------------------------------------------------------------ media from the camera
    fun onInit(slot: Int, data: ByteArray) {
        synchronized(lock) {
            if (slot !in subscribed) return
            enqueue(KIND_INIT, slot, data, force = true)
            gapTail.add(slot)
            primed.add(slot)
        }
    }

    fun onFrag(slot: Int, data: ByteArray, key: Boolean) {
        synchronized(lock) {
            if (slot in primed) enqueue(KIND_FRAG, slot, data, key = key)
        }
    }

    // Fragments are only dropped together with the rest of their group of pictures (see link.py).
    private fun dropGop(items: Array<Item?>, i: Int) {
        val slot = items[i]!!.slot
        for (j in i until items.size) {
            val it = items[j] ?: continue
            if (it.slot != slot) continue
            if (j > i && (it.kind == KIND_INIT || it.key)) return
            if (it.kind == KIND_FRAG) {
                dropped++
                queued -= it.framed.size - 3
                items[j] = null
            }
        }
        gapTail.add(slot)
    }

    private fun prune() {
        val now = System.nanoTime()
        val head = queue.peekFirst() ?: return
        if (now - head.at <= MAX_LAG_NS) return
        val items = queue.toTypedArray<Item?>()
        for (i in items.indices) {
            val it = items[i] ?: continue
            if (it.kind == KIND_FRAG && now - it.at > MAX_LAG_NS) dropGop(items, i)
        }
        queue.clear()
        for (it in items) if (it != null) queue.add(it)
    }

    private fun enqueue(kind: Int, slot: Int, data: ByteArray, force: Boolean = false, key: Boolean = true) {
        prune()
        if (kind == KIND_INIT) gapTail.remove(slot)
        else if (slot in gapTail) {
            if (!key) { dropped++; return }
            gapTail.remove(slot)
        }
        if (!force && queued + data.size > MAX_QUEUE_BYTES) {
            dropped++
            gapTail.add(slot)
            return
        }
        val framed = ByteArray(data.size + 3)
        framed[0] = kind.toByte()
        framed[1] = (slot shr 8).toByte()
        framed[2] = slot.toByte()
        System.arraycopy(data, 0, framed, 3, data.size)
        queue.add(Item(System.nanoTime(), kind, slot, key, framed))
        queued += data.size
        lock.notifyAll()
    }

    // ------------------------------------------------------------------ connection
    private fun loop() {
        var backoff = 3000L
        while (running) {
            state = "connecting"
            connectedSince = 0
            host.onLinkState(this)
            var delay: Long? = null
            try {
                session()
                backoff = 3000
            } catch (e: Reconnect) {
                state = "connecting"; error = ""
                host.log("server ${cfg.address}:${cfg.port}: ${e.message}")
                delay = 1000
            } catch (e: AuthError) {
                state = "error"; error = e.message ?: "refused"
                host.log("server ${cfg.address}:${cfg.port}: $error")
                backoff = 60000
            } catch (e: InterruptedException) {
                break
            } catch (e: Exception) {
                state = "error"; error = describe(e)
                host.log("server ${cfg.address}:${cfg.port}: $error")
            } finally {
                detach()
                ws?.abort()
                ws = null
                secure = false
                host.onLinkState(this)
            }
            if (!running) break
            if (delay == null) {
                if (connectedSince > 0 && backoff < 60000) backoff = 2000  // the link was up: viewers are waiting
                delay = backoff
                backoff = minOf(backoff * 2, 60000)
            }
            try { Thread.sleep(delay) } catch (_: InterruptedException) { break }
        }
        state = "stopped"
        host.onLinkState(this)
    }

    private fun detach() {
        val was: List<Int>
        synchronized(lock) {
            was = subscribed.toList()
            subscribed.clear()
            primed.clear()
            queue.clear()
            queued = 0
            gapTail.clear()
            lock.notifyAll()
        }
        for (slot in was) host.onUnsubscribe(this, slot)
    }

    private fun session() {
        val plainFirst = System.nanoTime() < plainUntil
        val pinBefore = cfg.tlsPin
        val w = open(cfg, plainFirst, host)
        ws = w
        secure = w.secure
        if (!secure && cfg.tlsMode == "auto") plainUntil = System.nanoTime() + PLAIN_RETRY_TLS_NS
        val welcome = handshake(w, cfg, host, keyCache, probe = false)
        if (secure && cfg.tlsVerify == "pin") {
            if (cfg.tlsPin.isEmpty()) cfg.tlsPin = w.fingerprint  // trust on first use, after the password proof
            if (cfg.tlsPin != pinBefore) host.onPinChanged(this, cfg.tlsPin)
        }
        serverTls = welcome["tls"] == true || secure
        if (serverTls && !secure && cfg.tlsMode == "auto") {
            plainUntil = 0
            if (plainFirst) throw Reconnect("the server offers TLS now - reconnecting encrypted")
        }
        serverName = welcome["server_name"] as? String ?: ""
        state = "connected"
        error = ""
        connectedSince = System.currentTimeMillis()
        host.log("connected to server ${cfg.address}:${cfg.port} ($serverName), ${if (secure) "TLS" else "NOT encrypted"}")
        host.onLinkState(this)
        sendCameras(w)
        val sender = Thread({ senderLoop(w) }, "link-send").apply { isDaemon = true; start() }
        val pinger = Thread({ pingerLoop(w) }, "link-ping").apply { isDaemon = true; start() }
        try {
            while (running) {
                val m = w.recv()
                if (m is WebSocket.Message.Text) {
                    val data = try { Json.obj(m.text) } catch (_: Exception) { continue }
                    handle(data)
                }
            }
        } catch (e: WebSocketClosed) {
            throw IOException("connection closed: ${e.message}")
        } finally {
            w.abort()
            sender.interrupt()
            pinger.interrupt()
            synchronized(lock) { lock.notifyAll() }
        }
    }

    private fun handle(data: Map<String, Any?>) {
        val slot = (data["slot"] as? Number)?.toInt() ?: return
        when (data["type"]) {
            "subscribe" -> {
                if (host.camerasJson().none { (it["slot"] as? Number)?.toInt() == slot }) return
                synchronized(lock) {
                    subscribed.add(slot)
                    primed.remove(slot)
                    val init = host.initFor(slot)
                    if (init != null) {
                        enqueue(KIND_INIT, slot, init, force = true)
                        gapTail.add(slot) // start the server's copy at the next keyframe
                        primed.add(slot)
                    }
                }
                host.onSubscribe(this, slot)
            }
            "unsubscribe" -> {
                synchronized(lock) {
                    subscribed.remove(slot)
                    primed.remove(slot)
                }
                host.onUnsubscribe(this, slot)
            }
        }
    }

    private fun sendCameras(w: WebSocket) {
        w.sendText(Json.encode(mapOf("type" to "cameras", "cameras" to host.camerasJson(),
            "agent_name" to host.agentName)))
    }

    private fun senderLoop(w: WebSocket) {
        try {
            while (!w.closed) {
                val item: Item
                synchronized(lock) {
                    prune()
                    while (queue.isEmpty() && !w.closed) {
                        lock.wait(1000)
                        prune()
                    }
                    if (w.closed) return
                    item = queue.pollFirst()
                    queued -= item.framed.size - 3
                }
                w.sendBinary(item.framed)
                bytesSent += item.framed.size
            }
        } catch (_: InterruptedException) {
        } catch (_: IOException) {
            w.abort()
        }
    }

    private fun pingerLoop(w: WebSocket) {
        try {
            while (!w.closed) {
                Thread.sleep(20000)
                if (System.nanoTime() - w.lastRecv > 75_000_000_000L) {
                    host.log("server ${cfg.address}:${cfg.port} stopped answering")
                    w.abort()
                    return
                }
                w.ping()
            }
        } catch (_: InterruptedException) {
        } catch (_: IOException) {
        }
    }

    companion object {
        const val KIND_INIT = 1
        const val KIND_FRAG = 2
        const val PATH = "/agent/ws"
        private const val MAX_QUEUE_BYTES = 8L * 1024 * 1024
        private const val MAX_LAG_NS = 3_000_000_000L
        private const val PLAIN_RETRY_TLS_NS = 600_000_000_000L
        private const val AUTO_TLS_TIMEOUT = 10000

        fun open(cfg: ServerConfig, plainFirst: Boolean, host: LinkHost): WebSocket {
            val mode = cfg.tlsMode
            val pinned = cfg.tlsPin.isNotEmpty()
            if (mode == "on" || (mode == "auto" && (pinned || !plainFirst))) {
                try {
                    return openTls(cfg, if (mode == "on" || pinned) 15000 else AUTO_TLS_TIMEOUT, host)
                } catch (e: AuthError) {
                    throw e
                } catch (e: IOException) {
                    if (mode == "on" || pinned || (cfg.tlsVerify == "ca" && e is SSLHandshakeException)) throw e
                    host.log("server ${cfg.address}:${cfg.port}: no TLS (${describe(e)}) - trying an unencrypted connection")
                }
            }
            return WebSocket.connect(cfg.address, cfg.port, PATH, tls = false)
        }

        private fun openTls(cfg: ServerConfig, timeout: Int, host: LinkHost): WebSocket {
            val w = WebSocket.connect(cfg.address, cfg.port, PATH, tls = true, verify = cfg.tlsVerify, timeoutMs = timeout)
            if (cfg.tlsVerify != "pin" || cfg.tlsPin.isEmpty() || Auth.same(w.fingerprint, cfg.tlsPin)) return w
            w.abort()
            // A CDN or Let's Encrypt renews its certificate every few months: a new certificate that a
            // public CA vouches for, for this very name, is followed; anything else is refused.
            val renewed = try {
                WebSocket.connect(cfg.address, cfg.port, PATH, tls = true, verify = "ca", timeoutMs = timeout)
            } catch (e: IOException) {
                throw AuthError("The server's TLS certificate changed (expected ${cfg.tlsPin.take(16)}..., got " +
                    "${w.fingerprint.take(16)}...). If you reinstalled the server, choose \"Forget pinned certificate\".")
            }
            host.log("server ${cfg.address}:${cfg.port} renewed its publicly trusted certificate - pin updated")
            cfg.tlsPin = renewed.fingerprint
            return renewed
        }

        /** Challenge/response login; the password never crosses the network. */
        fun handshake(w: WebSocket, cfg: ServerConfig, host: LinkHost, keyCache: MutableMap<String, ByteArray>,
                      probe: Boolean): Map<String, Any?> {
            val cnonce = Auth.randomHex(16)
            w.setTimeout(20000)
            w.sendText(Json.encode(mapOf("type" to "hello", "username" to cfg.username, "agent_id" to host.agentId,
                "agent_name" to host.agentName, "version" to host.version, "cnonce" to cnonce, "probe" to probe)))
            var msg = Json.obj((w.recv() as? WebSocket.Message.Text)?.text ?: throw AuthError("unexpected answer from server"))
            if (msg["type"] == "error") throw AuthError(msg["message"] as? String ?: "refused by server")
            if (msg["type"] != "challenge") throw AuthError("unexpected answer from server")
            val salt = msg["salt"] as String
            val iterations = (msg["iterations"] as Number).toInt()
            val nonce = msg["nonce"] as String
            val ck = "$salt|$iterations|${cfg.password.hashCode()}"
            val key = keyCache.getOrPut(ck) { Auth.pbkdf2(cfg.password, Auth.unhex(salt), iterations) }
            if (keyCache.size > 4) keyCache.keys.filter { it != ck }.forEach { keyCache.remove(it) }
            w.sendText(Json.encode(mapOf("type" to "auth", "proof" to Auth.proof(key, "agent", nonce, cnonce, host.agentId))))
            msg = Json.obj((w.recv() as? WebSocket.Message.Text)?.text ?: throw AuthError("unexpected answer from server"))
            if (msg["type"] == "error") throw AuthError(msg["message"] as? String ?: "Wrong username or password")
            if (msg["type"] != "welcome") throw AuthError("unexpected answer from server")
            if (!Auth.same(msg["proof"] as? String ?: "", Auth.proof(key, "server", nonce, cnonce, host.agentId))) {
                throw AuthError("server could not prove it knows the password (wrong server?)")
            }
            w.setTimeout(0)
            return msg
        }

        /** One-off connection test (does not replace a running link on the server). */
        fun test(cfg: ServerConfig, host: LinkHost): Map<String, Any?> {
            var w: WebSocket? = null
            return try {
                w = open(cfg, false, host)
                val welcome = handshake(w, cfg, host, HashMap(), probe = true)
                mapOf("ok" to true, "server_name" to (welcome["server_name"] ?: ""), "secure" to w.secure,
                    "server_tls" to (welcome["tls"] == true || w.secure), "fingerprint" to w.fingerprint,
                    "server_version" to (welcome["server_version"] ?: ""))
            } catch (e: AuthError) {
                mapOf("ok" to false, "error" to (e.message ?: "refused"))
            } catch (e: Exception) {
                mapOf("ok" to false, "error" to describe(e))
            } finally {
                w?.close()
            }
        }

        fun describe(e: Throwable): String = when (e) {
            is SocketTimeoutException -> "Connection timed out (check address, port and firewall)"
            is ConnectException -> "Connection refused (server not running or wrong port)"
            is UnknownHostException -> "Cannot resolve address (check the IP / domain)"
            is HandshakeError -> "Not an Eris CCTV server: ${e.message}"
            is SSLException -> "TLS error: ${e.message}"
            else -> e.message ?: e.javaClass.simpleName
        }
    }
}
