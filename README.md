# OmniSave Studio — Standalone Android App (v20.12)

Standalone Android APK wrapper for OmniSave Studio using **Kotlin + Chaquopy (Python 3.11 + yt-dlp) + ARM64 FFmpeg + Foreground Service + Share Intent + Built-in Cookie Login**.

## Build Automatically on GitHub Actions (No Android Studio Needed)

1. Create a **Private** repository on GitHub (e.g. `OmniSave-Android`).
2. Upload all files from this folder (make sure `.github/workflows/build-apk.yml` is included!).
3. Open the **Actions** tab -> click **Build OmniSave Android APK** -> **Run workflow**.
4. Wait ~4–6 minutes for the green checkmark, then download **OmniSave-APK** from **Artifacts**.
5. Install `OmniSave-v20.12-arm64.apk` on your Android phone and grant **All files access** + **Notifications**.

## Updating Later
- **Update yt-dlp:** Open Settings (⚙) inside the app and tap **Update yt-dlp** (updates directly without rebuilding the APK).
- **Update OmniSave engine:** Replace `app/src/main/python/omnisave_studio.py` on GitHub and GitHub Actions will build a fresh APK automatically.
