// Offline verifier: maps a game PE image read-only and times wmm::detail::resolve_rsa_thunk().
#include "wmm_pak_enabler.hpp"

#include <cstdio>
#include <fstream>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "Usage: test_enabler <path-to-game-shipping.exe>\n");
        return 2;
    }
    std::ifstream f(argv[1], std::ios::binary | std::ios::ate);
    if (!f) {
        std::fprintf(stderr, "Cannot open %s\n", argv[1]);
        return 2;
    }
    size_t sz = static_cast<size_t>(f.tellg());
    f.seekg(0);
    std::vector<uint8_t> raw(sz);
    f.read(reinterpret_cast<char*>(raw.data()), sz);

    auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(raw.data());
    auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(raw.data() + dos->e_lfanew);
    auto* sec = IMAGE_FIRST_SECTION(nt);
    uint32_t text_rva = 0, text_raw = 0, text_size = 0;
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++sec) {
        if (std::memcmp(sec->Name, ".text", 5) == 0) {
            text_rva = sec->VirtualAddress;
            text_raw = sec->PointerToRawData;
            text_size = sec->Misc.VirtualSize ? sec->Misc.VirtualSize : sec->SizeOfRawData;
            break;
        }
    }
    if (!text_size) {
        std::fprintf(stderr, ".text section not found\n");
        return 1;
    }

    // Fake image base pointer such that (img_base + text_rva) == &raw[text_raw]
    const uint8_t* fake_base = raw.data() + text_raw - text_rva;
    LARGE_INTEGER freq{}, t0{}, t1{};
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);

    uint32_t target_rva = 0;
    bool ok = wmm::detail::resolve_rsa_thunk(fake_base, text_rva, text_size, target_rva);

    QueryPerformanceCounter(&t1);
    double ms = static_cast<double>(t1.QuadPart - t0.QuadPart) * 1000.0 / static_cast<double>(freq.QuadPart);
    std::printf("resolve_rsa_thunk: ok=%s target_rva=0x%08X elapsed=%.2fms\n", ok ? "true" : "false", target_rva, ms);
    return ok ? 0 : 1;
}
