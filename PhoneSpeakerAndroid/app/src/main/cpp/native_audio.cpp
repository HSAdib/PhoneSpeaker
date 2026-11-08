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

    if (mReopenThread.joinable()) {
        mReopenThread.join();
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
        ->setDataCallback(this)
        ->setErrorCallback(this);

    if (mLowLatencyMode.load()) {
        builder.setPerformanceMode(oboe::PerformanceMode::LowLatency)
            ->setSharingMode(oboe::SharingMode::Exclusive);
    } else {
        builder.setPerformanceMode(oboe::PerformanceMode::None)
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

    // Configure double burst buffer size for optimal latency/stability balance
    int32_t burst = mAudioStream->getFramesPerBurst();
    mAudioStream->setBufferSizeInFrames(burst * 2);

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
        mAudioStream->stop();
        mAudioStream->close();
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
    }
}

oboe::DataCallbackResult NativeAudioEngine::onAudioReady(oboe::AudioStream* /*stream*/,
                                                         void* audioData,
                                                         int32_t numFrames) {
    int16_t* pcmOut = static_cast<int16_t*>(audioData);
    if (mMuted.load()) {
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

void NativeAudioEngine::onErrorAfterClose(oboe::AudioStream* /*stream*/, oboe::Result result) {
    LOGI("Audio stream error after close: %s. Reopening stream.", oboe::convertToText(result));
    if (mIsRunning.load()) {
        if (mReopenThread.joinable()) {
            mReopenThread.join();
        }
        mReopenThread = std::thread([this]() {
            if (mIsRunning.load()) {
                closeOboeStream();
                openAndStartOboeStream();
            }
        });
    }
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
        if (mReopenThread.joinable()) {
            mReopenThread.join();
        }
        mReopenThread = std::thread([this]() {
            if (mIsRunning.load()) {
                closeOboeStream();
                openAndStartOboeStream();
            }
        });
    }
}

bool NativeAudioEngine::isLowLatencyMode() const {
    return mLowLatencyMode.load();
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

