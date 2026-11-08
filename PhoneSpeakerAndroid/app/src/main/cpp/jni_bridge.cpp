#include "native_audio.h"
#include <jni.h>
#include <mutex>
#include <algorithm>

static NativeAudioEngine* gAudioEngine = nullptr;
static std::mutex gEngineMutex;

// Cached settings so UI changes made before nativeStart() are preserved
static float gSavedTargetMs = 30.0f;
static bool gSavedAutoMode = true;
static bool gSavedMuted = false;
static float gSavedVolumeGain = 1.0f;
static bool gSavedLowLatency = true;

extern "C" {

JNIEXPORT jboolean JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeStart(JNIEnv* /*env*/, jobject /*thiz*/, jint port) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    if (!gAudioEngine) {
        gAudioEngine = new NativeAudioEngine();
    }
    gAudioEngine->setTargetBufferMs(gSavedTargetMs);
    gAudioEngine->setAutoMode(gSavedAutoMode);
    gAudioEngine->setMuted(gSavedMuted);
    gAudioEngine->setVolumeGain(gSavedVolumeGain);
    gAudioEngine->setLowLatencyMode(gSavedLowLatency);
    return gAudioEngine->start(port) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT void JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeStop(JNIEnv* /*env*/, jobject /*thiz*/) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    if (gAudioEngine) {
        gAudioEngine->stop();
        delete gAudioEngine;
        gAudioEngine = nullptr;
    }
}

JNIEXPORT void JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeSetTargetBufferMs(JNIEnv* /*env*/, jobject /*thiz*/, jfloat targetMs) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    gSavedTargetMs = std::clamp(targetMs, 10.0f, 200.0f);
    if (gAudioEngine) {
        gAudioEngine->setTargetBufferMs(gSavedTargetMs);
    }
}

JNIEXPORT jfloat JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeGetTargetBufferMs(JNIEnv* /*env*/, jobject /*thiz*/) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    if (gAudioEngine) {
        return gAudioEngine->getTargetBufferMs();
    }
    return gSavedTargetMs;
}

JNIEXPORT void JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeSetAutoMode(JNIEnv* /*env*/, jobject /*thiz*/, jboolean enabled) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    gSavedAutoMode = (enabled == JNI_TRUE);
    if (gAudioEngine) {
        gAudioEngine->setAutoMode(gSavedAutoMode);
    }
}

JNIEXPORT jboolean JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeIsAutoMode(JNIEnv* /*env*/, jobject /*thiz*/) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    if (gAudioEngine) {
        return gAudioEngine->isAutoMode() ? JNI_TRUE : JNI_FALSE;
    }
    return gSavedAutoMode ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT void JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeSetMuted(JNIEnv* /*env*/, jobject /*thiz*/, jboolean muted) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    gSavedMuted = (muted == JNI_TRUE);
    if (gAudioEngine) {
        gAudioEngine->setMuted(gSavedMuted);
    }
}

JNIEXPORT jboolean JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeIsMuted(JNIEnv* /*env*/, jobject /*thiz*/) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    if (gAudioEngine) {
        return gAudioEngine->isMuted() ? JNI_TRUE : JNI_FALSE;
    }
    return gSavedMuted ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT void JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeSetVolumeGain(JNIEnv* /*env*/, jobject /*thiz*/, jfloat gain) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    gSavedVolumeGain = std::clamp(gain, 0.0f, 2.0f);
    if (gAudioEngine) {
        gAudioEngine->setVolumeGain(gSavedVolumeGain);
    }
}

JNIEXPORT jfloat JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeGetVolumeGain(JNIEnv* /*env*/, jobject /*thiz*/) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    if (gAudioEngine) {
        return gAudioEngine->getVolumeGain();
    }
    return gSavedVolumeGain;
}

JNIEXPORT void JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeSetLowLatencyMode(JNIEnv* /*env*/, jobject /*thiz*/, jboolean lowLatency) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    gSavedLowLatency = (lowLatency == JNI_TRUE);
    if (gAudioEngine) {
        gAudioEngine->setLowLatencyMode(gSavedLowLatency);
    }
}

JNIEXPORT jint JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeGetAudioSessionId(JNIEnv* /*env*/, jobject /*thiz*/) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    if (gAudioEngine) {
        return gAudioEngine->getAudioSessionId();
    }
    return 0;
}

JNIEXPORT jfloatArray JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeGetStats(JNIEnv* env, jobject /*thiz*/) {
    jfloatArray result = env->NewFloatArray(8);
    if (!result) return nullptr;

    jfloat statsArr[8] = {0.0f};
    statsArr[1] = gSavedTargetMs;
    statsArr[7] = gSavedAutoMode ? 1.0f : 0.0f;
    {
        std::lock_guard<std::mutex> lock(gEngineMutex);
        if (gAudioEngine) {
            JitterStats stats = gAudioEngine->getStats();
            statsArr[0] = stats.buffer_ms;
            statsArr[1] = stats.target_ms;
            statsArr[2] = static_cast<float>(stats.underruns);
            statsArr[3] = static_cast<float>(stats.lost_packets);
            statsArr[4] = static_cast<float>(stats.late_packets);
            statsArr[5] = static_cast<float>(stats.rx_pps);
            statsArr[6] = static_cast<float>(stats.active_codec);
            statsArr[7] = stats.auto_mode ? 1.0f : 0.0f;
        }
    }

    env->SetFloatArrayRegion(result, 0, 8, statsArr);
    return result;
}

JNIEXPORT jstring JNICALL
Java_com_hasan_phonespeaker_AudioNative_nativeGetSenderIp(JNIEnv* env, jobject /*thiz*/) {
    std::lock_guard<std::mutex> lock(gEngineMutex);
    if (gAudioEngine) {
        std::string ip = gAudioEngine->getSenderIp();
        if (!ip.empty()) {
            return env->NewStringUTF(ip.c_str());
        }
    }
    return nullptr;
}

} // extern "C"
