// Standalone WMM Pak Enabler proxy DLL (dsound.dll).
// Pure Win32, zero STL/CRT locks in DllMain. Forwards all 12 DirectSound exports
// to System32\dsound.dll and enables unsigned ~mods paks on DLL_PROCESS_ATTACH.

#include "wmm_pak_enabler.hpp"

namespace {

volatile PVOID g_real_dsound = nullptr;

HMODULE load_real_dsound() {
    HMODULE mod = reinterpret_cast<HMODULE>(g_real_dsound);
    if (mod) return mod;
    wchar_t sys_dir[MAX_PATH] = {};
    UINT len = GetSystemDirectoryW(sys_dir, MAX_PATH);
    wchar_t path[MAX_PATH + 32];
    std::swprintf(path, MAX_PATH + 32, L"%.*ls\\dsound.dll", static_cast<int>(len), sys_dir);
    HMODULE loaded = LoadLibraryW(path);
    if (loaded) {
        if (InterlockedCompareExchangePointer(&g_real_dsound, loaded, nullptr) != nullptr) {
            FreeLibrary(loaded);
        }
    }
    return reinterpret_cast<HMODULE>(g_real_dsound);
}

FARPROC get_real_proc(const char* name) {
    HMODULE mod = load_real_dsound();
    return mod ? GetProcAddress(mod, name) : nullptr;
}

}  // namespace

extern "C" {

HRESULT WINAPI proxy_DirectSoundCreate(const void* a, void* b, void* c) {
    using Fn = HRESULT(WINAPI*)(const void*, void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundCreate"));
    return fn ? fn(a, b, c) : E_FAIL;
}

HRESULT WINAPI proxy_DirectSoundEnumerateA(void* a, void* b) {
    using Fn = HRESULT(WINAPI*)(void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundEnumerateA"));
    return fn ? fn(a, b) : E_FAIL;
}

HRESULT WINAPI proxy_DirectSoundEnumerateW(void* a, void* b) {
    using Fn = HRESULT(WINAPI*)(void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundEnumerateW"));
    return fn ? fn(a, b) : E_FAIL;
}

HRESULT WINAPI proxy_DllCanUnloadNow(void) {
    using Fn = HRESULT(WINAPI*)(void);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DllCanUnloadNow"));
    return fn ? fn() : S_FALSE;
}

HRESULT WINAPI proxy_DllGetClassObject(const void* a, const void* b, void* c) {
    using Fn = HRESULT(WINAPI*)(const void*, const void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DllGetClassObject"));
    return fn ? fn(a, b, c) : CLASS_E_CLASSNOTAVAILABLE;
}

HRESULT WINAPI proxy_DirectSoundCaptureCreate(const void* a, void* b, void* c) {
    using Fn = HRESULT(WINAPI*)(const void*, void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundCaptureCreate"));
    return fn ? fn(a, b, c) : E_FAIL;
}

HRESULT WINAPI proxy_DirectSoundCaptureEnumerateA(void* a, void* b) {
    using Fn = HRESULT(WINAPI*)(void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundCaptureEnumerateA"));
    return fn ? fn(a, b) : E_FAIL;
}

HRESULT WINAPI proxy_DirectSoundCaptureEnumerateW(void* a, void* b) {
    using Fn = HRESULT(WINAPI*)(void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundCaptureEnumerateW"));
    return fn ? fn(a, b) : E_FAIL;
}

HRESULT WINAPI proxy_GetDeviceID(const void* a, void* b) {
    using Fn = HRESULT(WINAPI*)(const void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("GetDeviceID"));
    return fn ? fn(a, b) : E_FAIL;
}

HRESULT WINAPI proxy_DirectSoundFullDuplexCreate(
    const void* a, const void* b, const void* c, const void* d,
    HWND e, DWORD f, void* g, void* h, void* i, void* j) {
    using Fn = HRESULT(WINAPI*)(const void*, const void*, const void*, const void*, HWND, DWORD, void*, void*, void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundFullDuplexCreate"));
    return fn ? fn(a, b, c, d, e, f, g, h, i, j) : E_FAIL;
}

HRESULT WINAPI proxy_DirectSoundCreate8(const void* a, void* b, void* c) {
    using Fn = HRESULT(WINAPI*)(const void*, void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundCreate8"));
    return fn ? fn(a, b, c) : E_FAIL;
}

HRESULT WINAPI proxy_DirectSoundCaptureCreate8(const void* a, void* b, void* c) {
    using Fn = HRESULT(WINAPI*)(const void*, void*, void*);
    auto fn = reinterpret_cast<Fn>(get_real_proc("DirectSoundCaptureCreate8"));
    return fn ? fn(a, b, c) : E_FAIL;
}

}  // extern "C"

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinst);
        wmm::enable_unsigned_paks();
    } else if (reason == DLL_PROCESS_DETACH) {
        HMODULE mod = reinterpret_cast<HMODULE>(InterlockedExchangePointer(&g_real_dsound, nullptr));
        if (mod) FreeLibrary(mod);
    }
    return TRUE;
}
