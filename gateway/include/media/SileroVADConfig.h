#pragma once

#include <cstdint>
#include <string>

namespace voiceai {

struct SileroVADConfig {
    std::string model_path{"models/silero_vad.onnx"};
    float    speech_threshold{0.5f};   // window probability >= this → speech
    float    silence_threshold{0.35f}; // window probability <  this → silence
    uint32_t onset_ms{96};             // sustained speech before SpeechStart
    uint32_t hold_ms{600};             // sustained silence before SpeechEnd: the wait the caller feels
                                       // after every turn. Gateway-wide default; set it with
                                       // GATEWAY_VAD_HOLD_MS or media.vad_hold_ms, per tenant with vad_hold_ms.
};

} // namespace voiceai
