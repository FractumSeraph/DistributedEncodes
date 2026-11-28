# Fractum Worker for Android

An Android app that runs the regular worker (`worker_template.py`, the same script every desktop worker runs) with its own FFmpeg build, so tablets and phones can encode natively instead of in a browser tab.

- **Much faster than the browser worker.** Native ARM code with NEON/SVE instead of WebAssembly. No 4 GB memory limit, and sources are streamed from the server like on a PC.
- **Runs in the background** as a foreground service with a notification, with the screen off, and keeps the CPU and Wi-Fi awake while it works.
- **Device-friendly by default.** It works only while charging, only on Wi-Fi (unmetered networks), and pauses above 40 °C battery temperature (resuming 3 °C lower). All three can be changed in the app. Pausing freezes the encode, and it continues where it stopped.
- **Same rules as every worker.** It updates itself from the manager, uses the same encoding settings, and shows on the dashboard as `<name>-<device>`.

Requirements: a 64-bit ARM device (`arm64-v8a`) running Android 7.0 (API 24) or newer. Encoding at preset 2 is heavy: expect a 10-minute chunk to take hours on a tablet, and run one encode at a time unless the device has many fast cores.

## Using it

1. Install the APK (from the manager's dashboard, `/dl/android`, when the server has one). Android asks to allow installing apps from your browser the first time.
2. Enter your name (shown on the scoreboard) and, if you like, a device name and FractumCoin wallet.
3. Press **Let it keep running in the background** once and allow it. Without this, some manufacturers' battery savers stop the app after a while.
4. Press **Start**. The notification shows what it is doing. **Finish & stop** completes the work in hand first (this can take a while). **Stop now** hands it back to the server straight away.

## How it fits together

| Part | What it is |
|---|---|
| `app/src/main/python/android_worker.py` | Loads `worker.py` inside the app (embedded Python, [Chaquopy](https://chaquo.com/chaquopy/)) in *embedded* mode: no `os.execv` self-restart (the driver reloads the script after an update), no keyboard pause menu, and no signal handlers. It also captures the output for the screen and gives FFmpeg its CA bundle. |
| `app/src/main/java/.../WorkerService.kt` | Foreground service: wake lock and Wi-Fi lock, and the charging, Wi-Fi and temperature rules (it calls the driver's `pause()` and `resume()`, which freeze FFmpeg just like the keyboard pause on a PC). |
| `app/src/main/java/.../MainActivity.kt` | Settings, start/stop buttons, status, and the live log. |
| `ffmpeg/build.sh` | Builds `ffmpeg` and `ffprobe` for arm64. |
| `worker_template.py` (repository root) | Copied into the APK at build time. The app installs it into its files folder, and from then on the worker updates itself from the manager. |

The FFmpeg build:
- **Versions:** FFmpeg 9.1 with SVT-AV1 4.3.0, Opus 1.5.2 and mbedTLS 3.6.4 (for HTTPS).
- **Linking:** everything is linked in except Android's C library.
- **File names:** the binaries are stored as `jniLibs/arm64-v8a/libffmpeg.so` and `libffprobe.so`. Android only lets an app run executables from its native-library folder, and only `lib*.so` files are put there.
- **Certificates:** FFmpeg verifies HTTPS certificates. With mbedTLS it has no system CA store, so `build.sh` patches it to use the file named by `SSL_CERT_FILE` when `-ca_file` is not given. The app points that at Mozilla's CA bundle (from Python's `certifi`).

## Building

Needs a Linux machine with JDK 17+, the Android SDK (platform 35, build-tools 35) and NDK r28 or newer, plus `cmake`, `ninja`, `make`, `pkg-config` and `python3.11`. Chaquopy builds with a Python of the same version as the app's, which is 3.11.

```
cd android
ANDROID_NDK_HOME=$ANDROID_HOME/ndk/<version> ffmpeg/build.sh /tmp/ffbuild   # ~15-30 min, once
echo "sdk.dir=$ANDROID_HOME" > local.properties
./gradlew assembleRelease
# -> app/build/outputs/apk/release/app-release.apk
```

Copy the APK next to `manager.py` as `FractumWorker.apk` (or set `ANDROID_APK_FILE` in `config.py`). The dashboard then offers it, and `/dl/android` serves it.

**Signing.** Without the variables below, the release APK is signed with the build machine's debug key. That is fine for testing, but Android installs an update over an installed app only when both are signed with the same key. For releases, make one keystore, keep it safe, and build with:

```
FRACTUM_KEYSTORE=/path/release.jks FRACTUM_KEYSTORE_PASSWORD=... \
FRACTUM_KEY_ALIAS=fractum FRACTUM_KEY_PASSWORD=... ./gradlew assembleRelease
```

Bump `versionCode` in `app/build.gradle.kts` for every release. The worker script inside does not need an app release: the worker updates itself from the manager.

**Testing FFmpeg without a device.** `LINK=static ffmpeg/build.sh /tmp/ffbuild` builds fully static binaries into `/tmp/ffbuild/static`. They run on an x86 PC under `qemu-aarch64-static`, which is how the build was checked: version, encoders, a preset 2 / CRF 57 / 480p encode read over HTTPS, and certificate checks.
