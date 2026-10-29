package ir.eris.cctv.core

import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** The camera <-> server login: PBKDF2 key + HMAC challenge/response (same as eriscctv/auth.py). */
object Auth {
    private val random = SecureRandom()

    /** PBKDF2-HMAC-SHA256 with a 32-byte result, password as UTF-8 (Python's hashlib.pbkdf2_hmac). */
    fun pbkdf2(password: String, salt: ByteArray, iterations: Int): ByteArray {
        val pw = password.toByteArray(Charsets.UTF_8)
        require(pw.isNotEmpty()) { "empty password" }
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(pw, "HmacSHA256"))
        mac.update(salt)
        mac.update(byteArrayOf(0, 0, 0, 1))
        var u = mac.doFinal()
        val t = u.copyOf()
        for (n in 1 until iterations) {
            u = mac.doFinal(u)
            for (j in t.indices) t[j] = (t[j].toInt() xor u[j].toInt()).toByte()
        }
        return t
    }

    fun proof(key: ByteArray, vararg parts: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return hex(mac.doFinal(parts.joinToString("|").toByteArray(Charsets.UTF_8)))
    }

    fun same(a: String, b: String): Boolean = MessageDigest.isEqual(a.toByteArray(), b.toByteArray())

    fun sha256Hex(data: ByteArray): String = hex(MessageDigest.getInstance("SHA-256").digest(data))

    fun randomHex(bytes: Int): String = ByteArray(bytes).also { random.nextBytes(it) }.let { hex(it) }

    fun hex(b: ByteArray): String {
        val sb = StringBuilder(b.size * 2)
        for (x in b) {
            sb.append(HEX[(x.toInt() shr 4) and 15]).append(HEX[x.toInt() and 15])
        }
        return sb.toString()
    }

    fun unhex(s: String): ByteArray {
        require(s.length % 2 == 0) { "odd hex length" }
        return ByteArray(s.length / 2) { i -> s.substring(i * 2, i * 2 + 2).toInt(16).toByte() }
    }

    fun base64(data: ByteArray): String {
        val sb = StringBuilder()
        var i = 0
        while (i < data.size) {
            val b0 = data[i].toInt() and 255
            val b1 = if (i + 1 < data.size) data[i + 1].toInt() and 255 else -1
            val b2 = if (i + 2 < data.size) data[i + 2].toInt() and 255 else -1
            sb.append(B64[b0 shr 2])
            sb.append(B64[((b0 and 3) shl 4) or (if (b1 < 0) 0 else b1 shr 4)])
            sb.append(if (b1 < 0) '=' else B64[((b1 and 15) shl 2) or (if (b2 < 0) 0 else b2 shr 6)])
            sb.append(if (b2 < 0) '=' else B64[b2 and 63])
            i += 3
        }
        return sb.toString()
    }

    private const val HEX = "0123456789abcdef"
    private const val B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
}
