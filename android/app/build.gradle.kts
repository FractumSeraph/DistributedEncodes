plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// The worker script the app ships with: the repository's worker_template.py.
// The app copies it into its files folder; from then on the worker updates
// itself from the manager like any other worker.
val bundledWorkerDir = layout.buildDirectory.dir("generated/bundledWorker")
val copyWorker by tasks.registering(Copy::class) {
    from(rootProject.file("../worker_template.py"))
    rename { "worker.py" }
    into(bundledWorkerDir)
}

android {
    namespace = "net.fractumseraph.worker"
    compileSdk = 35

    defaultConfig {
        applicationId = "net.fractumseraph.worker"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    // Release signing: set FRACTUM_KEYSTORE (path), FRACTUM_KEYSTORE_PASSWORD,
    // FRACTUM_KEY_ALIAS and FRACTUM_KEY_PASSWORD. Without them the release
    // build is signed with the debug key (fine for testing; Android only
    // installs an update signed with the same key as the installed app).
    signingConfigs {
        create("release") {
            val ks = System.getenv("FRACTUM_KEYSTORE")
            if (ks != null) {
                storeFile = file(ks)
                storePassword = System.getenv("FRACTUM_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("FRACTUM_KEY_ALIAS")
                keyPassword = System.getenv("FRACTUM_KEY_PASSWORD")
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (System.getenv("FRACTUM_KEYSTORE") != null)
                signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
    }

    sourceSets["main"].assets.srcDir(bundledWorkerDir)

    // FFmpeg ships as executables named lib*.so (see ffmpeg/build.sh). They
    // must be extracted to the native-library folder to be runnable.
    packaging { jniLibs { useLegacyPackaging = true } }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
}

tasks.named("preBuild") { dependsOn(copyWorker) }

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            install("requests")
            install("certifi")
        }
    }
}
