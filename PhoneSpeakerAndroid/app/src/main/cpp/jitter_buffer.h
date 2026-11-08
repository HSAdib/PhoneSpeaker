#pragma once

#include "protocol.h"
#include "opus_wrapper.h"
#include <mutex>
#include <map>
#include <vector>
#include <atomic>
#include <cstring>
#include <chrono>

struct JitterStats {
    float buffer_ms{0.0f};
    float target_ms{30.0f};
    uint32_t underruns{0};
    uint32_t lost_packets{0};
    uint32_t late_packets{0};
    uint32_t rx_pps{0};
    uint8_t active_codec{0};
    bool auto_mode{true};
};

class JitterBuffer {
public:
    JitterBuffer();
    ~JitterBuffer();

    void pushPacket(const AudioPacket& packet);
    int readAudio(int16_t* out_samples, int num_frames);

    void setTargetBufferMs(float target_ms);
    float getTargetBufferMs() const;
    void setAutoMode(bool enabled);
    bool isAutoMode() const;
    void reset();

    JitterStats getStats();

private:
    void adaptTargetBuffer();
    void popNextPacketOrConceal();
    void concealPcmGap(int frames);
    void concealOpusGap(int frames, uint32_t missing_seq);

    mutable std::mutex mMutex;
    std::map<uint32_t, AudioPacket> mPacketMap;
    std::vector<int16_t> mPcmFifo;

    bool mHasStarted{false};
    bool mIsPrebuffering{true};
    uint32_t mExpectedSeq{0};
    float mTargetBufferMs{30.0f};
    std::atomic<bool> mAutoMode{true};

    OpusDecoderWrapper mOpusDecoder;
    uint8_t mCurrentCodec{0};
    int mChannels{2};
    int mSampleRate{48000};

    int mLastFrameSamplesPerCh{240};
    float mPacketDurationMs{5.0f};
    std::vector<int16_t> mLastValidFrame;
    int mConsecutiveLosses{0};

    std::atomic<uint32_t> mUnderrunCount{0};
    std::atomic<uint32_t> mLostCount{0};
    std::atomic<uint32_t> mLateCount{0};
    std::atomic<uint32_t> mRxPacketsInterval{0};
    std::atomic<uint32_t> mCurrentRxPps{0};

    std::chrono::steady_clock::time_point mLastPacketRecvTime;
    std::chrono::steady_clock::time_point mLastPpsTime;
    std::chrono::steady_clock::time_point mLastUnderrunAdaptTime;
    std::chrono::steady_clock::time_point mLastDecayAdaptTime;
};
