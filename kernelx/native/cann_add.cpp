// Small ACLNN Add performance case; all initialization and warmup precede profiling.
#include <acl/acl.h>
#include <acl/acl_prof.h>
#include <aclnnop/aclnn_add.h>
#include <chrono>
#include <cstdlib>
#include <dlfcn.h>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

static void check(int code, const char* name) {
    if (code != 0) throw std::runtime_error(std::string(name) + " failed: " + std::to_string(code));
}
#define CHECK(x) check((x), #x)
static std::string quote(const std::string& text) {
    std::string s = "\"";
    for (char c: text) { if (c == '\\' || c == '"') s += '\\'; if (c == '\n') s += "\\n"; else s += c; }
    return s + "\"";
}
static uint64_t ns() { return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count(); }
int main(int argc, char** argv) {
    if (argc != 6) { std::cerr << "device warmup repeats profile_dir sidecar\n"; return 2; }
    try {
        uint32_t device = std::stoul(argv[1]); int warmup = std::stoi(argv[2]), repeats = std::stoi(argv[3]);
        if (warmup < 1 || repeats < 1 || repeats > 1000) throw std::runtime_error("invalid repetitions");
        std::ofstream sidecar(argv[5]);
        if (!sidecar) throw std::runtime_error("cannot open sidecar");
        CHECK(aclInit(nullptr)); CHECK(aclrtSetDevice(device));
        aclrtStream stream = nullptr; CHECK(aclrtCreateStream(&stream));
        int64_t shape[] = {4096}, stride[] = {1};
        std::vector<float> host(4096, 1.0f);
        void *a=nullptr, *b=nullptr, *out=nullptr;
        CHECK(aclrtMalloc(&a, host.size()*sizeof(float), ACL_MEM_MALLOC_NORMAL_ONLY));
        CHECK(aclrtMalloc(&b, host.size()*sizeof(float), ACL_MEM_MALLOC_NORMAL_ONLY));
        CHECK(aclrtMalloc(&out, host.size()*sizeof(float), ACL_MEM_MALLOC_NORMAL_ONLY));
        CHECK(aclrtMemcpy(a, host.size()*sizeof(float), host.data(), host.size()*sizeof(float), ACL_MEMCPY_HOST_TO_DEVICE));
        CHECK(aclrtMemcpy(b, host.size()*sizeof(float), host.data(), host.size()*sizeof(float), ACL_MEMCPY_HOST_TO_DEVICE));
        auto x=aclCreateTensor(shape,1,ACL_FLOAT,stride,0,ACL_FORMAT_ND,shape,1,a);
        auto y=aclCreateTensor(shape,1,ACL_FLOAT,stride,0,ACL_FORMAT_ND,shape,1,b);
        auto z=aclCreateTensor(shape,1,ACL_FLOAT,stride,0,ACL_FORMAT_ND,shape,1,out);
        float alpha_value=1.0f; auto alpha=aclCreateScalar(&alpha_value, ACL_FLOAT);
        if (!x || !y || !z || !alpha) throw std::runtime_error("tensor/scalar creation failed");
        for (int i=0; i<warmup; ++i) {
            uint64_t warmup_start=ns();
            uint64_t bytes=0; aclOpExecutor* exec=nullptr;
            CHECK(aclnnAddGetWorkspaceSize(x,y,alpha,z,&bytes,&exec));
            void* ws=nullptr; if (bytes) CHECK(aclrtMalloc(&ws,bytes,ACL_MEM_MALLOC_NORMAL_ONLY));
            CHECK(aclnnAdd(ws,bytes,exec,stream)); CHECK(aclrtSynchronizeStream(stream));
            uint64_t warmup_end=ns();
            if (ws) CHECK(aclrtFree(ws));
            sidecar << "{\"phase\":\"WARMUP\",\"iteration\":" << i << ",\"rank\":0,\"profile_active\":false,\"start_monotonic_ns\":" << warmup_start
                    << ",\"end_monotonic_ns\":" << warmup_end << ",\"host_elapsed_us\":" << (warmup_end-warmup_start)/1000.0 << "}\n";
        }
        // Prepare invocation executors/workspaces outside the profiling interval.
        std::vector<aclOpExecutor*> execs(repeats); std::vector<uint64_t> sizes(repeats);
        uint64_t max_bytes=0;
        for (int i=0;i<repeats;++i) { CHECK(aclnnAddGetWorkspaceSize(x,y,alpha,z,&sizes[i],&execs[i])); if(sizes[i]>max_bytes) max_bytes=sizes[i]; }
        void* workspace=nullptr; if(max_bytes) CHECK(aclrtMalloc(&workspace,max_bytes,ACL_MEM_MALLOC_NORMAL_ONLY));
        std::string path=argv[4]; CHECK(aclprofInit(path.c_str(),path.size()));
        auto config=aclprofCreateConfig(&device,1,ACL_AICORE_NONE,nullptr,
                                       ACL_PROF_TASK_TIME | ACL_PROF_ACL_API | ACL_PROF_MSPROFTX);
        if (!config) throw std::runtime_error("profiling config failed");
        sidecar << "{\"phase\":\"PROFILE_START\",\"rank\":0,\"warmup_completed\":" << warmup << "}\n"; sidecar.flush();
        CHECK(aclprofStart(config));
        // Explicit test-only fault injection for deadline/cancellation acceptance.
        // Ordinary runs have no pause and no extra phase.
        if (const char* pause=std::getenv("KERNELX_TEST_PAUSE_AFTER_PROFILE_START")) {
            int seconds=std::stoi(pause);
            if(seconds<1 || seconds>30) throw std::runtime_error("fault pause must be 1..30 seconds");
            sidecar << "{\"phase\":\"FAULT_INJECTION\",\"profile_active\":true,\"seconds\":" << seconds << "}\n"; sidecar.flush();
            std::this_thread::sleep_for(std::chrono::seconds(seconds));
        }
        for (int i=0; i<repeats; ++i) {
            std::string name="kernelx:measure:"+std::to_string(i);
            void* stamp=aclprofCreateStamp(); if (!stamp) throw std::runtime_error("stamp failed");
            CHECK(aclprofSetStampTraceMessage(stamp,name.c_str(),name.size()));
            uint32_t range=0; uint64_t start=ns(); CHECK(aclprofRangeStart(stamp,&range));
            CHECK(aclnnAdd(workspace,sizes[i],execs[i],stream)); CHECK(aclrtSynchronizeStream(stream));
            CHECK(aclprofRangeStop(range)); uint64_t end=ns(); aclprofDestroyStamp(stamp);
            sidecar << "{\"phase\":\"MEASURE\",\"iteration\":" << i << ",\"rank\":0,\"profile_active\":true,\"range_name\":" << quote(name)
                    << ",\"start_monotonic_ns\":" << start << ",\"end_monotonic_ns\":" << end << "}\n"; sidecar.flush();
        }
        CHECK(aclprofStop(config)); CHECK(aclprofDestroyConfig(config)); CHECK(aclprofFinalize());
        sidecar << "{\"phase\":\"PROFILE_STOP\",\"rank\":0,\"measured_iterations\":" << repeats << "}\n"; sidecar.flush();
        void* symbol=dlsym(RTLD_DEFAULT,"aclnnAdd");
        Dl_info info{}; if (!symbol || !dladdr(symbol, &info)) throw std::runtime_error("dladdr failed");
        std::ifstream maps("/proc/self/maps"); std::set<std::string> libraries; std::string line;
        while(std::getline(maps,line)) { auto pos=line.find('/'); if(pos!=std::string::npos && line.find(".so",pos)!=std::string::npos) libraries.insert(line.substr(pos)); }
        std::ofstream providers(std::string(argv[5])+".providers.json");
        providers << "{\"api_symbol\":\"aclnnAdd\",\"api_path\":" << quote(info.dli_fname) << ",\"loaded_libraries\":[";
        bool first=true; for(auto& lib:libraries) { if(!first) providers << ','; first=false; providers << quote(lib); } providers << "]}\n";
        if(workspace) CHECK(aclrtFree(workspace));
        CHECK(aclDestroyScalar(alpha)); CHECK(aclDestroyTensor(x)); CHECK(aclDestroyTensor(y)); CHECK(aclDestroyTensor(z));
        CHECK(aclrtFree(a)); CHECK(aclrtFree(b)); CHECK(aclrtFree(out)); CHECK(aclrtDestroyStream(stream));
        // Releases this process's device context; does not reset the shared board.
        CHECK(aclrtResetDevice(device)); CHECK(aclFinalize());
        sidecar << "{\"phase\":\"RELEASED\",\"rank\":0}\n";
        return 0;
    } catch (const std::exception& exc) { std::cerr << exc.what() << '\n'; return 1; }
}
