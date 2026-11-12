#include <cstdio>
#include <exception>
#include <stdexcept>
#include <string>

#include "test_harness.hpp"

namespace cgmusic::test {

std::vector<TestCase>& registry() {
  static std::vector<TestCase> cases;
  return cases;
}

void check_failed(const char* expression, const char* file, int line) {
  throw std::runtime_error(std::string("CHECK failed: ") + expression + " (" + file + ":" +
                           std::to_string(line) + ")");
}

}  // namespace cgmusic::test

int main() {
  int failed = 0;
  for (const auto& test_case : cgmusic::test::registry()) {
    try {
      test_case.fn();
      std::printf("PASS %s\n", test_case.name);
    } catch (const std::exception& error) {
      ++failed;
      std::printf("FAIL %s: %s\n", test_case.name, error.what());
    }
  }
  std::printf("%zu test(s), %d failed\n", cgmusic::test::registry().size(), failed);
  return failed == 0 ? 0 : 1;
}
