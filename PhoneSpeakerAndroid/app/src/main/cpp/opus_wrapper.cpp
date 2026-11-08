#include "opus_wrapper.h"
#include <android/log.h>

#define TAG "OpusDecoderWrapper"
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

OpusDecoderWrapper::OpusDecoderWrapper() {}

OpusDecoderWrapper::~OpusDecoderWrapper() {
    reset();
}

void OpusDecoderWrapper::reset() {
    if (mDecoder) {
        opus_decoder_destroy(mDecoder);
        mDecoder = nullptr;
    }
}

bool OpusDecoderWrapper::init(int sample_rate, int channels) {
    reset();
    mSampleRate = sample_rate;
    mChannels = channels;

    int error = OPUS_OK;
    mDecoder = opus_decoder_create(sample_rate, channels, &error);
    if (error != OPUS_OK || !mDecoder) {
        LOGE("Failed to create Opus decoder: %s", opus_strerror(error));
        return false;
    }
    return true;
}

int OpusDecoderWrapper::decode(const uint8_t* data, int len, int16_t* out_pcm, int frame_size, bool fec) {
    if (!mDecoder) return -1;

    // decode_fec parameter: 1 for FEC, 0 for normal/PLC
    int result = opus_decode(mDecoder, data, len, out_pcm, frame_size, fec ? 1 : 0);
    if (result < 0) {
        LOGE("opus_decode error: %s (len=%d, fec=%d)", opus_strerror(result), len, (int)fec);
    }
    return result;
}

