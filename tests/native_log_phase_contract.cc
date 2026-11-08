// Actual Linux filesystem/inotify contract. No ns-3 runtime, GPU, PHY or hardware.
#include "native-log-phase.h"
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#ifdef __linux__
#include <fcntl.h>
#include <unistd.h>
#endif

namespace fs=std::filesystem;
namespace
{
size_t checks=0;
void Require(bool ok,const char* why) { ++checks; if(!ok) throw std::runtime_error(why); }
void Write(const fs::path& path,const std::string& text)
{
    std::ofstream out(path,std::ios::trunc); out<<text; out.close();
    Require(static_cast<bool>(out),"fixture write failed");
}
std::string Read(const fs::path& path)
{
    std::ifstream input(path);std::string value;
    if(input.is_open())std::getline(input,value);
    for(auto& c:value)if(c==',')c='_';
    return value.empty()?"unclassified":value;
}
}

int main()
{
#ifndef __linux__
    std::cout<<"{\"status\":\"skipped\",\"reason\":\"Linux inotify contract\"}\n";
    return 0;
#else
    fs::path directory;
    try
    {
        std::string pattern=(fs::exists("/dev/shm")?"/dev/shm":"/tmp")+std::string("/bas-log-phase-XXXXXX");
        auto* name=mkdtemp(pattern.data());Require(name!=nullptr,"mkdtemp failed");directory=name;
        struct Cleanup {fs::path path;~Cleanup(){std::error_code error;fs::remove_all(path,error);}} cleanup{directory};
        const auto path=directory/"current_phase.txt";
        Write(path,"preflight\n");size_t reads=0;
        bas::NativeLogPhase cache(path.string(),[&]{++reads;return Read(path);});
        Require(cache.Get()=="preflight","initial phase wrong");
        Require(cache.GetStats().watchActive,"inotify watch not active");
        const int descriptor=cache.GetStats().notificationFd;
        Require((fcntl(descriptor,F_GETFL)&O_NONBLOCK)!=0,"descriptor not nonblocking");
        Require((fcntl(descriptor,F_GETFD)&FD_CLOEXEC)!=0,"descriptor not cloexec");
        for(int i=0;i<1000;++i)Require(cache.Get()=="preflight","unchanged phase changed");
        Require(reads==1,"unchanged phase repeatedly parsed file");
        Write(directory/"unrelated.txt","other\n");Require(cache.Get()=="preflight","unrelated file retagged phase");
        Require(reads==1 && cache.GetStats().ignoredEvents>0,"unrelated file triggered phase parse");
        const auto stamp=fs::last_write_time(path);
        Write(path,"secondone\n");fs::last_write_time(path,stamp);
        Require(cache.Get()=="secondone","same size/mtime rewrite was hidden");
        Write(path,"");Require(cache.Get()=="unclassified","truncate returned stale phase");
        Write(directory/"new.tmp","replacement\n");fs::rename(directory/"new.tmp",path);
        Require(cache.Get()=="replacement","atomic replace returned stale phase");
        fs::remove(path);Require(cache.Get()=="unclassified","delete returned stale phase");
        Write(path,"recreated\n");Require(cache.Get()=="recreated","recreate returned stale phase");
        fs::create_hard_link(path,directory/"alias");
        Require(cache.Get()=="recreated","link attribute event broke phase");
        Write(directory/"alias","via_alias\n");Require(cache.Get()=="via_alias","hardlink write bypassed inode watch");
        fs::remove(directory/"alias");
        for(int i=0;i<100;++i)Write(path,"burst"+std::to_string(i)+"\n");
        Require(cache.Get()=="burst99","burst did not return final observed contents");
        size_t maxQueued=0;std::ifstream("/proc/sys/fs/inotify/max_queued_events")>>maxQueued;
        bool overflowTested=false;
        if(maxQueued>0 && maxQueued<=65536)
        {
            cache.Get();
            for(size_t i=0;i<maxQueued+128;++i)
            {
                const auto other=directory/("overflow-"+std::to_string(i));
                int fd=open(other.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
                Require(fd>=0,"overflow fixture open failed");close(fd);fs::remove(other);
            }
            Write(path,"after_overflow\n");
            Require(cache.Get()=="after_overflow","overflow served stale contents");
            Require(cache.GetStats().overflows>0,"actual queue overflow not observed");overflowTested=true;
        }
        // Replacement of the watched directory invalidates the cache and must
        // keep using the original callback, including later path recreation.
        const auto gone=directory/"watched";fs::create_directory(gone);const auto fallbackPath=gone/"phase";
        Write(fallbackPath,"one\n");size_t fallbackReads=0;
        bas::NativeLogPhase fallback(fallbackPath.string(),[&]{++fallbackReads;return Read(fallbackPath);});
        Require(fallback.Get()=="one","fallback initial read failed");
        fs::remove(fallbackPath);fs::remove(gone);
        Require(fallback.Get()=="unclassified","lost watch reused previous value");
        fs::create_directory(gone);Write(fallbackPath,"two\n");
        Require(fallback.Get()=="two" && fallback.Get()=="two","failed watch did not use uncached callback");
        Require(fallback.GetStats().fallbackCalls>=3 && !fallback.GetStats().lastFallbackReason.empty(),"fallback invisible in metrics");
        cache.Close();errno=0;
        Require(fcntl(cache.GetStats().notificationFd,F_GETFD)==-1,"closed descriptor retained");
        Require(cache.GetStats().closed && !cache.GetStats().watchActive,"close state wrong");
        Write(path,"after_close\n");Require(cache.Get()=="after_close","closed owner returned cached data");
        int owned=-1;
        {bas::NativeLogPhase scoped(path.string(),[&]{return Read(path);});scoped.Get();owned=scoped.GetStats().notificationFd;}
        errno=0;Require(fcntl(owned,F_GETFD)==-1 && errno==EBADF,"RAII leaked notification descriptor");
        bas::NativeLogPhase bench(path.string(),[&]{return Read(path);});bench.Get();
        const int count=50000;size_t consumed=0;
        auto start=std::chrono::steady_clock::now();for(int i=0;i<count;++i)consumed+=Read(path).size();
        const auto baseline=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
        start=std::chrono::steady_clock::now();for(int i=0;i<count;++i)consumed+=bench.Get().size();
        const auto cached=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
        Require(consumed>0 && bench.GetStats().reloads==1,"benchmark failed or reparsed unchanged input");
        std::cout<<"{\"status\":\"passed\",\"checks\":"<<checks<<",\"overflow_tested\":"<<(overflowTested?"true":"false")
                 <<",\"microbenchmark_calls\":"<<count<<",\"baseline_ns\":"<<baseline<<",\"cached_ns\":"<<cached
                 <<",\"scope\":\"CPU filesystem boundary; no RF or realtime SLA claim\"}\n";
        return 0;
    }
    catch(const std::exception& error)
    {std::cerr<<"FAIL log phase: "<<error.what()<<'\n';return 1;}
#endif
}
