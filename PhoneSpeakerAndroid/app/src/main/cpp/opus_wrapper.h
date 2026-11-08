#pragma once

#include <cstdint>
#include <vector>
#include <opus.h>

class OpusDecoderWrapper {
public:
    OpusDecoderWrapper();
    ~OpusDecoderWrapper();

    bool init(int sample_rate = 48000, int channels = 2);
    void reset();

    // Decode opus payload into out_pcm (int16_t interleaved).
    // If payload is empty or data is nullptr, triggers PLC or FEC.
    // If fec is true, decodes FEC from next packet's payload.
    int decode(const uint8_t* data, int len, int16_t* out_pcm, int frame_size, bool fec = false);

private:
    OpusDecoder* mDecoder{nullptr};
    int mSampleRate{48000};
    int mChannels{2};
};

