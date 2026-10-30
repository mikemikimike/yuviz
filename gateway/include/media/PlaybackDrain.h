#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "media/PlaybackQueue.h"
#include "websocket/IWebSocketConnection.h"

#include <atomic>
#include <cstdint>
#include <thread>

namespace voiceai {

// Drains PlaybackQueue on a dedicated thread and sends PCM to mod_audio_fork.
class PlaybackDrain : private NonCopyable, private NonMovable {
public:
    PlaybackDrain(PlaybackQueue&        queue,
                  IWebSocketConnection& connection,
                  Logger&               logger);
    ~PlaybackDrain();

    void start();
    void stop();

    // Audio bytes handed to the connection so far (end-of-response markers excluded).
    uint64_t sent_bytes() const { return sent_bytes_.load(std::memory_order_acquire); }

private:
    void loop() noexcept;

    PlaybackQueue&         queue_;
    IWebSocketConnection&  connection_;
    Logger&                logger_;
    std::atomic<bool>      running_{false};
    std::atomic<uint64_t>  sent_bytes_{0};
    std::thread            thread_;
};

} // namespace voiceai
