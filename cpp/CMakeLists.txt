cmake_minimum_required(VERSION 3.21)

project(TS2Python
    VERSION 0.1.0
    DESCRIPTION "TradeStation EL -> ZeroMQ PUB bridge DLL"
    LANGUAGES CXX
)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Refuse to build anything other than x86 — TradeStation is a 32-bit host.
if(WIN32 AND NOT CMAKE_SIZEOF_VOID_P EQUAL 4)
    message(FATAL_ERROR
        "TS2Python must be built as Win32 (x86). Use CMake preset x86-release "
        "or configure with -A Win32 / --toolchain <vcpkg>/scripts/buildsystems/vcpkg.cmake "
        "and VCPKG_TARGET_TRIPLET=x86-windows.")
endif()

# C++20 <chrono> zoned_time + IANA tzdb is needed for ET → UTC conversion in
# parse_el_timestamp_to_utc(). On Windows the tzdata comes from the system
# ICU (shipped in Windows 10 1903+). MSVC support landed in VS 2019 16.10
# (_MSC_VER 1929); enforce that here so an older toolset fails fast at
# configure time instead of with a cryptic <chrono> error mid-build.
if(MSVC AND MSVC_VERSION LESS 1929)
    message(FATAL_ERROR
        "MSVC ${MSVC_VERSION} is too old for std::chrono::zoned_time. "
        "Need MSVC 1929 (VS 2019 16.10) or newer; VS 2022 17.x recommended.")
endif()

find_package(cppzmq CONFIG REQUIRED)

# -----------------------------------------------------------------------------
# TS2Python.dll
# -----------------------------------------------------------------------------
add_library(TS2Python SHARED
    src/ts2python.cpp
    # .def pins undecorated export names (EL_Init, EL_PublishTick, ...) so
    # EasyLanguage's DefineDLLFunc can resolve them on x86 + __stdcall.
    # MSVC auto-picks this up as /DEF:; ignored by non-MSVC toolchains.
    src/TS2Python.def
)

target_include_directories(TS2Python
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)

target_link_libraries(TS2Python
    PRIVATE
        cppzmq
)

set_target_properties(TS2Python PROPERTIES
    OUTPUT_NAME "TS2Python"
    PREFIX ""
    WINDOWS_EXPORT_ALL_SYMBOLS OFF
    # ts2python.h switches on TS2PYTHON_EXPORTS to pick dllexport over
    # dllimport. CMake would otherwise define TS2Python_EXPORTS (target
    # name, different case), leaving the header on the dllimport branch
    # and every definition in ts2python.cpp failing with C2491. The
    # MSBuild project sets this in PreprocessorDefinitions; DEFINE_SYMBOL
    # is the CMake equivalent.
    DEFINE_SYMBOL "TS2PYTHON_EXPORTS"
)

if(MSVC)
    # /utf-8       — treat all source (and execution strings) as UTF-8 so
    #                cp950 / cp936 hosts don't mis-decode non-ASCII comments
    #                (silences C4819 project-wide).
    # /Zc:__cplusplus so __cplusplus reports the real C++20 value.
    target_compile_options(TS2Python PRIVATE /utf-8 /W4 /Zc:__cplusplus /permissive-)
    target_compile_definitions(TS2Python PRIVATE
        WIN32_LEAN_AND_MEAN
        NOMINMAX
        _CRT_SECURE_NO_WARNINGS
    )
endif()

# -----------------------------------------------------------------------------
# Standalone test harness (links TS2Python statically via the exported C API)
# -----------------------------------------------------------------------------
add_executable(TS2Python_TestHarness
    src/test_harness.cpp
)
target_link_libraries(TS2Python_TestHarness PRIVATE TS2Python)
if(MSVC)
    target_compile_options(TS2Python_TestHarness PRIVATE /utf-8 /W4 /Zc:__cplusplus /permissive-)
endif()
set_target_properties(TS2Python_TestHarness PROPERTIES
    OUTPUT_NAME "TS2Python_TestHarness"
)

# -----------------------------------------------------------------------------
# Install: stage the DLL + deps into bin/ so deploy script has a single root
# -----------------------------------------------------------------------------
install(TARGETS TS2Python TS2Python_TestHarness
    RUNTIME DESTINATION bin
    LIBRARY DESTINATION bin
    ARCHIVE DESTINATION lib
)
install(FILES include/ts2python.h DESTINATION include)
