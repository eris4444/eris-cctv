package ir.eris.cctv.core

import java.io.ByteArrayOutputStream

/**
 * Fragmented MP4 writer for a live H.264 (+ AAC) stream, matching what the desktop camera agent sends:
 * one init segment (ftyp + moov), then moof + mdat fragments of ~2 frames. A fragment that starts with
 * a keyframe carries first_sample_flags = sync, every other video sample is marked non-sync, so the
 * server's KeyframeDetector and the browser see the same thing as with ffmpeg's output.
 *
 * Input is what Android's MediaCodec produces: Annex-B H.264 access units and raw AAC frames, with
 * presentation times in microseconds on one clock.
 */
class Mp4Muxer(
    private val width: Int,
    private val height: Int,
    private val rotation: Int,
    private val audioRate: Int,          // 0 = no audio track
    private val audioChannels: Int,
    private val fragmentUs: Long,
    private val sink: (init: Boolean, data: ByteArray, key: Boolean) -> Unit,
) {
    private class Sample(val data: ByteArray, val dts: Long, var duration: Long, val key: Boolean)

    private var sps: ByteArray? = null
    private var pps: ByteArray? = null
    private var asc: ByteArray? = null
    var initSegment: ByteArray? = null
        private set
    private var t0 = Long.MIN_VALUE
    private var sequence = 1
    private var lastVideo: Sample? = null        // held back until the next frame tells its duration
    private val video = ArrayList<Sample>()
    private val audio = ArrayList<Sample>()
    private var audioNextDts = Long.MIN_VALUE

    val hasAudio: Boolean get() = audioRate > 0

    /** MediaCodec's codec string for MediaSource, e.g. "avc1.42c01f,mp4a.40.2". */
    val codec: String
        get() {
            val s = sps ?: return ""
            // Locale.US: on a phone set to Persian, String.format may otherwise use Persian digits
            val v = String.format(java.util.Locale.US, "avc1.%02x%02x%02x",
                s[1].toInt() and 255, s[2].toInt() and 255, s[3].toInt() and 255)
            return if (hasAudio) "$v,mp4a.40.2" else v
        }

    /** The codec-config buffer of the video encoder (Annex-B SPS + PPS). */
    @Synchronized
    fun setVideoConfig(annexB: ByteArray) {
        for (nal in nals(annexB)) {
            when (nal[0].toInt() and 31) {
                7 -> sps = nal
                8 -> pps = nal
            }
        }
        maybeInit()
    }

    /** The codec-config buffer of the AAC encoder (AudioSpecificConfig). */
    @Synchronized
    fun setAudioConfig(config: ByteArray) {
        asc = config
        maybeInit()
    }

    private fun maybeInit() {
        if (initSegment != null || sps == null || pps == null || (hasAudio && asc == null)) return
        val init = buildInit()
        initSegment = init
        sink(true, init, false)
    }

    @Synchronized
    fun addVideo(annexB: ByteArray, ptsUs: Long, key: Boolean) {
        if (initSegment == null) {
            // parameter sets may also come in-band (some encoders) - pick them up
            if (sps == null || pps == null) setVideoConfig(annexB)
            if (initSegment == null) return
        }
        if (t0 == Long.MIN_VALUE) {
            if (!key) return // start at a keyframe
            t0 = ptsUs
        }
        val avcc = toLengthPrefixed(annexB)
        if (avcc.isEmpty()) return
        val dts = (ptsUs - t0) * VIDEO_TIMESCALE / 1_000_000
        val prev = lastVideo
        if (prev != null) {
            if (dts <= prev.dts) return // out-of-order or duplicate timestamp
            prev.duration = dts - prev.dts
            video.add(prev)
            val span = (dts - video.first().dts) * 1_000_000 / VIDEO_TIMESCALE
            // a keyframe always starts a new fragment; otherwise cut every fragmentUs (5 ms for rounding)
            if (key || span + 5000 >= fragmentUs) flush()
        }
        lastVideo = Sample(avcc, dts, 0, key)
    }

    @Synchronized
    fun addAudio(frame: ByteArray, ptsUs: Long) {
        if (!hasAudio || initSegment == null || t0 == Long.MIN_VALUE || frame.isEmpty()) return
        val samples = 1024L
        var dts = (ptsUs - t0) * audioRate / 1_000_000
        if (dts < 0) return
        if (audioNextDts != Long.MIN_VALUE && Math.abs(dts - audioNextDts) < samples * 2) {
            dts = audioNextDts // keep the audio timeline gapless despite clock jitter
        }
        audioNextDts = dts + samples
        audio.add(Sample(frame, dts, samples, true))
    }

    private fun flush() {
        if (video.isEmpty()) return
        val key = video.first().key
        val frag = buildFragment(video.toList(), audio.toList())
        video.clear()
        audio.clear()
        sink(false, frag, key)
    }

    // ---------------------------------------------------------------------------------- boxes
    private class Box(type: String) {
        val out = ByteArrayOutputStream()
        init {
            int(0)
            out.write(type.toByteArray(Charsets.ISO_8859_1))
        }
        fun int(v: Int) { out.write(v ushr 24); out.write(v ushr 16 and 255); out.write(v ushr 8 and 255); out.write(v and 255) }
        fun short(v: Int) { out.write(v ushr 8 and 255); out.write(v and 255) }
        fun byte(v: Int) { out.write(v and 255) }
        fun long(v: Long) { int((v ushr 32).toInt()); int(v.toInt()) }
        fun bytes(b: ByteArray) { out.write(b) }
        fun zeros(n: Int) { repeat(n) { out.write(0) } }
        fun box(b: Box) { out.write(b.done()) }
        fun done(): ByteArray {
            val a = out.toByteArray()
            val n = a.size
            a[0] = (n ushr 24).toByte(); a[1] = (n ushr 16).toByte(); a[2] = (n ushr 8).toByte(); a[3] = n.toByte()
            return a
        }
    }

    private fun full(type: String, version: Int, flags: Int) = Box(type).apply { int((version shl 24) or flags) }

    private fun buildInit(): ByteArray {
        val ftyp = Box("ftyp").apply {
            bytes("isom".toByteArray()); int(0x200)
            for (b in listOf("isom", "iso6", "avc1", "mp41")) bytes(b.toByteArray())
        }
        val moov = Box("moov")
        moov.box(full("mvhd", 0, 0).apply {
            int(0); int(0); int(1000); int(0)          // times, timescale, duration
            int(0x00010000); short(0x0100); zeros(10)  // rate, volume, reserved
            matrix(this, 0)
            zeros(24)
            int(if (hasAudio) 3 else 2)                // next track ID
        })
        moov.box(videoTrak())
        if (hasAudio) moov.box(audioTrak())
        moov.box(Box("mvex").apply {
            box(trex(1))
            if (hasAudio) box(trex(2))
        })
        return ftyp.done() + moov.done()
    }

    private fun matrix(b: Box, degrees: Int) {
        val one = 0x00010000
        val m = when (degrees) {
            90 -> intArrayOf(0, one, 0, -one, 0, 0)
            180 -> intArrayOf(-one, 0, 0, 0, -one, 0)
            270 -> intArrayOf(0, -one, 0, one, 0, 0)
            else -> intArrayOf(one, 0, 0, 0, one, 0)
        }
        for (v in m) b.int(v)
        b.int(0); b.int(0); b.int(0x40000000)
    }

    private fun tkhd(track: Int, w: Int, h: Int, audio: Boolean) = full("tkhd", 0, 3).apply {
        int(0); int(0); int(track); int(0); int(0)     // times, track id, reserved, duration
        zeros(8); short(0); short(if (audio) 1 else 0)
        short(if (audio) 0x0100 else 0); short(0)
        matrix(this, if (audio) 0 else rotation)
        int(w shl 16); int(h shl 16)
    }

    private fun mdhd(timescale: Int) = full("mdhd", 0, 0).apply {
        int(0); int(0); int(timescale); int(0); short(0x55c4); short(0)
    }

    private fun hdlr(type: String, name: String) = full("hdlr", 0, 0).apply {
        int(0); bytes(type.toByteArray()); zeros(12); bytes(name.toByteArray()); byte(0)
    }

    private fun dinf() = Box("dinf").apply {
        box(full("dref", 0, 0).apply { int(1); box(full("url ", 0, 1)) })
    }

    private fun emptyTables(b: Box) {
        b.box(full("stts", 0, 0).apply { int(0) })
        b.box(full("stsc", 0, 0).apply { int(0) })
        b.box(full("stsz", 0, 0).apply { int(0); int(0) })
        b.box(full("stco", 0, 0).apply { int(0) })
    }

    private fun videoTrak(): Box {
        val s = sps!!
        val p = pps!!
        val avcC = Box("avcC").apply {
            byte(1); byte(s[1].toInt()); byte(s[2].toInt()); byte(s[3].toInt())
            byte(0xFF); byte(0xE1); short(s.size); bytes(s)
            byte(1); short(p.size); bytes(p)
        }
        val avc1 = Box("avc1").apply {
            zeros(6); short(1)                         // reserved, data reference index
            zeros(16)                                  // pre_defined + reserved
            short(width); short(height)
            int(0x00480000); int(0x00480000); int(0)
            short(1); zeros(32); short(0x18); short(0xFFFF)
            box(avcC)
        }
        val stbl = Box("stbl").apply {
            box(full("stsd", 0, 0).apply { int(1); box(avc1) })
            emptyTables(this)
        }
        val minf = Box("minf").apply {
            box(full("vmhd", 0, 1).apply { zeros(8) })
            box(dinf()); box(stbl)
        }
        val mdia = Box("mdia").apply { box(mdhd(VIDEO_TIMESCALE.toInt())); box(hdlr("vide", "VideoHandler")); box(minf) }
        return Box("trak").apply { box(tkhd(1, width, height, false)); box(mdia) }
    }

    private fun audioTrak(): Box {
        val config = asc!!
        fun descriptor(tag: Int, body: ByteArray): ByteArray {
            val o = ByteArrayOutputStream()
            o.write(tag)
            o.write(0x80); o.write(0x80); o.write(0x80); o.write(body.size)   // 4-byte size, like ffmpeg
            o.write(body)
            return o.toByteArray()
        }
        val decSpecific = descriptor(5, config)
        val decConfig = descriptor(4, ByteArrayOutputStream().apply {
            write(0x40); write(0x15)                   // AAC, audio stream
            write(0); write(0); write(0)               // buffer size
            for (i in 0 until 2) { write(0); write(1); write(0xF4); write(0) }  // max/avg bitrate (128k)
            write(decSpecific)
        }.toByteArray())
        val sl = descriptor(6, byteArrayOf(2))
        val es = descriptor(3, ByteArrayOutputStream().apply {
            write(0); write(2); write(0)               // ES_ID, flags
            write(decConfig); write(sl)
        }.toByteArray())
        val mp4a = Box("mp4a").apply {
            zeros(6); short(1); zeros(8)
            short(audioChannels); short(16); short(0); short(0)
            int(audioRate shl 16)
            box(full("esds", 0, 0).apply { bytes(es) })
        }
        val stbl = Box("stbl").apply {
            box(full("stsd", 0, 0).apply { int(1); box(mp4a) })
            emptyTables(this)
        }
        val minf = Box("minf").apply {
            box(full("smhd", 0, 0).apply { int(0) })
            box(dinf()); box(stbl)
        }
        val mdia = Box("mdia").apply { box(mdhd(audioRate)); box(hdlr("soun", "SoundHandler")); box(minf) }
        return Box("trak").apply { box(tkhd(2, 0, 0, true)); box(mdia) }
    }

    private fun trex(track: Int) = full("trex", 0, 0).apply { int(track); int(1); int(0); int(0); int(0) }

    private fun buildFragment(v: List<Sample>, a: List<Sample>): ByteArray {
        val videoBytes = v.sumOf { it.data.size }
        fun traf(track: Int, samples: List<Sample>, defaultFlags: Int, firstFlags: Int?, dataOffset: Int): Box {
            return Box("traf").apply {
                box(full("tfhd", 0, 0x020020).apply { int(track); int(defaultFlags) })
                box(full("tfdt", 1, 0).apply { long(samples.first().dts) })
                val flags = 0x000301 or (if (firstFlags != null) 0x4 else 0)
                box(full("trun", 0, flags).apply {
                    int(samples.size); int(dataOffset)
                    if (firstFlags != null) int(firstFlags)
                    for (s in samples) { int(s.duration.toInt()); int(s.data.size) }
                })
            }
        }
        // the data offsets depend on the moof size, which does not depend on their values: build twice
        var moof = ByteArray(0)
        for (pass in 0 until 2) {
            val base = moof.size + 8
            val m = Box("moof")
            m.box(full("mfhd", 0, 0).apply { int(sequence) })
            m.box(traf(1, v, NON_SYNC, if (v.first().key) SYNC else null, base))
            if (a.isNotEmpty()) m.box(traf(2, a, SYNC, null, base + videoBytes))
            moof = m.done()
        }
        sequence++
        val mdat = ByteArrayOutputStream(moof.size + 8 + videoBytes + a.sumOf { it.data.size })
        mdat.write(moof)
        val size = 8 + videoBytes + a.sumOf { it.data.size }
        mdat.write(byteArrayOf((size ushr 24).toByte(), (size ushr 16).toByte(), (size ushr 8).toByte(), size.toByte()))
        mdat.write("mdat".toByteArray())
        for (s in v) mdat.write(s.data)
        for (s in a) mdat.write(s.data)
        return mdat.toByteArray()
    }

    companion object {
        const val VIDEO_TIMESCALE = 90000L
        private const val SYNC = 0x02000000          // sample_depends_on = 2 (I-frame)
        private const val NON_SYNC = 0x01010000      // depends on others, is_non_sync_sample

        /** NAL units of an Annex-B buffer, without start codes. */
        fun nals(b: ByteArray): List<ByteArray> {
            val out = ArrayList<ByteArray>()
            var i = 0
            var start = -1
            while (i + 2 < b.size) {
                if (b[i].toInt() == 0 && b[i + 1].toInt() == 0 && b[i + 2].toInt() == 1) {
                    if (start >= 0) {
                        var end = i
                        while (end > start && b[end - 1].toInt() == 0) end--
                        if (end > start) out.add(b.copyOfRange(start, end))
                    }
                    i += 3
                    start = i
                } else i++
            }
            if (start >= 0 && start < b.size) out.add(b.copyOfRange(start, b.size))
            if (start < 0 && b.isNotEmpty()) out.add(b)  // no start code: already a single NAL
            return out
        }

        /** Annex-B access unit -> 4-byte length-prefixed NAL units (parameter sets and AUDs removed). */
        fun toLengthPrefixed(annexB: ByteArray): ByteArray {
            val o = ByteArrayOutputStream(annexB.size + 16)
            for (nal in nals(annexB)) {
                val type = nal[0].toInt() and 31
                if (type == 7 || type == 8 || type == 9) continue
                val n = nal.size
                o.write(n ushr 24); o.write(n ushr 16 and 255); o.write(n ushr 8 and 255); o.write(n and 255)
                o.write(nal)
            }
            return o.toByteArray()
        }
    }
}
