package ir.eris.cctv

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Range
import android.util.Size
import android.view.Surface
import ir.eris.cctv.core.Mp4Muxer
import kotlin.math.abs

/**
 * Camera2 -> H.264 encoder (input surface) and microphone -> AAC encoder, packed by Mp4Muxer into the
 * fragmented MP4 stream the Eris CCTV server expects. Runs on its own threads; no preview is needed,
 * so it keeps working with the screen off (inside the foreground service).
 */
class Capture(private val context: Context, private val opt: Options, private val listener: Listener) {

    class Options(
        val back: Boolean, val width: Int, val height: Int, val fps: Int, val bitrate: Int,
        val audio: Boolean, val rotation: Int, val displayRotation: Int,
    )

    interface Listener {
        fun onInit(data: ByteArray, codec: String, width: Int, height: Int)
        fun onFragment(data: ByteArray, key: Boolean)
        fun onCaptureState(state: String, error: String)
    }

    private val thread = HandlerThread("eris-camera").apply { start() }
    private val handler = Handler(thread.looper)
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var encoder: MediaCodec? = null
    private var encoderSurface: Surface? = null
    private var preview: Surface? = null
    private var audioRecord: AudioRecord? = null
    private var audioEncoder: MediaCodec? = null
    private var muxer: Mp4Muxer? = null
    @Volatile private var running = false
    private var realtimeClock = false
    private var fpsRange: Range<Int>? = null
    private var afContinuous = false
    var width = opt.width; private set
    var height = opt.height; private set
    var frames = 0L; private set
    /** Clockwise degrees a viewer turns the picture to show it upright (stored in the stream). */
    @Volatile var rotation = 0; private set

    @SuppressLint("MissingPermission")
    fun start() {
        running = true
        handler.post {
            try {
                open()
            } catch (e: Exception) {
                fail("camera: " + (e.message ?: e.javaClass.simpleName))
            }
        }
    }

    fun stop() {
        running = false
        handler.post {
            try { session?.close() } catch (_: Exception) { }
            try { device?.close() } catch (_: Exception) { }
            session = null
            device = null
            releaseCodecs()
            thread.quitSafely()
        }
    }

    /** Show (or stop showing) the camera on a preview surface while streaming. */
    fun setPreview(surface: Surface?) {
        handler.post {
            preview = surface
            if (device != null && encoderSurface != null) startSession()
        }
    }

    /** Ask the encoder for a keyframe now, so a new viewer does not wait for the next one. */
    fun requestKeyFrame() {
        try {
            encoder?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
        } catch (_: Exception) { }
    }

    private fun fail(message: String) {
        if (!running) return
        running = false
        listener.onCaptureState("error", message)
    }

    // ------------------------------------------------------------------------------ camera
    @SuppressLint("MissingPermission")
    private fun open() {
        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val facing = if (opt.back) CameraCharacteristics.LENS_FACING_BACK else CameraCharacteristics.LENS_FACING_FRONT
        val id = manager.cameraIdList.firstOrNull {
            manager.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == facing
        } ?: manager.cameraIdList.firstOrNull() ?: throw IllegalStateException("no camera on this phone")
        val ch = manager.getCameraCharacteristics(id)
        val sensor = ch.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
        realtimeClock = ch.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE) ==
            CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME
        val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val sizes = map?.getOutputSizes(MediaCodec::class.java)?.toList() ?: emptyList()
        val size = pickSize(sizes)
        width = size.width
        height = size.height
        fpsRange = pickFps(ch.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)?.toList() ?: emptyList())
        afContinuous = ch.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES)
            ?.contains(CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO) == true
        rotation = if (opt.rotation >= 0) opt.rotation else {
            val d = opt.displayRotation
            if (facing == CameraCharacteristics.LENS_FACING_FRONT) (sensor + d) % 360 else (sensor - d + 360) % 360
        }
        val audio = opt.audio && startAudioEncoder()
        val fragmentUs = maxOf(100_000L, 2_000_000L / opt.fps)
        muxer = Mp4Muxer(width, height, rotation, if (audio) AUDIO_RATE else 0, 1, fragmentUs) { init, data, key ->
            if (init) listener.onInit(data, muxer?.codec ?: "", width, height) else listener.onFragment(data, key)
        }
        startVideoEncoder()
        if (audio) startAudioCapture()
        listener.onCaptureState("starting", "")
        manager.openCamera(id, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                if (!running) { camera.close(); return }
                device = camera
                startSession()
            }
            override fun onDisconnected(camera: CameraDevice) {
                camera.close()
                device = null
                fail("the camera was taken by another app or disconnected")
            }
            override fun onError(camera: CameraDevice, error: Int) {
                camera.close()
                device = null
                fail(when (error) {
                    ERROR_CAMERA_IN_USE, ERROR_MAX_CAMERAS_IN_USE -> "the camera is in use by another app"
                    ERROR_CAMERA_DISABLED -> "the camera is disabled by a device policy"
                    else -> "camera error $error"
                })
            }
        }, handler)
    }

    private fun pickSize(sizes: List<Size>): Size {
        if (sizes.isEmpty()) return Size(opt.width, opt.height)
        val enc = try {
            MediaCodec.createEncoderByType(MIME).let { c ->
                val caps = c.codecInfo.getCapabilitiesForType(MIME).videoCapabilities
                c.release()
                caps
            }
        } catch (_: Exception) { null }
        val usable = sizes.filter { s ->
            s.width % 16 == 0 && s.height % 8 == 0 && (enc == null || enc.isSizeSupported(s.width, s.height))
        }.ifEmpty { sizes }
        usable.firstOrNull { it.width == opt.width && it.height == opt.height }?.let { return it }
        val want = opt.width.toDouble() / opt.height
        return usable.minByOrNull { s ->
            abs(s.width.toDouble() / s.height - want) * 4 + abs(s.width * s.height - opt.width * opt.height) /
                (opt.width * opt.height).toDouble()
        } ?: usable[0]
    }

    private fun pickFps(ranges: List<Range<Int>>): Range<Int>? {
        val f = opt.fps
        return ranges.filter { it.upper == f }.minByOrNull { it.upper - it.lower }   // [f, f] or [x, f]
            ?: ranges.filter { it.lower <= f && it.upper >= f }.minByOrNull { it.upper - it.lower }
            ?: ranges.minByOrNull { abs(it.upper - f) }
    }

    private fun startSession() {
        val dev = device ?: return
        val enc = encoderSurface ?: return
        try { session?.close() } catch (_: Exception) { }
        session = null
        val targets = listOfNotNull(enc, preview)
        try {
            createSession(dev, targets)
        } catch (e: Exception) {
            // on the camera thread an uncaught error would stop the whole app
            fail("camera session: " + (e.message ?: e.javaClass.simpleName))
        }
    }

    private fun createSession(dev: CameraDevice, targets: List<Surface>) {
        @Suppress("DEPRECATION")
        dev.createCaptureSession(targets, object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                if (!running || device == null) { s.close(); return }
                session = s
                try {
                    val req = dev.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                        for (t in targets) addTarget(t)
                        fpsRange?.let { set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
                        set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                        if (afContinuous) set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                    }
                    s.setRepeatingRequest(req.build(), null, handler)
                    listener.onCaptureState("running", "")
                } catch (e: Exception) {
                    fail("camera session: " + (e.message ?: e.javaClass.simpleName))
                }
            }
            override fun onConfigureFailed(s: CameraCaptureSession) {
                if (preview != null) {
                    preview = null       // some phones cannot add a preview to a recording session
                    startSession()
                } else fail("the camera could not be configured for $width x $height")
            }
        }, handler)
    }

    // ------------------------------------------------------------------------------ encoders
    private fun startVideoEncoder() {
        val codec = MediaCodec.createEncoderByType(MIME)
        fun format(baseline: Boolean) = MediaFormat.createVideoFormat(MIME, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, opt.bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, opt.fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
            if (baseline) {
                setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline)
                setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel31)
            }
            if (Build.VERSION.SDK_INT >= 23) setInteger(MediaFormat.KEY_PRIORITY, 0)
            if (Build.VERSION.SDK_INT >= 29) setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0)
        }
        try {
            codec.configure(format(true), null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        } catch (_: Exception) {
            codec.reset()
            codec.configure(format(false), null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        }
        encoderSurface = codec.createInputSurface()
        codec.start()
        encoder = codec
        Thread({ drainVideo(codec) }, "eris-video-out").apply { isDaemon = true; start() }
    }

    private fun drainVideo(codec: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        try {
            while (running) {
                val i = codec.dequeueOutputBuffer(info, 50_000)
                if (i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    // some encoders only put SPS/PPS into the output format (csd-0 / csd-1)
                    val f = codec.outputFormat
                    val csd = listOf("csd-0", "csd-1").mapNotNull { k ->
                        if (f.containsKey(k)) f.getByteBuffer(k)?.let { bb -> ByteArray(bb.remaining()).also { bb.duplicate().get(it) } } else null
                    }
                    if (csd.isNotEmpty()) muxer?.setVideoConfig(csd.reduce { a, b -> a + b })
                    continue
                }
                if (i < 0) continue
                val buf = codec.getOutputBuffer(i)
                if (buf != null && info.size > 0) {
                    val data = ByteArray(info.size)
                    buf.position(info.offset)
                    buf.get(data)
                    val m = muxer
                    if (m != null) {
                        if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) m.setVideoConfig(data)
                        else {
                            frames++
                            m.addVideo(data, info.presentationTimeUs, info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0)
                        }
                    }
                }
                codec.releaseOutputBuffer(i, false)
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) break
            }
        } catch (e: Exception) {
            if (running) handler.post { fail("video encoder: " + (e.message ?: e.javaClass.simpleName)) }
        }
    }

    private fun startAudioEncoder(): Boolean {
        return try {
            val f = MediaFormat.createAudioFormat("audio/mp4a-latm", AUDIO_RATE, 1).apply {
                setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
                setInteger(MediaFormat.KEY_BIT_RATE, 64_000)
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16384)
            }
            val c = MediaCodec.createEncoderByType("audio/mp4a-latm")
            c.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            c.start()
            audioEncoder = c
            true
        } catch (_: Exception) {
            audioEncoder = null
            false
        }
    }

    @SuppressLint("MissingPermission")
    private fun startAudioCapture() {
        val enc = audioEncoder ?: return
        val min = AudioRecord.getMinBufferSize(AUDIO_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(MediaRecorder.AudioSource.CAMCORDER, AUDIO_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(min, 8192) * 2)
        } catch (_: Exception) { null }
        if (rec == null || rec.state != AudioRecord.STATE_INITIALIZED) {
            rec?.release()
            return
        }
        audioRecord = rec
        rec.startRecording()
        Thread({ pumpAudio(rec, enc) }, "eris-audio-in").apply { isDaemon = true; start() }
        Thread({ drainAudio(enc) }, "eris-audio-out").apply { isDaemon = true; start() }
    }

    private fun clockUs(): Long =
        (if (realtimeClock) SystemClock.elapsedRealtimeNanos() else System.nanoTime()) / 1000

    private fun pumpAudio(rec: AudioRecord, enc: MediaCodec) {
        val pcm = ByteArray(2048)   // 1024 samples, 16-bit mono
        try {
            while (running) {
                val n = rec.read(pcm, 0, pcm.size)
                if (n <= 0) continue
                val pts = clockUs() - n / 2 * 1_000_000L / AUDIO_RATE
                val i = enc.dequeueInputBuffer(20_000)
                if (i < 0) continue
                val buf = enc.getInputBuffer(i) ?: continue
                buf.clear()
                val len = minOf(n, buf.remaining())
                buf.put(pcm, 0, len)
                enc.queueInputBuffer(i, 0, len, pts, 0)
            }
        } catch (_: Exception) { }
    }

    private fun drainAudio(enc: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        try {
            while (running) {
                val i = enc.dequeueOutputBuffer(info, 50_000)
                if (i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    val f = enc.outputFormat
                    if (f.containsKey("csd-0")) f.getByteBuffer("csd-0")?.let { bb ->
                        muxer?.setAudioConfig(ByteArray(bb.remaining()).also { bb.duplicate().get(it) })
                    }
                    continue
                }
                if (i < 0) continue
                val buf = enc.getOutputBuffer(i)
                if (buf != null && info.size > 0) {
                    val data = ByteArray(info.size)
                    buf.position(info.offset)
                    buf.get(data)
                    if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) muxer?.setAudioConfig(data)
                    else muxer?.addAudio(data, info.presentationTimeUs)
                }
                enc.releaseOutputBuffer(i, false)
            }
        } catch (_: Exception) { }
    }

    private fun releaseCodecs() {
        try { audioRecord?.stop() } catch (_: Exception) { }
        try { audioRecord?.release() } catch (_: Exception) { }
        audioRecord = null
        for (c in listOfNotNull(encoder, audioEncoder)) {
            try { c.stop() } catch (_: Exception) { }
            try { c.release() } catch (_: Exception) { }
        }
        encoder = null
        audioEncoder = null
        try { encoderSurface?.release() } catch (_: Exception) { }
        encoderSurface = null
    }

    companion object {
        private const val MIME = "video/avc"
        const val AUDIO_RATE = 48000
    }
}
