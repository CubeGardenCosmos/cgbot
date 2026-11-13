#pragma once

#include <vector>

// 最小测试框架:CGMUSIC_TEST(name) 注册用例,CGMUSIC_CHECK(expr) 断言。
// 测试文件命名为 tests/test_*.cpp,由 CMake 自动收集。

namespace cgmusic::test {

using TestFn = void (*)();

struct TestCase {
  const char* name;
  TestFn fn;
};

std::vector<TestCase>& registry();
[[noreturn]] void check_failed(const char* expression, const char* file, int line);

struct Registrar {
  Registrar(const char* name, TestFn fn) { registry().push_back({name, fn}); }
};

}  // namespace cgmusic::test

#define CGMUSIC_CHECK(expression)                                                      \
  do {                                                                                 \
    if (!(expression)) ::cgmusic::test::check_failed(#expression, __FILE__, __LINE__); \
  } while (false)

#define CGMUSIC_TEST(name)                                                    \
  static void name();                                                         \
  static const ::cgmusic::test::Registrar name##_registrar(#name, &name);     \
  static void name()
