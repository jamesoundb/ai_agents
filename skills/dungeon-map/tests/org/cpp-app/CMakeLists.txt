cmake_minimum_required(VERSION 3.24)
project(CppApp CXX)
include(FetchContent)
FetchContent_Declare(
  cppnet
  GIT_REPOSITORY http://{HOST}/{ORG}/cpp-net.git
  GIT_TAG v1.0
)
FetchContent_Declare(googletest URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip)
ExternalProject_Add(corebin URL http://{HOST}/{ORG}/cpp-core/archive/v2.0.tar.gz)
CPMAddPackage("gh:fmtlib/fmt@10.2.1")
find_package(CppCore 2.0 REQUIRED)
find_package(Threads REQUIRED)
