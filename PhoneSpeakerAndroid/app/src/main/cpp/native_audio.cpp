#include "native_audio.h"
#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <android/log.h>

#define TAG "NativeAudioEngine"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

NativeAudioEngine::NativeAudioEngine() {}

NativeAudioEngine::~NativeAudioEngine() {
    stop();
}

bool NativeAudioEngine::start(int audio_port) {
    if (mIsRunning.load()) return true;

    mAudioPort = audio_port;
    mIsRunning.store(true);
    mJitterBuffer.reset();

    // 1. Open UDP socket
    int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        LOGE("Failed to create UDP socket");
        mIsRunning.store(false);
        return false;
    }

    // Set socket receive buffer size
    int rcvbuf = 1024 * 1024; // 1 MB
    setsockopt(sockfd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

    // Enable SO_REUSEADDR
    int reuse = 1;
    setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    struct sockaddr_in servaddr{};
    servaddr.sin_family = AF_INET;
    servaddr.sin_addr.s_addr = INADDR_ANY;
    servaddr.sin_port = htons(mAudioPort);

    if (bind(sockfd, (const struct sockaddr*)&servaddr, sizeof(servaddr)) < 0) {
        LOGE("Failed to bind UDP socket to port %d", mAudioPort);
        close(sockfd);
        mIsRunning.store(false);
        return false;
    }

    mSocketFd.store(sockfd);

    // 2. Start receiver thread
    mReceiverThread = std::thread(&NativeAudioEngine::socketReceiverLoop, this);

    // 3. Start Oboe audio output stream
    if (!openAndStartOboeStream()) {
        LOGE("Failed to start Oboe stream");
        stop();
        return false;
    }

    LOGI("NativeAudioEngine started successfully on port %d", mAudioPort);
    return true;
}

void NativeAudioEngine::stop() {
    if (!mIsRunning.exchange(false)) return;

    int fd = mSocketFd.exchange(-1);
    if (fd >= 0) {
        shutdown(fd, SHUT_RDWR);
        close(fd);
    }

    if (mReceiverThread.joinable()) {
        mReceiverThread.join();
    }

    {
        std::lock_guard<std::mutex> lock(mReopenMutex);
        if (mReopenThread.joinable()) {
            mReopenThread.join();
        }
    }

    closeOboeStream();
    mJitterBuffer.reset();
    LOGI("NativeAudioEngine stopped");
}

bool NativeAudioEngine::openAndStartOboeStream() {
    std::lock_guard<std::mutex> lock(mStreamMutex);
    oboe::AudioStreamBuilder builder;
    builder.setFormat(oboe::AudioFormat::I16)
        ->setChannelCount(oboe::ChannelCount::Stereo)
        ->setSampleRate(48000)
        ->setUsage(oboe::Usage::Media)
        ->setContentType(oboe::ContentType::Music)
        ->setDataCallback(this)
        ->setErrorCallback(this);

    if (mLowLatencyMode.load()) {
        builder.setAudioApi(oboe::AudioApi::AAudio)
            ->setPerformanceMode(oboe::PerformanceMode::LowLatency)
            ->setSharingMode(oboe::SharingMode::Exclusive);
    } else {
        builder.setAudioApi(oboe::AudioApi::OpenSLES)
            ->setPerformanceMode(oboe::PerformanceMode::None)
            ->setSharingMode(oboe::SharingMode::Shared)
            ->setSessionId(oboe::SessionId::Allocate);
    }

    oboe::Result result = builder.openStream(mAudioStream);
    if (result != oboe::Result::OK) {
        LOGE("Failed to open preferred stream: %s, falling back to Shared", oboe::convertToText(result));
        builder.setSharingMode(oboe::SharingMode::Shared);
        result = builder.openStream(mAudioStream);
        if (result != oboe::Result::OK) {
            LOGE("Failed to open Shared stream: %s", oboe::convertToText(result));
            return false;
        }
    }

    // Configure burst buffer size for optimal latency/stability balance
    int32_t burst = std::max(192, mAudioStream->getFramesPerBurst());
    int32_t mult = mLowLatencyMode.load() ? 3 : 4;
    mAudioStream->setBufferSizeInFrames(burst * mult);

    result = mAudioStream->requestStart();
    if (result != oboe::Result::OK) {
        LOGE("Failed to start stream: %s", oboe::convertToText(result));
        mAudioStream->close();
        mAudioStream.reset();
        return false;
    }

    return true;
}

void NativeAudioEngine::closeOboeStream() {
    std::lock_guard<std::mutex> lock(mStreamMutex);
    if (mAudioStream) {
        oboe::StreamState state = mAudioStream->getState();
        if (state != oboe::StreamState::Closed && state != oboe::StreamState::Disconnected && state != oboe::StreamState::Stopping) {
            try {
                mAudioStream->stop();
            } catch (...) {}
        }
        try {
            mAudioStream->close();
        } catch (...) {}
        mAudioStream.reset();
    }
}

void NativeAudioEngine::socketReceiverLoop() {
    uint8_t buffer[2048];
    struct sockaddr_in clientaddr{};
    socklen_t addrlen = sizeof(clientaddr);

    while (mIsRunning.load()) {
        int fd = mSocketFd.load();
        if (fd < 0) break;

        ssize_t n = recvfrom(fd, buffer, sizeof(buffer), 0, (struct sockaddr*)&clientaddr, &addrlen);
        if (n < 0) {
            if (!mIsRunning.load()) break;
            continue;
        }

        // Cache the sender IP address for the Port 5005 stats back-channel
        char ipStr[INET_ADDRSTRLEN] = {0};
        if (inet_ntop(AF_INET, &clientaddr.sin_addr, ipStr, sizeof(ipStr))) {
            std::lock_guard<std::mutex> lock(mSenderIpMutex);
            mLastSenderIp = ipStr;
        }

        // Validate 12-byte header
        if (n < static_cast<ssize_t>(sizeof(AudioHeader))) {
            continue;
        }

        AudioHeader header;
        std::memcpy(&header, buffer, sizeof(AudioHeader));
        uint8_t hopCount = (header.codec >> 4) & 0x07;
        header.codec &= 0x0F;

        // Validation rules:
        if (header.codec != CODEC_PCM16 && header.codec != CODEC_OPUS) {
            LOGE("Dropping packet with unknown codec: %d", header.codec);
            continue;
        }

        if (header.channels != 1 && header.channels != 2) {
            LOGE("Dropping packet with invalid channel count: %d", header.channels);
            continue;
        }

        if (header.sample_rate_div100 != 480) {
            LOGE("Dropping packet with unexpected sample_rate_div100: %d", header.sample_rate_div100);
            continue;
        }

        size_t payload_len = n - sizeof(AudioHeader);

        // Strict PCM validation
        if (header.codec == CODEC_PCM16) {
            size_t sample_size = header.channels * sizeof(int16_t);
            if (payload_len == 0 || (payload_len % 2 != 0) || (payload_len % sample_size != 0)) {
                LOGE("Dropping malformed PCM packet: payload_len=%zu for %d channels",
                     payload_len, header.channels);
                continue;
            }
        }

        AudioPacket packet;
        packet.header = header;
        packet.recv_time_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();

        if (payload_len > 0) {
            packet.payload.assign(buffer + sizeof(AudioHeader), buffer + n);
        }

        mJitterBuffer.pushPacket(packet);

        // Forward to downstream peers if Hosting or Extending Party (up to 4 hops)
        if (mPartyRelayEnabled.load() && hopCount < 4 && fd >= 0) {
            uint8_t origByte8 = buffer[8];
            buffer[8] = (header.codec & 0x0F) | (((hopCount + 1) & 0x07) << 4);
            std::lock_guard<std::mutex> lock(mPartyPeersMutex);
            for (const auto& peerAddr : mPartyPeerAddrs) {
                if (peerAddr.sin_addr.s_addr != clientaddr.sin_addr.s_addr) {
                    sendto(fd, buffer, n, MSG_DONTWAIT, (const struct sockaddr*)&peerAddr, sizeof(peerAddr));
                }
            }
            buffer[8] = origByte8;
        }
    }
}

oboe::DataCallbackResult NativeAudioEngine::onAudioReady(oboe::AudioStream* /*stream*/,
                                                         void* audioData,
                                                         int32_t numFrames) {
    if (!audioData || numFrames <= 0) {
        return oboe::DataCallbackResult::Continue;
    }
    int16_t* pcmOut = static_cast<int16_t*>(audioData);
    if (mMuted.load() || mVolumeGain.load() <= 0.0001f) {
        std::memset(pcmOut, 0, numFrames * 2 * sizeof(int16_t));
        return oboe::DataCallbackResult::Continue;
    }
    mJitterBuffer.readAudio(pcmOut, numFrames);
    float gain = mVolumeGain.load();
    if (gain != 1.0f) {
        int total = numFrames * 2;
        for (int i = 0; i < total; i++) {
            int32_t val = static_cast<int32_t>(pcmOut[i] * gain);
            if (val > 32767) val = 32767;
            else if (val < -32768) val = -32768;
            pcmOut[i] = static_cast<int16_t>(val);
        }
    }
    return oboe::DataCallbackResult::Continue;
}

void NativeAudioEngine::scheduleStreamReopen() {
    if (!mIsRunning.load()) return;
    bool expected = false;
    if (!mReopenPending.compare_exchange_strong(expected, true)) {
        return; // Reopen already in progress
    }
    std::lock_guard<std::mutex> lock(mReopenMutex);
    if (mReopenThread.joinable()) {
        if (mReopenThread.get_id() == std::this_thread::get_id()) {
            mReopenThread.detach();
        } else {
            mReopenThread.join();
        }
    }
    mReopenThread = std::thread([this]() {
        if (mIsRunning.load()) {
            closeOboeStream();
            openAndStartOboeStream();
        }
        mReopenPending.store(false);
    });
}

void NativeAudioEngine::onErrorAfterClose(oboe::AudioStream* /*stream*/, oboe::Result result) {
    LOGI("Audio stream error after close: %s. Reopening stream.", oboe::convertToText(result));
    scheduleStreamReopen();
}

void NativeAudioEngine::setTargetBufferMs(float target_ms) {
    mJitterBuffer.setTargetBufferMs(target_ms);
}

float NativeAudioEngine::getTargetBufferMs() {
    return mJitterBuffer.getTargetBufferMs();
}

void NativeAudioEngine::setAutoMode(bool enabled) {
    mJitterBuffer.setAutoMode(enabled);
}

bool NativeAudioEngine::isAutoMode() {
    return mJitterBuffer.isAutoMode();
}

void NativeAudioEngine::setMuted(bool muted) {
    mMuted.store(muted);
}

bool NativeAudioEngine::isMuted() const {
    return mMuted.load();
}

void NativeAudioEngine::setVolumeGain(float gain) {
    mVolumeGain.store(std::clamp(gain, 0.0f, 2.0f));
}

float NativeAudioEngine::getVolumeGain() const {
    return mVolumeGain.load();
}

void NativeAudioEngine::setLowLatencyMode(bool lowLatency) {
    bool prev = mLowLatencyMode.exchange(lowLatency);
    if (prev != lowLatency && mIsRunning.load()) {
        scheduleStreamReopen();
    }
}

bool NativeAudioEngine::isLowLatencyMode() const {
    return mLowLatencyMode.load();
}

void NativeAudioEngine::setPartyRelayEnabled(bool enabled) {
    mPartyRelayEnabled.store(enabled);
}

bool NativeAudioEngine::isPartyRelayEnabled() const {
    return mPartyRelayEnabled.load();
}

void NativeAudioEngine::setPartyPeers(const std::vector<std::string>& peers) {
    std::lock_guard<std::mutex> lock(mPartyPeersMutex);
    mPartyPeerAddrs.clear();
    for (const auto& ip : peers) {
        struct sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(mAudioPort);
        if (inet_pton(AF_INET, ip.c_str(), &addr.sin_addr) == 1) {
            mPartyPeerAddrs.push_back(addr);
        }
    }
}

int32_t NativeAudioEngine::getAudioSessionId() const {
    std::lock_guard<std::mutex> lock(const_cast<std::mutex&>(mStreamMutex));
    if (mAudioStream) {
        return mAudioStream->getSessionId();
    }
    return 0;
}

JitterStats NativeAudioEngine::getStats() {
    return mJitterBuffer.getStats();
}

std::string NativeAudioEngine::getSenderIp() {
    std::lock_guard<std::mutex> lock(mSenderIpMutex);
    return mLastSenderIp;
}

