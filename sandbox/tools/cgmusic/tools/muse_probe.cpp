// muse_probe:对真实 libMuseSamplerCoreLib 的独立冒烟程序,不属于主 CMake。
// 构建:g++ -std=c++17 -Wall -Wextra -I src tools/muse_probe.cpp src/muse/loader.cpp -ldl -o muse_probe
// 环境变量:MUSESAMPLER_LIB(库路径,缺省 /opt/musesampler/lib/libMuseSamplerCoreLib.so)、
//           MUSE_SAMPLES_DIR(样本根,缺省 /opt/muse-sounds;传给库的 MUSESAMPLER_INSTRUMENT_FOLDER)。
// 退出码:0 = 渲染非静音;1 = 失败或静音。

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <string>
#include <thread>
#include <vector>

#include "common.hpp"
#include "muse/loader.hpp"

namespace {

std::string env_or(const char* name, const char* fallback) {
  const char* value = std::getenv(name);
  return value != nullptr && *value != '\0' ? value : fallback;
}

std::string lower(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return text;
}

bool contains(const std::string& haystack, const char* needle) {
  return lower(haystack).find(needle) != std::string::npos;
}

}  // namespace

int main() {
  using namespace cgmusic::muse;
  try {
    const std::string lib_path = env_or("MUSESAMPLER_LIB", "/opt/musesampler/lib/libMuseSamplerCoreLib.so");
    const std::string samples = env_or("MUSE_SAMPLES_DIR", "/opt/muse-sounds");

    const MuseLib lib = MuseLib::load(lib_path);
    const Api& api = lib.api();
    std::printf("version=%s major=%d minor=%d revision=%d build=%d init=%s\n", lib.version().text.c_str(),
                lib.version().major, lib.version().minor, lib.version().revision, lib.version().build,
                lib.init_kind() == InitKind::Init3 ? "ms_init_3" : "ms_init_2");

    lib.init(samples);
    std::printf("init ok (sample root %s)\n", samples.c_str());

    const std::vector<InstrumentEntry> instruments = lib.list_instruments();
    std::printf("instruments=%zu\n", instruments.size());
    std::map<std::string, int> per_category;
    for (const InstrumentEntry& entry : instruments) ++per_category[entry.category];
    for (const auto& [category, count] : per_category) std::printf("  category %s: %d\n", category.c_str(), count);
    for (const InstrumentEntry& entry : instruments) {
      std::printf("  id=%d name=\"%s\" category=\"%s\" package=\"%s\"\n", entry.id, entry.name.c_str(),
                  entry.category.c_str(), entry.package.c_str());
    }
    if (instruments.empty()) {
      std::fprintf(stderr, "FAIL: 乐器目录为空(样本根未被库识别?)\n");
      return 1;
    }

    const InstrumentEntry* chosen = nullptr;
    for (const InstrumentEntry& entry : instruments) {
      if (contains(entry.category, "woodwind") || contains(entry.category, "string") ||
          contains(entry.package, "woodwind") || contains(entry.package, "string")) {
        chosen = &entry;
        break;
      }
    }
    if (chosen == nullptr) chosen = &instruments.front();
    std::printf("chosen id=%d name=\"%s\"\n", chosen->id, chosen->name.c_str());

    constexpr double kRate = 44100.0;
    constexpr int kBlock = 512;
    constexpr int kChannels = 2;
    constexpr double kSeconds = 3.0;

    abi::ms_MuseSampler sampler = api.ms_MuseSampler_create();
    if (sampler == nullptr) throw cgmusic::Error("muse_unavailable", "ms_MuseSampler_create 返回空");
    if (api.ms_MuseSampler_init_2(sampler, kRate, kBlock, kChannels) != abi::kResultOk) {
      throw cgmusic::Error("muse_unavailable", "ms_MuseSampler_init_2 失败");
    }
    abi::ms_Track track = api.ms_MuseSampler_add_track(sampler, chosen->id);
    if (track == nullptr) throw cgmusic::Error("muse_unavailable", "ms_MuseSampler_add_track 返回空");

    const abi::ms_DynamicsEvent_2 dynamics{0, 64.0 / 127.0};  // mf
    std::printf("add_dynamics -> %d\n", api.ms_MuseSampler_add_track_dynamics_event_2(sampler, track, dynamics));
    const int pitches[] = {60, 64, 67, 72};
    for (int i = 0; i < 4; ++i) {
      abi::ms_NoteEvent_5 note{};
      note._voice = 0;
      note._location_us = static_cast<long long>(i) * 500000;
      note._duration_us = 450000;
      note._pitch = pitches[i];
      note._tempo = 120.0;
      long long event_id = -1;
      const int result = api.ms_MuseSampler_add_track_note_event_6(sampler, track, note, &event_id);
      std::printf("add_note pitch=%d -> %d event_id=%lld\n", pitches[i], result, event_id);
    }
    std::printf("finalize_track -> %d\n", api.ms_MuseSampler_finalize_track(sampler, track));

    api.ms_MuseSampler_trigger_render(sampler);
    const auto start = std::chrono::steady_clock::now();
    while (!api.ms_MuseSampler_ready_to_play(sampler)) {
      if (std::chrono::steady_clock::now() - start > std::chrono::seconds(120)) {
        std::fprintf(stderr, "FAIL: ready_to_play 120 s 内未就绪\n");
        return 1;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    std::printf("ready_to_play after %.2fs\n",
                std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count());

    if (api.ms_MuseSampler_start_offline_mode(sampler, kRate) != abi::kResultOk) {
      throw cgmusic::Error("muse_unavailable", "start_offline_mode 失败");
    }
    const std::size_t total = static_cast<std::size_t>(kSeconds * kRate);
    std::vector<std::vector<float>> out(kChannels, std::vector<float>(total, 0.0F));
    std::vector<std::vector<float>> block(kChannels, std::vector<float>(kBlock, 0.0F));
    float* pointers[kChannels];
    for (int c = 0; c < kChannels; ++c) pointers[c] = block[static_cast<std::size_t>(c)].data();
    int failures = 0;
    for (std::size_t done = 0; done < total; done += kBlock) {
      for (auto& channel : block) std::fill(channel.begin(), channel.end(), 0.0F);
      abi::ms_OutputBuffer buffer{pointers, kBlock, kChannels};
      if (api.ms_MuseSampler_process_offline(sampler, buffer) != abi::kResultOk) ++failures;
      const std::size_t n = std::min<std::size_t>(kBlock, total - done);
      for (int c = 0; c < kChannels; ++c) {
        std::copy_n(block[static_cast<std::size_t>(c)].begin(), n, out[static_cast<std::size_t>(c)].begin() + static_cast<long>(done));
      }
    }
    api.ms_MuseSampler_stop_offline_mode(sampler);
    api.ms_MuseSampler_destroy(sampler);

    double peak = 0.0;
    double sum_sq = 0.0;
    for (const auto& channel : out) {
      for (const float sample : channel) {
        peak = std::max(peak, static_cast<double>(std::fabs(sample)));
        sum_sq += static_cast<double>(sample) * sample;
      }
    }
    const double rms = std::sqrt(sum_sq / static_cast<double>(total * kChannels));
    std::printf("render frames=%zu process_offline_failures=%d peak=%.6f rms=%.6f\n", total, failures, peak, rms);
    if (peak > 1e-4 && failures == 0) {
      std::printf("PASS: 非静音\n");
      return 0;
    }
    std::printf("FAIL: 静音或 process_offline 报错\n");
    return 1;
  } catch (const cgmusic::Error& error) {
    std::fprintf(stderr, "FAIL [%s]: %s\n", error.code().c_str(), error.what());
    return 1;
  }
}
