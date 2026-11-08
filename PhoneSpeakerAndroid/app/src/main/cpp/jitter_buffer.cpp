#include "jitter_buffer.h"
#include <algorithm>
#include <cmath>

static inline int32_t seqDiff(uint32_t a, uint32_t b) {
    return static_cast<int32_t>(a - b);
}

JitterBuffer::JitterBuffer() {
    auto now = std::chrono::steady_clock::now();
    mLastPacketRecvTime = now;
    mLastPpsTime = now;
    mLastUnderrunAdaptTime = now;
    mLastDecayAdaptTime = now;
    mOpusDecoder.init(48000, 2);
    mLastValidFrame.resize(480 * 2, 0);
}

JitterBuffer::~JitterBuffer() {
    reset();
}

void JitterBuffer::reset() {
    std::lock_guard<std::mutex> lock(mMutex);
    mPacketMap.clear();
    mPcmFifo.clear();
    mHasStarted = false;
    mIsPrebuffering = true;
    mExpectedSeq = 0;
    mConsecutiveLosses = 0;
    mLastFrameSamplesPerCh = 240;
    mPacketDurationMs = 5.0f;
    std::fill(mLastValidFrame.begin(), mLastValidFrame.end(), 0);
}

void JitterBuffer::pushPacket(const AudioPacket& packet) {
    std::lock_guard<std::mutex> lock(mMutex);

    auto now = std::chrono::steady_clock::now();
    auto idleMs = std::chrono::duration_cast<std::chrono::milliseconds>(now - mLastPacketRecvTime).count();
    mLastPacketRecvTime = now;

    mRxPacketsInterval++;
    auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - mLastPpsTime).count();
    if (elapsed >= 1000) {
        mCurrentRxPps = (mRxPacketsInterval.load() * 1000) / elapsed;
        mRxPacketsInterval = 0;
        mLastPpsTime = now;
    }

    mCurrentCodec = packet.header.codec;
    mChannels = (packet.header.channels == 1) ? 1 : 2;
    if (packet.header.sample_rate_div100 > 0) {
        mSampleRate = packet.header.sample_rate_div100 * 100;
    }

    if (!mHasStarted || idleMs > 500) {
        mPacketMap.clear();
        mPcmFifo.clear();
        mExpectedSeq = packet.header.seq;
        mConsecutiveLosses = 0;
        mIsPrebuffering = true;
        mHasStarted = true;
    } else {
        int32_t diff = seqDiff(packet.header.seq, mExpectedSeq);
        if (diff < -100 || diff > 1000) {
            mPacketMap.clear();
            mPcmFifo.clear();
            mExpectedSeq = packet.header.seq;
            mConsecutiveLosses = 0;
            mIsPrebuffering = false;
        } else if (diff < 0) {
            mLateCount++;
            return;
        }
    }

    mPacketMap[packet.header.seq] = packet;

    while (mPacketMap.size() > 100) {
        mPacketMap.erase(mPacketMap.begin());
        if (!mPacketMap.empty() && seqDiff(mPacketMap.begin()->first, mExpectedSeq) > 0) {
            mExpectedSeq = mPacketMap.begin()->first;
        }
    }
}

void JitterBuffer::adaptTargetBuffer() {
    if (!mAutoMode.load()) return;
    auto now = std::chrono::steady_clock::now();
    auto noUnderrunSec = std::chrono::duration_cast<std::chrono::seconds>(now - mLastUnderrunAdaptTime).count();
    auto decayIntervalSec = std::chrono::duration_cast<std::chrono::seconds>(now - mLastDecayAdaptTime).count();

    if (noUnderrunSec >= 10 && decayIntervalSec >= 10) {
        mTargetBufferMs = std::max(15.0f, mTargetBufferMs - 2.5f);
        mLastDecayAdaptTime = now;
    }
}

int JitterBuffer::readAudio(int16_t* out_samples, int num_frames) {
    std::lock_guard<std::mutex> lock(mMutex);

    size_t needed_samples = static_cast<size_t>(num_frames * mChannels);

    auto now = std::chrono::steady_clock::now();
    auto sinceLastRxMs = std::chrono::duration_cast<std::chrono::milliseconds>(now - mLastPacketRecvTime).count();
    if (sinceLastRxMs > 1500) {
        mCurrentRxPps = 0;
    }

    if (!mHasStarted || sinceLastRxMs > 600) {
        mHasStarted = false;
        mIsPrebuffering = true;
        std::memset(out_samples, 0, needed_samples * sizeof(int16_t));
        return num_frames;
    }

    adaptTargetBuffer();

    float fifoMs = (mPcmFifo.size() > 0 && mSampleRate > 0)
        ? (mPcmFifo.size() * 1000.0f / (mChannels * mSampleRate))
        : 0.0f;
    float currentBufferMs = (mPacketMap.size() * mPacketDurationMs) + fifoMs;

    // Only prebuffer once at the very start of a stream (never pause mid-stream!)
    if (mIsPrebuffering) {
        float startThreshold = std::min(mTargetBufferMs * 0.6f, 20.0f);
        if (currentBufferMs < startThreshold && mPacketMap.size() < 2) {
            std::memset(out_samples, 0, needed_samples * sizeof(int16_t));
            return num_frames;
        }
        mIsPrebuffering = false;
        mConsecutiveLosses = 0;
        if (!mPacketMap.empty()) {
            mExpectedSeq = mPacketMap.begin()->first;
        }
    }

    while (mPcmFifo.size() < needed_samples) {
        if (mPacketMap.empty() && mConsecutiveLosses >= 12) {
            break;
        }
        popNextPacketOrConceal();
    }

    size_t available = std::min(needed_samples, mPcmFifo.size());
    if (available > 0) {
        std::memcpy(out_samples, mPcmFifo.data(), available * sizeof(int16_t));
        mPcmFifo.erase(mPcmFifo.begin(), mPcmFifo.begin() + available);
    }
    if (available < needed_samples) {
        std::memset(reinterpret_cast<uint8_t*>(out_samples) + (available * sizeof(int16_t)), 0,
                    (needed_samples - available) * sizeof(int16_t));
    }

    return num_frames;
}

void JitterBuffer::popNextPacketOrConceal() {
    float fifoMs = (mPcmFifo.size() > 0 && mSampleRate > 0)
        ? (mPcmFifo.size() * 1000.0f / (mChannels * mSampleRate))
        : 0.0f;
    float totalQueuedMs = (mPacketMap.size() * mPacketDurationMs) + fifoMs;

    // Soft anti-drift: only trim if excess exceeds target + 30ms
    if (totalQueuedMs > (mTargetBufferMs + 30.0f) && mPacketMap.size() > 4) {
        auto oldest = mPacketMap.begin();
        mExpectedSeq = oldest->first + 1;
        mPacketMap.erase(oldest);
    }

    // Only snap forward if gap > 4 or after > 2 consecutive losses so single-packet Opus FEC works
    if (!mPacketMap.empty()) {
        uint32_t earliestSeq = mPacketMap.begin()->first;
        int32_t gap = seqDiff(earliestSeq, mExpectedSeq);
        if (gap > 4 || (mConsecutiveLosses > 2 && gap > 0)) {
            mExpectedSeq = earliestSeq;
        }
    }

    auto it = mPacketMap.find(mExpectedSeq);
    if (it != mPacketMap.end()) {
        const auto& pkt = it->second;
        size_t old_size = mPcmFifo.size();
        int total_samples = 0;

        if (pkt.header.codec == CODEC_PCM16) {
            int frame_count = pkt.payload.size() / (mChannels * sizeof(int16_t));
            if (frame_count <= 0) frame_count = 240;
            mLastFrameSamplesPerCh = frame_count;
            mPacketDurationMs = (frame_count * 1000.0f) / mSampleRate;

            total_samples = frame_count * mChannels;
            mPcmFifo.resize(old_size + total_samples);

            int copyBytes = std::min(static_cast<size_t>(total_samples * sizeof(int16_t)), pkt.payload.size());
            std::memcpy(mPcmFifo.data() + old_size, pkt.payload.data(), copyBytes);
        } else {
            int max_frames = 960;
            int16_t decoded_buf[960 * 2];
            int decoded = mOpusDecoder.decode(pkt.payload.data(), pkt.payload.size(), decoded_buf, max_frames, false);
            if (decoded > 0) {
                mLastFrameSamplesPerCh = decoded;
                mPacketDurationMs = (decoded * 1000.0f) / mSampleRate;

                total_samples = decoded * mChannels;
                mPcmFifo.resize(old_size + total_samples);
                std::memcpy(mPcmFifo.data() + old_size, decoded_buf, total_samples * sizeof(int16_t));
            } else {
                total_samples = mLastFrameSamplesPerCh * mChannels;
                mPcmFifo.resize(old_size + total_samples, 0);
            }
        }

        // Smooth 32-sample crossfade if recovering from packet loss/concealment
        if (mConsecutiveLosses > 0 && total_samples > 0 && !mLastValidFrame.empty()) {
            int fade_samples = std::min({32 * mChannels, total_samples, static_cast<int>(mLastValidFrame.size())});
            int16_t* new_ptr = mPcmFifo.data() + old_size;
            for (int i = 0; i < fade_samples; i++) {
                float alpha = static_cast<float>(i) / static_cast<float>(fade_samples);
                new_ptr[i] = static_cast<int16_t>(mLastValidFrame[i] * (1.0f - alpha) + new_ptr[i] * alpha);
            }
        }

        if (total_samples > 0) {
            if (mLastValidFrame.size() < static_cast<size_t>(total_samples)) {
                mLastValidFrame.resize(total_samples);
            }
            std::memcpy(mLastValidFrame.data(), mPcmFifo.data() + old_size, total_samples * sizeof(int16_t));
        }

        mPacketMap.erase(it);
        mExpectedSeq++;
        mConsecutiveLosses = 0;
    } else {
        if (!mPacketMap.empty()) {
            mLostCount++;
        } else {
            mUnderrunCount++;
        }

        auto now = std::chrono::steady_clock::now();
        if (mAutoMode.load()) {
            auto sinceLastBumpMs = std::chrono::duration_cast<std::chrono::milliseconds>(now - mLastUnderrunAdaptTime).count();
            if (sinceLastBumpMs >= 300) {
                mTargetBufferMs = std::min(200.0f, mTargetBufferMs + 5.0f);
                mLastUnderrunAdaptTime = now;
            }
        }

        int conceal_frames = mLastFrameSamplesPerCh;
        if (mCurrentCodec == CODEC_PCM16) {
            concealPcmGap(conceal_frames);
        } else {
            concealOpusGap(conceal_frames, mExpectedSeq);
        }

        mConsecutiveLosses++;
        if (!mPacketMap.empty()) {
            mExpectedSeq++;
        }
    }
}

void JitterBuffer::concealPcmGap(int frames) {
    int total_samples = frames * mChannels;
    size_t old_size = mPcmFifo.size();
    mPcmFifo.resize(old_size + total_samples);
    int16_t* dest = mPcmFifo.data() + old_size;

    if (mConsecutiveLosses >= 6 || mLastValidFrame.empty()) {
        std::memset(dest, 0, total_samples * sizeof(int16_t));
        return;
    }

    float factor = std::pow(0.8f, static_cast<float>(mConsecutiveLosses + 1));
    int copyCount = std::min(static_cast<int>(mLastValidFrame.size()), total_samples);

    for (int i = 0; i < copyCount; i++) {
        dest[i] = static_cast<int16_t>(mLastValidFrame[i] * factor);
    }
    if (copyCount < total_samples) {
        std::memset(dest + copyCount, 0, (total_samples - copyCount) * sizeof(int16_t));
    }
}

void JitterBuffer::concealOpusGap(int frames, uint32_t missing_seq) {
    size_t old_size = mPcmFifo.size();
    mPcmFifo.resize(old_size + frames * mChannels);
    int16_t* dest = mPcmFifo.data() + old_size;

    auto nextIt = mPacketMap.find(missing_seq + 1);
    if (nextIt != mPacketMap.end() && !nextIt->second.payload.empty()) {
        int decoded = mOpusDecoder.decode(nextIt->second.payload.data(),
                                          nextIt->second.payload.size(),
                                          dest, frames, true);
        if (decoded > 0) return;
    }

    int decoded = mOpusDecoder.decode(nullptr, 0, dest, frames, false);
    if (decoded <= 0) {
        std::memset(dest, 0, frames * mChannels * sizeof(int16_t));
    }
}

void JitterBuffer::setTargetBufferMs(float target_ms) {
    std::lock_guard<std::mutex> lock(mMutex);
    mTargetBufferMs = std::clamp(target_ms, 10.0f, 200.0f);
}

float JitterBuffer::getTargetBufferMs() const {
    std::lock_guard<std::mutex> lock(mMutex);
    return mTargetBufferMs;
}

void JitterBuffer::setAutoMode(bool enabled) {
    mAutoMode.store(enabled);
}

bool JitterBuffer::isAutoMode() const {
    return mAutoMode.load();
}

JitterStats JitterBuffer::getStats() {
    std::lock_guard<std::mutex> lock(mMutex);
    JitterStats stats;
    float fifo_ms = (mPcmFifo.size() > 0 && mSampleRate > 0)
        ? (mPcmFifo.size() * 1000.0f / (mChannels * mSampleRate))
        : 0.0f;
    stats.buffer_ms = (mPacketMap.size() * mPacketDurationMs) + fifo_ms;
    stats.target_ms = mTargetBufferMs;
    stats.underruns = mUnderrunCount.load();
    stats.lost_packets = mLostCount.load();
    stats.late_packets = mLateCount.load();
    stats.rx_pps = mCurrentRxPps.load();
    stats.active_codec = mCurrentCodec;
    stats.auto_mode = mAutoMode.load();
    return stats;
}
