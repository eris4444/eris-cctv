package ir.eris.cctv.core

import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

class WebSocketClosed(message: String) : IOException(message)
class HandshakeError(message: String) : IOException(message)
class CertificateChanged(message: String) : IOException(message)

/** Blocking RFC 6455 client (masked frames, ping/pong, close) over TCP or TLS. */
class WebSocket private constructor(private val socket: Socket) {
    private val input: InputStream = BufferedInputStream(socket.getInputStream(), 65536)
    private val output: OutputStream = socket.getOutputStream()
    private val writeLock = Object()
    private val random = SecureRandom()

    @Volatile var closed = false
        private set
    @Volatile var lastRecv = System.nanoTime()
        private set
    /** SHA-256 of the server certificate (TLS only). */
    var fingerprint = ""
        private set
    val secure: Boolean get() = socket is SSLSocket

    sealed class Message {
        class Text(val text: String) : Message()
        class Binary(val data: ByteArray) : Message()
    }

    /** Read timeout for recv() in ms (0 = none); a timeout closes the connection. */
    fun setTimeout(ms: Int) {
        socket.soTimeout = ms
    }

    fun sendText(text: String) = send(OP_TEXT, text.toByteArray(Charsets.UTF_8))
    fun sendBinary(data: ByteArray) = send(OP_BIN, data)
    fun ping() = send(OP_PING, ByteArray(0))

    private fun send(op: Int, data: ByteArray) {
        if (closed && op != OP_CLOSE) throw WebSocketClosed("closed")
        val n = data.size
        val head = ByteArrayOutputStream(14)
        head.write(0x80 or op)
        when {
            n < 126 -> head.write(0x80 or n)
            n < 65536 -> { head.write(0x80 or 126); head.write(n shr 8); head.write(n and 255) }
            else -> {
                head.write(0x80 or 127)
                for (s in 56 downTo 0 step 8) head.write(((n.toLong() shr s) and 255).toInt())
            }
        }
        val mask = ByteArray(4).also { random.nextBytes(it) }
        head.write(mask)
        val masked = ByteArray(n)
        for (i in 0 until n) masked[i] = (data[i].toInt() xor mask[i and 3].toInt()).toByte()
        synchronized(writeLock) {
            try {
                output.write(head.toByteArray())
                output.write(masked)
                output.flush()
            } catch (e: IOException) {
                closed = true
                throw WebSocketClosed(e.message ?: "connection lost")
            }
        }
    }

    /** Next text or binary message; answers pings, ends with WebSocketClosed. */
    fun recv(): Message {
        val parts = ByteArrayOutputStream()
        var msgOp = -1
        while (true) {
            if (closed) throw WebSocketClosed("closed")
            val b0: Int
            val b1: Int
            val payload: ByteArray
            try {
                b0 = readByte()
                b1 = readByte()
                var n = (b1 and 127).toLong()
                if (n == 126L) n = ((readByte() shl 8) or readByte()).toLong()
                else if (n == 127L) {
                    n = 0
                    repeat(8) { n = (n shl 8) or readByte().toLong() }
                }
                if (n > MAX_MESSAGE) throw WebSocketClosed("frame too large")
                val mask = if (b1 and 0x80 != 0) readFully(4) else null
                payload = readFully(n.toInt())
                if (mask != null) for (i in payload.indices) payload[i] = (payload[i].toInt() xor mask[i and 3].toInt()).toByte()
            } catch (e: IOException) {
                closed = true
                if (e is WebSocketClosed) throw e
                throw WebSocketClosed(e.message ?: "connection lost")
            }
            lastRecv = System.nanoTime()
            val fin = b0 and 0x80 != 0
            when (val op = b0 and 15) {
                OP_PING -> { try { send(OP_PONG, payload) } catch (_: IOException) { } }
                OP_PONG -> { }
                OP_CLOSE -> {
                    if (!closed) try { send(OP_CLOSE, payload.copyOf(minOf(2, payload.size))) } catch (_: IOException) { }
                    closed = true
                    abort()
                    throw WebSocketClosed("closed by peer")
                }
                OP_TEXT, OP_BIN, OP_CONT -> {
                    if (op != OP_CONT) msgOp = op
                    parts.write(payload)
                    if (parts.size() > MAX_MESSAGE) throw WebSocketClosed("message too large")
                    if (fin) {
                        return if (msgOp == OP_TEXT) Message.Text(parts.toString("UTF-8"))
                        else Message.Binary(parts.toByteArray())
                    }
                }
                else -> throw WebSocketClosed("unknown opcode $op")
            }
        }
    }

    private fun readByte(): Int {
        val b = input.read()
        if (b < 0) throw EOFException("connection closed")
        return b
    }

    private fun readFully(n: Int): ByteArray {
        val out = ByteArray(n)
        var off = 0
        while (off < n) {
            val r = input.read(out, off, n - off)
            if (r < 0) throw EOFException("connection closed")
            off += r
        }
        return out
    }

    fun close() {
        if (!closed) {
            try { send(OP_CLOSE, byteArrayOf(0x03, 0xE8.toByte())) } catch (_: IOException) { }
        }
        abort()
    }

    fun abort() {
        closed = true
        try { socket.close() } catch (_: IOException) { }
    }

    companion object {
        private const val OP_CONT = 0
        private const val OP_TEXT = 1
        private const val OP_BIN = 2
        private const val OP_CLOSE = 8
        private const val OP_PING = 9
        private const val OP_PONG = 10
        private const val MAX_MESSAGE = 64L * 1024 * 1024
        private const val GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

        /** Accepts every certificate: the caller pins the fingerprint instead (trust on first use). */
        private val pinContext: SSLContext by lazy {
            SSLContext.getInstance("TLS").apply {
                init(null, arrayOf(object : X509TrustManager {
                    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}
                    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {}
                    override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
                }), SecureRandom())
            }
        }

        /**
         * tls = false: plain ws://. tls = true: verify = "pin" (any certificate, caller compares the
         * fingerprint), "ca" (public CA + host name) or "none".
         */
        fun connect(host: String, port: Int, path: String, tls: Boolean, verify: String = "pin",
                    timeoutMs: Int = 15000, headers: Map<String, String> = emptyMap()): WebSocket {
            val raw = Socket()
            raw.tcpNoDelay = true
            raw.sendBufferSize = 128 * 1024
            try {
                raw.connect(InetSocketAddress(host, port), timeoutMs)
                raw.soTimeout = timeoutMs
                var sock: Socket = raw
                var fp = ""
                if (tls) {
                    val ctx = if (verify == "ca") SSLContext.getDefault() else pinContext
                    val ssl = ctx.socketFactory.createSocket(raw, host, port, true) as SSLSocket
                    ssl.startHandshake()
                    val chain = ssl.session.peerCertificates
                    fp = Auth.sha256Hex(chain[0].encoded)
                    if (verify == "ca" && !HttpsURLConnection.getDefaultHostnameVerifier().verify(host, ssl.session)) {
                        ssl.close()
                        throw javax.net.ssl.SSLPeerUnverifiedException("certificate is not valid for $host")
                    }
                    sock = ssl
                }
                val ws = WebSocket(sock)
                ws.fingerprint = fp
                ws.handshake(host, port, path, tls, headers)
                sock.soTimeout = 0
                return ws
            } catch (e: Exception) {
                try { raw.close() } catch (_: IOException) { }
                throw e
            }
        }
    }

    private fun handshake(host: String, port: Int, path: String, tls: Boolean, headers: Map<String, String>) {
        val key = Auth.base64(ByteArray(16).also { random.nextBytes(it) })
        val defaultPort = if (tls) 443 else 80
        val h = if (host.contains(':')) "[$host]" else host
        val sb = StringBuilder()
        sb.append("GET ").append(path).append(" HTTP/1.1\r\n")
        sb.append("Host: ").append(if (port == defaultPort) h else "$h:$port").append("\r\n")
        sb.append("Upgrade: websocket\r\nConnection: Upgrade\r\n")
        sb.append("Sec-WebSocket-Key: ").append(key).append("\r\n")
        sb.append("Sec-WebSocket-Version: 13\r\n")
        for ((k, v) in headers) sb.append(k).append(": ").append(v).append("\r\n")
        sb.append("\r\n")
        output.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
        output.flush()
        val head = ByteArrayOutputStream()
        var last4 = 0
        while (true) {
            val b = input.read()
            if (b < 0) throw HandshakeError("connection closed during the handshake")
            head.write(b)
            last4 = (last4 shl 8) or b
            if (last4 == 0x0D0A0D0A) break
            if (head.size() > 16384) throw HandshakeError("handshake answer too long")
        }
        val lines = head.toString("ISO-8859-1").split("\r\n")
        val status = lines[0].split(" ")
        if (status.size < 2 || status[1] != "101") throw HandshakeError("server answered: ${lines[0].take(100)}")
        val accept = lines.drop(1).firstOrNull { it.lowercase().startsWith("sec-websocket-accept:") }
            ?.substringAfter(':')?.trim()
        val expect = Auth.base64(MessageDigest.getInstance("SHA-1").digest((key + GUID).toByteArray()))
        if (accept != expect) throw HandshakeError("bad handshake from server")
    }
}
