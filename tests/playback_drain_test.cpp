#include <gtest/gtest.h>
#include "media/PlaybackDrain.h"
#include "media/PlaybackQueue.h"
#include "logging/Logger.h"

#include <atomic>
#include <chrono>
#include <thread>

namespace {

using namespace voiceai;

class StubMetrics final : public IMetrics {
public:
    bool initialize() override { return true; }
    bool start()      override { return true; }
    void stop()       override {}
    void shutdown()   override {}
    void increment(const std::string&, double = 1.0) override {}
    void gauge(const std::string&, double)           override {}
    void observe(const std::string&, double)         override {}
};

class StubConnection final : public IWebSocketConnection {
public:
    const std::string& id()   const noexcept override { return id_; }
    const std::string& path() const noexcept override { return id_; }
    bool is_open()            const noexcept override { return true; }
    void send_text(const std::string&)           override {}
    void send_binary(const uint8_t*, size_t n)   override { ++sends; wire_bytes += n; }
    void close()                                 override {}
    void set_on_binary(BinaryHandler) override {}
    void set_on_text(TextHandler)     override {}
    void set_on_close(CloseHandler)   override {}

    std::atomic<int>      sends{0};
    std::atomic<uint64_t> wire_bytes{0};
private:
    std::string id_{"conn"};
};

AudioFrame make_frame(size_t bytes) {
    AudioFrame f;
    f.set_session_id("s1");
    f.direction = AudioDirection::Outbound;
    f.payload.assign(bytes, 0);
    return f;
}

TEST(PlaybackDrainTest, SentBytesCountsAudioAndSkipsEndMarkers) {
    StubMetrics   metrics;
    Logger        logger = Logger::make_null();
    PlaybackQueue queue{"s1", 10, metrics, logger};
    StubConnection conn;
    PlaybackDrain drain{queue, conn, logger};

    EXPECT_EQ(drain.sent_bytes(), 0u);
    drain.start();
    queue.push(make_frame(640));
    queue.push(make_frame(320));
    queue.push(make_frame(0));  // end-of-response marker

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds{2};
    while (conn.sends.load() < 2 && std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds{5});
    // The marker is popped after the second audio frame; give it time to be skipped.
    std::this_thread::sleep_for(std::chrono::milliseconds{100});
    drain.stop();

    EXPECT_EQ(conn.sends.load(), 2);
    EXPECT_EQ(drain.sent_bytes(), 960u);
    EXPECT_EQ(drain.sent_bytes(), conn.wire_bytes.load());
}

} // namespace
