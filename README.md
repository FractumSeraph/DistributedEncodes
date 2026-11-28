# Fractum Worker for Android: test builds

This branch only holds built APKs of the Android app (source code: `android/` on `main`).

| File | Version | Notes |
|---|---|---|
| [FractumWorker-1.2.0.apk](FractumWorker-1.2.0.apk) | 1.2.0 | **Latest. First build signed with the permanent key**: uninstall a 1.0/1.1 test build once, then install this. From here on the app updates itself from the GitHub releases. Site theme and live stats. |
| [FractumWorker-1.1.1.apk](FractumWorker-1.1.1.apk) | 1.1.1 | Test key. Fixes Start failing when the manager serves an older worker (it no longer downgrades). |
| [FractumWorker-1.1.0.apk](FractumWorker-1.1.0.apk) | 1.1.0 | Auto-start ("Work automatically"). 64-bit ARM, Android 7+. |

1.0.0-1.1.1 are test builds signed with a temporary key; 1.2.0 and the GitHub releases use the permanent key. Checksums are in `SHA256SUMS`.
