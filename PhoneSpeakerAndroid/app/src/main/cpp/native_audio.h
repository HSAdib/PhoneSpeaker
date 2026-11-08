#pragma once

#include "jitter_buffer.h"
#include <oboe/Oboe.h>
#include <thread>
#include <atomic>
#include <memory>
#include <netinet/in.h>

class NativeAudioEngine : public oboe::AudioStreamDataCallback,
                          public oboe::AudioStreamErrorCallback {
public:
    NativeAudioEngine();
    ~NativeAudioEngine();

    bool start(int audio_port = 5004);
    void stop();

    void setTargetBufferMs(float target_ms);
    float getTargetBufferMs();
    void setAutoMode(bool enabled);
    bool isAutoMode();
    void setMuted(bool muted);
    bool isMuted() const;
    void setVolumeGain(float gain);
    float getVolumeGain() const;
    void setLowLatencyMode(bool lowLatency);
    bool isLowLatencyMode() const;
    int32_t getAudioSessionId() const;

    JitterStats getStats();
    std::string getSenderIp();

    // Oboe AudioStreamDataCallback
    oboe::DataCallbackResult onAudioReady(oboe::AudioStream* stream, void* audioData, int32_t numFrames) override;

    // Oboe AudioStreamErrorCallback
    void onErrorAfterClose(oboe::AudioStream* stream, oboe::Result result) override;

private:
    bool openAndStartOboeStream();
    void closeOboeStream();
    void scheduleStreamReopen();
    void socketReceiverLoop();

    int mAudioPort{5004};
    std::atomic<bool> mIsRunning{false};
    std::atomic<bool> mMuted{false};
    std::atomic<float> mVolumeGain{1.0f};
    std::atomic<bool> mLowLatencyMode{true};
    std::atomic<int> mSocketFd{-1};
    std::mutex mSenderIpMutex;
    std::string mLastSenderIp;

    std::mutex mStreamMutex;
    std::mutex mReopenMutex;
    std::atomic<bool> mReopenPending{false};
    std::thread mReceiverThread;
    std::thread mReopenThread;
    std::shared_ptr<oboe::AudioStream> mAudioStream;

    JitterBuffer mJitterBuffer;
};

