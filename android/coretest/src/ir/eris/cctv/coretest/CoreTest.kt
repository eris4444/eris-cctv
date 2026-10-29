package ir.eris.cctv.coretest

import ir.eris.cctv.core.Json
import ir.eris.cctv.core.LinkHost
import ir.eris.cctv.core.Mp4Muxer
import ir.eris.cctv.core.ServerConfig
import ir.eris.cctv.core.ServerLink
import ir.eris.cctv.core.StatusText
import java.io.File
import java.io.FileOutputStream

/**
 * Drives the phone's core code on a PC: H.264 (Annex-B with AUDs) + AAC (ADTS) files made by ffmpeg
 * stand in for MediaCodec's output, paced like a live camera.
 *
 *   test   HOST PORT USER PASS TLSMODE                     -> JSON result of a connection test
 *   stream HOST PORT USER PASS TLSMODE H264 AAC OUT SECONDS FPS ROTATION
 *          streams to the server (and writes the fMP4 to OUT), prints JSON stats at the end
 *   mux    H264 AAC OUT FPS ROTATION                       -> only writes the fMP4 file, as fast as possible
 */
fun main(args: Array<String>) {
    when (args[0]) {
        "status" -> statusChecks()
        "test" -> {
            val host = Host(Muxed(null))
            println(Json.encode(ServerLink.test(cfg(args), host)))
        }
        "mux" -> {
            val out = FileOutputStream(args[3])
            val m = Muxed(out)
            feed(m, args[1], args[2], args[4].toInt(), args[5].toInt(), realtime = false, seconds = 1e9)
            out.close()
            println(Json.encode(mapOf("init" to (m.muxer?.initSegment?.size ?: 0), "fragments" to m.frags,
                "keys" to m.keys, "codec" to (m.muxer?.codec ?: ""))))
        }
        "stream" -> {
            val out = FileOutputStream(args[8])
            val m = Muxed(out)
            val host = Host(m)
            val link = ServerLink(cfg(args), host)
            m.link = link
            link.start()
            val start = System.currentTimeMillis()
            while (link.state != "connected" && System.currentTimeMillis() - start < 30000) Thread.sleep(100)
            feed(m, args[6], args[7], args[10].toInt(), args[11].toInt(), realtime = true, seconds = args[9].toDouble())
            Thread.sleep(1500)
            println(Json.encode(mapOf("state" to link.state, "secure" to link.secure, "pin" to link.cfg.tlsPin,
                "bytes_sent" to link.bytesSent, "dropped" to link.dropped, "fragments" to m.frags,
                "keys" to m.keys, "codec" to (m.muxer?.codec ?: ""), "error" to link.error,
                "subscribes" to host.subscribes, "log" to host.lines)))
            link.stop()
            out.close()
        }
    }
}

private fun cfg(a: Array<String>) = ServerConfig(a[1], a[2].toInt(), a[3], a[4], tlsMode = a[5])

/** The main screen's status lines (the 1.0.0 crash) under a Persian locale, with '%' in server texts. */
private fun statusChecks() {
    java.util.Locale.setDefault(java.util.Locale("fa", "IR"))
    val out = LinkedHashMap<String, Any?>()
    out["old_pattern_throws"] = try {
        @Suppress("FormatStringAnnotation")
        "Battery 86%, charging, %.0f C".format(34.0)
        false
    } catch (e: Exception) {
        e.javaClass.simpleName
    }
    out["battery"] = StatusText.battery(86, true, 34.4)
    out["battery_hot"] = StatusText.battery(100, false, 46.0)
    out["connected"] = StatusText.service(mapOf("server" to "connected", "server_name" to "Eris 100% CCTV %s %d",
        "secure" to true, "sent" to 1_572_864L, "dropped" to 0, "watchers" to 2, "camera" to "running",
        "size" to "1280x720", "frames" to 1234L), "panel.example.ir", 8800, 15, StatusText.battery(86, true, 34.4))
    out["error"] = StatusText.service(mapOf("server" to "error", "error" to "100%, broken %n %, c", "camera" to "error",
        "camera_error" to "the camera is in use (50%)", "sent" to 0L, "dropped" to 3, "watchers" to 0), "h", 1, 10, "")
    out["connecting"] = StatusText.service(mapOf("server" to "connecting", "camera" to "idle"), "panel.example.ir", 8800, 15, "")
    println(Json.encode(out))
}

class Muxed(private val out: FileOutputStream?) {
    var muxer: Mp4Muxer? = null
    var link: ServerLink? = null
    var frags = 0
    var keys = 0
    fun sink(init: Boolean, data: ByteArray, key: Boolean) {
        out?.write(data)
        if (init) link?.onInit(0, data) else {
            frags++
            if (key) keys++
            link?.onFrag(0, data, key)
        }
    }
}

class Host(private val m: Muxed) : LinkHost {
    override val agentId = "jvmtest" + "0123456789"
    override val agentName = "JVM phone test"
    override val version = "android-test"
    var subscribes = 0
    val lines = ArrayList<String>()
    override fun camerasJson(): List<Map<String, Any?>> {
        val mx = m.muxer
        return listOf(mapOf("id" to "phonecam", "slot" to 0, "name" to "Phone test", "online" to (mx?.initSegment != null),
            "state" to "running", "error" to "", "codec" to (mx?.codec ?: ""), "audio" to (mx?.hasAudio == true),
            "width" to 640, "height" to 360, "fps" to 15, "enabled" to true, "recording" to false))
    }
    override fun initFor(slot: Int) = m.muxer?.initSegment
    override fun onSubscribe(link: ServerLink, slot: Int) { subscribes++ }
    override fun onUnsubscribe(link: ServerLink, slot: Int) {}
    override fun onPinChanged(link: ServerLink, pin: String) { lines.add("pin $pin") }
    override fun onLinkState(link: ServerLink) {}
    override fun log(message: String) { lines.add(message); System.err.println("[link] $message") }
}

/** Access units of an Annex-B stream that has an AUD (NAL type 9) before every frame. */
private fun accessUnits(data: ByteArray): List<ByteArray> {
    val starts = ArrayList<Int>()
    var i = 0
    while (i + 3 < data.size) {
        if (data[i].toInt() == 0 && data[i + 1].toInt() == 0 && data[i + 2].toInt() == 1 && (data[i + 3].toInt() and 31) == 9) {
            starts.add(if (i > 0 && data[i - 1].toInt() == 0) i - 1 else i)
            i += 4
        } else i++
    }
    starts.add(data.size)
    return (0 until starts.size - 1).map { data.copyOfRange(starts[it], starts[it + 1]) }
}

private class Adts(val frames: List<ByteArray>, val asc: ByteArray, val rate: Int, val channels: Int)

private fun adts(data: ByteArray): Adts {
    val rates = intArrayOf(96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
    val frames = ArrayList<ByteArray>()
    var i = 0
    var asc = ByteArray(0)
    var rate = 0
    var channels = 0
    while (i + 7 <= data.size) {
        val b1 = data[i + 1].toInt() and 255
        val b2 = data[i + 2].toInt() and 255
        val b3 = data[i + 3].toInt() and 255
        val len = ((b3 and 3) shl 11) or ((data[i + 4].toInt() and 255) shl 3) or ((data[i + 5].toInt() and 255) shr 5)
        val header = if (b1 and 1 == 1) 7 else 9
        val profile = (b2 shr 6) + 1
        val freq = (b2 shr 2) and 15
        channels = ((b2 and 1) shl 2) or (b3 shr 6)
        rate = rates[freq]
        asc = byteArrayOf(((profile shl 3) or (freq shr 1)).toByte(), (((freq and 1) shl 7) or (channels shl 3)).toByte())
        frames.add(data.copyOfRange(i + header, i + len))
        i += len
    }
    return Adts(frames, asc, rate, channels)
}

private fun feed(m: Muxed, h264: String, aac: String, fps: Int, rotation: Int, realtime: Boolean, seconds: Double) {
    val aus = accessUnits(File(h264).readBytes())
    val audio = if (aac == "-") null else adts(File(aac).readBytes())
    val muxer = Mp4Muxer(640, 360, rotation, audio?.rate ?: 0, audio?.channels ?: 1, maxOf(100_000L, 2_000_000L / fps),
        m::sink)
    m.muxer = muxer
    muxer.setVideoConfig(aus[0])
    if (audio != null) muxer.setAudioConfig(audio.asc)
    m.link?.camerasChanged()
    val start = System.nanoTime()
    var a = 0
    for ((n, au) in aus.withIndex()) {
        val pts = n * 1_000_000L / fps
        if (pts / 1e6 > seconds) break
        if (realtime) {
            val wait = start + pts * 1000 - System.nanoTime()
            if (wait > 0) Thread.sleep(wait / 1_000_000, (wait % 1_000_000).toInt())
        }
        val key = Mp4Muxer.nals(au).any { (it[0].toInt() and 31) == 5 }
        muxer.addVideo(au, pts, key)
        if (audio != null) {
            while (a < audio.frames.size && a * 1024L * 1_000_000 / audio.rate <= pts) {
                muxer.addAudio(audio.frames[a], a * 1024L * 1_000_000 / audio.rate)
                a++
            }
        }
    }
}
