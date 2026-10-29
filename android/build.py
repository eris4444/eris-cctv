"""Builds the Eris CCTV Android app without Gradle: kotlinc -> d8 -> aapt2 -> zipalign -> apksigner.

Everything comes from the Android SDK (build-tools 34.0.0, platform 34), a JDK 17 and the Kotlin
compiler jars in the Gradle cache - no network access is needed.

usage:
  python build.py apk [--out PATH]        build and sign the APK
  python build.py core                    compile core + the JVM test tool into build/core.jar
Environment: ANDROID_SDK (default %LOCALAPPDATA%/Android/Sdk), JAVA_HOME, GRADLE_USER_HOME.
"""
import argparse
import glob
import os
import re
import secrets
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
APP = os.path.join(HERE, "app")
CORE_SRC = os.path.join(HERE, "core", "src")
TEST_SRC = os.path.join(HERE, "coretest", "src")
KOTLIN = "1.9.24"
MIN_SDK, TARGET_SDK = 24, 34


def sdk() -> str:
    return os.environ.get("ANDROID_SDK") or os.environ.get("ANDROID_HOME") or \
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Android", "Sdk")


def tool(*parts) -> str:
    p = os.path.join(sdk(), *parts)
    if not os.path.exists(p):
        sys.exit("missing: " + p)
    return p


def java(name="java") -> str:
    home = os.environ.get("JAVA_HOME")
    if home:
        exe = os.path.join(home, "bin", name + (".exe" if os.name == "nt" else ""))
        if os.path.exists(exe):
            return exe
    found = shutil.which(name)
    if not found:
        sys.exit(name + " not found (set JAVA_HOME to a JDK 17)")
    return found


def cached_jar(group: str, artifact: str, version: str) -> str:
    """A jar from the Gradle cache; with ERIS_ALLOW_DOWNLOAD=1 (CI) from Maven Central instead."""
    gh = os.environ.get("GRADLE_USER_HOME") or os.path.join(os.path.expanduser("~"), ".gradle")
    pattern = os.path.join(gh, "caches", "modules-2", "files-2.1", group, artifact, version, "*",
                           "%s-%s.jar" % (artifact, version))
    hits = glob.glob(pattern)
    if hits:
        return hits[0]
    dest = os.path.join(HERE, ".deps", "%s-%s.jar" % (artifact, version))
    if os.path.exists(dest):
        return dest
    if os.environ.get("ERIS_ALLOW_DOWNLOAD") != "1":
        sys.exit("not in the Gradle cache: %s:%s:%s (set ERIS_ALLOW_DOWNLOAD=1 to fetch it)" % (group, artifact, version))
    import urllib.request
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    url = "https://repo1.maven.org/maven2/%s/%s/%s/%s-%s.jar" % (group.replace(".", "/"), artifact, version,
                                                               artifact, version)
    print("downloading", url)
    urllib.request.urlretrieve(url, dest + ".tmp")
    os.replace(dest + ".tmp", dest)
    return dest


def kotlin_stdlib() -> str:
    return cached_jar("org.jetbrains.kotlin", "kotlin-stdlib", KOTLIN)


def kotlinc(sources: list, classpath: list, out: str) -> None:
    compiler = [cached_jar("org.jetbrains.kotlin", "kotlin-compiler-embeddable", KOTLIN),
                kotlin_stdlib(),
                cached_jar("org.jetbrains.kotlin", "kotlin-script-runtime", KOTLIN),
                cached_jar("org.jetbrains.kotlin", "kotlin-reflect", "1.6.10"),
                cached_jar("org.jetbrains.kotlin", "kotlin-daemon-embeddable", KOTLIN),
                cached_jar("org.jetbrains.intellij.deps", "trove4j", "1.0.20200330"),
                cached_jar("org.jetbrains", "annotations", "13.0")]
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    files = []
    for s in sources:
        files += glob.glob(os.path.join(s, "**", "*.kt"), recursive=True)
    cmd = [java(), "-Xss4m", "-cp", os.pathsep.join(compiler), "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
           "-no-stdlib", "-no-reflect", "-jvm-target", "1.8", "-nowarn",
           "-classpath", os.pathsep.join(classpath), "-d", out] + files
    print("kotlinc: %d files" % len(files))
    r = subprocess.run(cmd)
    if r.returncode:
        sys.exit("kotlin compilation failed")


def build_core() -> str:
    classes = os.path.join(BUILD, "core-classes")
    kotlinc([CORE_SRC, TEST_SRC], [kotlin_stdlib()], classes)
    jar = os.path.join(BUILD, "core.jar")
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _d, fs in os.walk(classes):
            for f in fs:
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, classes))
    print("core jar:", jar)
    return jar


def version() -> tuple:
    text = open(os.path.join(APP, "AndroidManifest.xml"), encoding="utf-8").read()
    name = re.search(r'versionName="([^"]+)"', text).group(1)
    code = re.search(r'versionCode="(\d+)"', text).group(1)
    return name, code


def keystore() -> tuple:
    """Release keystore next to the project (kept out of git). Created on first use."""
    ks = os.path.join(HERE, "release.jks")
    props = os.path.join(HERE, "keystore.properties")
    if os.path.exists(ks) and os.path.exists(props):
        pw = dict(line.strip().split("=", 1) for line in open(props) if "=" in line)["password"]
        return ks, pw
    pw = secrets.token_urlsafe(18)
    subprocess.run([java("keytool"), "-genkeypair", "-keystore", ks, "-storepass", pw, "-keypass", pw,
                    "-alias", "eriscctv", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                    "-dname", "CN=Eris CCTV, O=ERIS Tech"], check=True)
    with open(props, "w") as f:
        f.write("password=%s\n" % pw)
    print("created signing key:", ks, "(keep it - updates must be signed with the same key)")
    return ks, pw


def build_apk(out: str) -> str:
    bt = os.path.join(sdk(), "build-tools", "34.0.0")
    android_jar = tool("platforms", "android-34", "android.jar")
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
    # 1. resources + manifest -> base apk and R.java
    flat = os.path.join(BUILD, "res.zip")
    subprocess.run([os.path.join(bt, "aapt2"), "compile", "--dir", os.path.join(APP, "res"), "-o", flat], check=True)
    name, code = version()
    base = os.path.join(BUILD, "base.apk")
    gen = os.path.join(BUILD, "gen")
    os.makedirs(gen)
    subprocess.run([os.path.join(bt, "aapt2"), "link", "-o", base, "-I", android_jar,
                    "--manifest", os.path.join(APP, "AndroidManifest.xml"), "--java", gen,
                    "--min-sdk-version", str(MIN_SDK), "--target-sdk-version", str(TARGET_SDK),
                    "--version-name", name, "--version-code", code, flat], check=True)
    # 2. R.java -> classes
    rclasses = os.path.join(BUILD, "r-classes")
    os.makedirs(rclasses)
    subprocess.run([java("javac"), "-source", "8", "-target", "8", "-nowarn", "-Xlint:-options", "-encoding", "UTF-8",
                    "-cp", android_jar, "-d", rclasses] +
                   glob.glob(os.path.join(gen, "**", "*.java"), recursive=True), check=True)
    # 3. Kotlin -> classes
    classes = os.path.join(BUILD, "classes")
    kotlinc([CORE_SRC, os.path.join(APP, "src")], [android_jar, kotlin_stdlib(), rclasses], classes)
    # 4. classes + stdlib -> dex
    dex = os.path.join(BUILD, "dex")
    os.makedirs(dex)
    class_files = glob.glob(os.path.join(classes, "**", "*.class"), recursive=True) + \
        glob.glob(os.path.join(rclasses, "**", "*.class"), recursive=True)
    argfile = os.path.join(BUILD, "d8-inputs.txt")
    with open(argfile, "w", encoding="utf-8") as f:
        f.write("\n".join(class_files + [kotlin_stdlib()]))
    subprocess.run([java(), "-cp", os.path.join(bt, "lib", "d8.jar"), "com.android.tools.r8.D8", "--release",
                    "--min-api", str(MIN_SDK), "--lib", android_jar, "--output", dex, "@" + argfile], check=True)
    # 5. add the dex files
    with zipfile.ZipFile(base, "a", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(dex)):
            z.write(os.path.join(dex, f), f)
    # 6. align + sign
    aligned = os.path.join(BUILD, "aligned.apk")
    subprocess.run([os.path.join(bt, "zipalign"), "-f", "-p", "4", base, aligned], check=True)
    ks, pw = keystore()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    subprocess.run([java(), "-jar", os.path.join(bt, "lib", "apksigner.jar"), "sign", "--ks", ks,
                    "--ks-pass", "pass:" + pw, "--key-pass", "pass:" + pw, "--ks-key-alias", "eriscctv",
                    "--out", out, aligned], check=True)
    subprocess.run([java(), "-jar", os.path.join(bt, "lib", "apksigner.jar"), "verify", "--print-certs", out],
                   check=True, stdout=subprocess.DEVNULL)
    print("APK: %s (%d KB, version %s / %s)" % (out, os.path.getsize(out) // 1024, name, code))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["apk", "core"])
    ap.add_argument("--out", default=os.path.join(BUILD, "out", "eris-cctv-camera.apk"))
    a = ap.parse_args()
    if a.what == "core":
        os.makedirs(BUILD, exist_ok=True)
        build_core()
    else:
        build_apk(a.out)


if __name__ == "__main__":
    main()
