// CallSession-level EndCall handling against a real control thread, MediaSession and
// PlaybackDrain, with a callback-keeping transport and a counting timer service.

#include <gtest/gtest.h>

#include "common/FakeClock.h"
#include "config/Config.h"
#include "dispatcher/IDispatcher.h"
#include "logging/Logger.h"
#include "media/AudioWorkerPool.h"
#include "media/IVAD.h"
#include "media/MediaSession.h"
#include "metrics/IMetrics.h"
#include "session/CallSession.h"
#include "telephony/EslClient.h"
#include "telephony/TransferCorrelator.h"
#include "timer/ITimerService.h"
#include "transport/IConversationTransport.h"
#include "websocket/IWebSocketConnection.h"

#include <algorithm>
#include <chrono>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

using namespace voiceai;
using namespace std::chrono_literals;

constexpr uint32_t kSampleRate = 16'000;
constexpr uint32_t kFrameMs    = 20;
constexpr size_t   kFrameBytes = kSampleRate * kFrameMs / 1000 * 2;  // PCM S16LE

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

class InlineDispatcher final : public IDispatcher {
public:
    bool initialize() override { return true; }
    bool start()      override { return true; }
    void stop()       override {}
    void shutdown()   override {}
    void dispatch(SessionEvent) override {}
    SubscriberId subscribe(Subscriber) override { return 0; }
    void unsubscribe(SubscriberId)     override {}
};

// Never fires; records what was armed.
class CountingTimerService final : public ITimerService {
public:
    TimerId schedule(TimerType type, const std::string&, std::chrono::milliseconds,
                     TimerCallback) override {
        std::lock_guard lock{mutex_};
        armed_.push_back(type);
        return ++next_id_;
    }
    void cancel(TimerId) override {}
    bool start() override { return true; }
    void stop()  override {}

    size_t armed(TimerType type) const {
        std::lock_guard lock{mutex_};
        return static_cast<size_t>(std::count(armed_.begin(), armed_.end(), type));
    }

private:
    mutable std::mutex     mutex_;
    std::vector<TimerType> armed_;
    TimerId                next_id_{0};
};

class FakeConnection final : public IWebSocketConnection {
public:
    const std::string& id()   const noexcept override { return id_; }
    const std::string& path() const noexcept override { return id_; }
    bool is_open() const noexcept override { return false; }
    void send_text(const std::string&)          override {}
    void send_binary(const uint8_t*, size_t)    override {}
    void close()                                override {}
    void set_on_binary(BinaryHandler)           override {}
    void set_on_text(TextHandler)               override {}
    void set_on_close(CloseHandler)             override {}

private:
    std::string id_{"conn"};
};

// Reports one SpeechStart, then nothing.
class OneShotVad final : public IVAD {
public:
    VADEvent process(const int16_t*, size_t) noexcept override {
        return fired_++ == 0 ? VADEvent::SpeechStart : VADEvent::None;
    }
    float    last_energy_db()    const noexcept override { return -30.0f; }
    uint32_t speech_duration_ms() const noexcept override { return 0; }
    void     reset() noexcept override {}

private:
    int fired_{0};
};

// Keeps the callbacks CallSession registers so the test can play the Conversation Service.
class KeepCallbacksTransport final : public IConversationTransport {
public:
    explicit KeepCallbacksTransport(std::shared_ptr<ConversationTransportCallbacks> out)
        : out_(std::move(out)) {}

    bool start() override { return true; }
    void stop()  override {}
    void open_session(const SessionContext&) override {}
    void send_audio(AudioFrame) override {}
    void cancel_generation(const std::string&) override {}
    void send_playback_finished(const std::string&, bool) override {}
    void send_speech_ended(const std::string&, uint32_t, float) override {}
    void close_session(const std::string&) override {}
    void send_transfer_initiated(const std::string&, const std::string&, const std::string&,
                                 const std::string&, const std::string&) override {}
    void send_transfer_completed(const std::string&, const std::string&,
                                 const std::string&) override {}
    void send_transfer_failed(const std::string&, const std::string&, const std::string&,
                              const std::string&) override {}
    void send_dtmf(const std::string&, const std::string&) override {}
    void set_callbacks(ConversationTransportCallbacks cbs) override { *out_ = std::move(cbs); }

private:
    std::shared_ptr<ConversationTransportCallbacks> out_;
};

bool wait_for(const std::function<bool()>& pred) {
    const auto deadline = std::chrono::steady_clock::now() + 2s;
    while (!pred()) {
        if (std::chrono::steady_clock::now() > deadline) return false;
        std::this_thread::sleep_for(5ms);
    }
    return true;
}

// The control queue is private; nothing observable marks "drained", so negative
// assertions wait this long for a wrongly-queued action to show up.
void settle() { std::this_thread::sleep_for(150ms); }

AudioFrame empty_final_frame() {
    AudioFrame f;
    f.direction = AudioDirection::Outbound;
    f.is_final  = true;
    return f;
}

class CallSessionEndCallTest : public ::testing::Test {
protected:
    // Drives a session to Thinking, as after a caller turn reaches STT.
    void SetUp() override {
        SessionContext ctx;
        ctx.obs.session_id = "11111111-1111-1111-1111-111111111111";
        ctx.obs.tenant_id  = "tenant";
        ctx.tenant         = std::make_shared<TenantConfig>();

        auto media = std::make_unique<MediaSession>(
            ctx.obs.session_id, kSampleRate, 1, 1000, kFrameMs,
            std::make_unique<OneShotVad>(), 64, metrics_, clock_, logger_);

        session_ = std::make_unique<CallSession>(
            std::move(ctx), std::make_shared<FakeConnection>(), std::move(media), pool_,
            std::make_unique<KeepCallbacksTransport>(cbs_), timers_, dispatcher_, metrics_,
            clock_, logger_, esl_, transfer_correlator_, job_correlator_);

        cbs_->on_service_ready(session_->session_id());
        ASSERT_TRUE(wait_for([&] { return session_->fsm_state() == CallFsmState::Listening; }));

        // The VAD fires SpeechStart on the first inbound frame.
        const std::vector<uint8_t> pcm(kFrameBytes);
        session_->push_inbound_audio(pcm.data(), pcm.size());
        session_->media_session().drain_available();
        ASSERT_TRUE(wait_for([&] { return session_->fsm_state() == CallFsmState::Recognizing; }));

        cbs_->on_stt_result("hello", 0.9f);
        ASSERT_TRUE(wait_for([&] { return session_->fsm_state() == CallFsmState::Thinking; }));
    }

    // The empty final chunk is the terminal marker the servicer always sends.
    void send_terminal_chunk() { cbs_->on_tts_chunk(empty_final_frame()); }

    void wait_playback_drained() {
        ASSERT_TRUE(wait_for([&] { return session_->media_session().playback_queue().is_empty(); }));
        settle();
    }

    FakeClock                 clock_;
    StubMetrics               metrics_;
    Logger                    logger_ = Logger::make_null();
    AudioWorkerPool           pool_{1, kFrameMs, logger_};
    CountingTimerService      timers_;
    InlineDispatcher          dispatcher_;
    EslClient                 esl_{EslConfig{}, logger_};
    TransferCorrelator        transfer_correlator_;
    TransferCorrelator        job_correlator_;
    std::shared_ptr<ConversationTransportCallbacks> cbs_ =
        std::make_shared<ConversationTransportCallbacks>();
    // Last member: destroyed first, while everything it references is alive.
    std::unique_ptr<CallSession> session_;
};

TEST_F(CallSessionEndCallTest, AgentEndedCall_StaysPendingUntilPlaybackFinishes) {
    cbs_->on_tts_started();
    cbs_->on_end_call(session_->session_id(), "agent_ended_call", 1500);

    ASSERT_TRUE(wait_for([&] { return session_->end_call_pending(); }));
    EXPECT_EQ(session_->fsm_state(), CallFsmState::Synthesizing);
    EXPECT_EQ(timers_.armed(TimerType::GoodbyeTimeout), 0u);

    send_terminal_chunk();
    ASSERT_TRUE(wait_for([&] { return session_->fsm_state() == CallFsmState::WaitingForHangup; }));
    wait_playback_drained();

    EXPECT_FALSE(session_->end_call_pending());
    EXPECT_EQ(timers_.armed(TimerType::GoodbyeTimeout), 1u);
}

// The servicer sends EndCall after the terminal chunk; run both orders so neither
// the wire order nor the reverse can leave the flag set.
class MaxDurationNoAudioOrderTest : public CallSessionEndCallTest,
                                    public ::testing::WithParamInterface<bool> {};

TEST_P(MaxDurationNoAudioOrderTest, EndsCallAtOnceAndLeavesNothingPending) {
    const bool end_call_first = GetParam();

    cbs_->on_tts_started();
    if (end_call_first) cbs_->on_end_call(session_->session_id(), "max_duration_no_audio", 1500);
    send_terminal_chunk();
    if (!end_call_first) cbs_->on_end_call(session_->session_id(), "max_duration_no_audio", 1500);

    ASSERT_TRUE(wait_for([&] { return session_->fsm_state() == CallFsmState::WaitingForHangup; }));
    wait_playback_drained();

    EXPECT_FALSE(session_->end_call_pending());
    EXPECT_EQ(timers_.armed(TimerType::GoodbyeTimeout), 1u);

    // A later playback-finished must not hang up again.
    session_->media_session().push_outbound(empty_final_frame());
    wait_playback_drained();

    EXPECT_FALSE(session_->end_call_pending());
    EXPECT_EQ(session_->fsm_state(), CallFsmState::WaitingForHangup);
    EXPECT_EQ(timers_.armed(TimerType::GoodbyeTimeout), 1u);
}

INSTANTIATE_TEST_SUITE_P(Orders, MaxDurationNoAudioOrderTest, ::testing::Bool(),
    [](const ::testing::TestParamInfo<bool>& info) {
        return info.param ? "EndCallBeforeChunk" : "EndCallAfterChunk";
    });

} // namespace
