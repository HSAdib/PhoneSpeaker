#pragma once

#include <cstdint>
#include <vector>

#pragma pack(push, 1)
struct AudioHeader {
    uint32_t seq;
    uint32_t timestamp_ms;
    uint8_t codec;
    uint8_t channels;
    uint16_t sample_rate_div100;
};
#pragma pack(pop)

static_assert(sizeof(AudioHeader) == 12, "AudioHeader must be exactly 12 bytes");

enum AudioCodec : uint8_t {
    CODEC_PCM16 = 0,
    CODEC_OPUS = 1
};

struct AudioPacket {
    AudioHeader header;
    std::vector<uint8_t> payload;
    int64_t recv_time_ns;
};

