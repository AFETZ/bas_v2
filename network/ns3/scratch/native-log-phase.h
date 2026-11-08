#pragma once
// Logging only. Control/source/fault readers must keep their uncached callback.
// One owner on the native main thread. No TTL, new phase parser or worker queue.
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <functional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#ifdef __linux__
#include <cerrno>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace bas
{
class NativeLogPhase
{
  public:
    struct Stats
    {
        uint64_t lookups{0},cacheHits{0},reloads{0},relevantEvents{0},ignoredEvents{0};
        uint64_t overflows{0},watchFailures{0},readErrors{0},fallbackCalls{0},rebinds{0};
        bool watchActive{false},closed{false};
        int notificationFd{-1},lastErrno{0};
        std::string lastFallbackReason;
    };
    NativeLogPhase(std::string path,std::function<std::string()> uncached)
        : m_path(std::move(path)),m_read(std::move(uncached))
    {
        if(!m_read)throw std::invalid_argument("Logging phase needs the original reader");
        const std::filesystem::path input(m_path);
        m_parent=input.parent_path().empty()?std::filesystem::path("."):input.parent_path();
        m_name=input.filename().string();
        SetUp();
    }
    ~NativeLogPhase(){Close();}
    NativeLogPhase(const NativeLogPhase&)=delete;
    NativeLogPhase& operator=(const NativeLogPhase&)=delete;
    NativeLogPhase(NativeLogPhase&&)=delete;
    NativeLogPhase& operator=(NativeLogPhase&&)=delete;

    const std::string& Get()
    {
        ++m_stats.lookups;
        if(m_stats.closed || m_failed)return Uncached();
        Drain();
        if(m_failed)return Uncached();
        if(m_dirty || !m_haveValue)
        {
            if(m_rebind)
            {
                ++m_stats.rebinds;
                SetUp(); // Rewatch before reading; also recovers after overflow.
                if(m_failed)return Uncached();
            }
            ++m_stats.reloads;
            m_value=m_read(); // Never retag/sanitize/invent a value here.
            m_haveValue=true;m_dirty=false;m_rebind=false;
        }
        else ++m_stats.cacheHits;
        return m_value;
    }
    Stats GetStats() const {return m_stats;}
    void Close() noexcept
    {
        CloseDescriptor();m_stats.closed=true;
    }

  private:
    void CloseDescriptor() noexcept
    {
#ifdef __linux__
        if(m_fd>=0)::close(m_fd);
#endif
        m_fd=-1;m_directoryWatch=-1;m_fileWatch=-1;
        m_stats.notificationFd=-1;m_stats.watchActive=false;
    }
    void Disable(const char* why,int error=0,bool watchFailure=false)
    {
        m_failed=true;m_stats.lastFallbackReason=why;m_stats.lastErrno=error;
        if(watchFailure)++m_stats.watchFailures;
        CloseDescriptor();
    }
    const std::string& Uncached()
    {
        ++m_stats.fallbackCalls;
        if(m_stats.closed)m_stats.lastFallbackReason="closed";
        m_value=m_read();return m_value;
    }
    void SetUp()
    {
        CloseDescriptor();
#ifdef __linux__
        if(m_name.empty()){Disable("invalid_basename");return;}
        // Symlinked parent paths can be retargeted without notifying the
        // watched inode. Keep original uncached semantics for that case.
        std::error_code error;
        const auto absolute=std::filesystem::absolute(m_parent,error).lexically_normal();
        if(error){Disable("parent_path_error",error.value(),true);return;}
        const auto canonical=std::filesystem::canonical(m_parent,error);
        if(error){Disable("parent_unavailable",error.value(),true);return;}
        if(canonical!=absolute){Disable("symlinked_parent");return;}
        m_fd=inotify_init1(IN_NONBLOCK|IN_CLOEXEC);
        if(m_fd<0){Disable("inotify_init_failed",errno,true);return;}
        constexpr uint32_t changes=IN_MODIFY|IN_ATTRIB|IN_CLOSE_WRITE|IN_CREATE|IN_DELETE|IN_MOVED_TO|IN_MOVED_FROM;
        m_directoryWatch=inotify_add_watch(m_fd,m_parent.c_str(),changes|IN_DELETE_SELF|IN_MOVE_SELF|IN_UNMOUNT|IN_ONLYDIR);
        if(m_directoryWatch<0){Disable("directory_watch_failed",errno,true);return;}
        struct stat status{};
        if(lstat(m_path.c_str(),&status)==0)
        {
            if(!S_ISREG(status.st_mode)){Disable("unsupported_phase_file_type");return;}
            // The inode watch also catches writes through a hardlink outside
            // this directory. Parent watch owns deletion/atomic replacement.
            m_fileWatch=inotify_add_watch(m_fd,m_path.c_str(),IN_MODIFY|IN_ATTRIB|IN_CLOSE_WRITE|IN_DELETE_SELF|IN_MOVE_SELF|IN_DONT_FOLLOW);
            if(m_fileWatch<0 && errno!=ENOENT){Disable("file_watch_failed",errno,true);return;}
        }
        else if(errno!=ENOENT){Disable("phase_stat_failed",errno,true);return;}
        m_stats.notificationFd=m_fd;m_stats.watchActive=true;
#else
        Disable("inotify_unsupported_platform");
#endif
    }
    void Drain()
    {
#ifdef __linux__
        alignas(inotify_event) char buffer[16384];
        for(unsigned reads=0;reads<64;++reads)
        {
            const auto size=::read(m_fd,buffer,sizeof(buffer));
            if(size<0)
            {
                if(errno==EINTR)continue;
                if(errno==EAGAIN || errno==EWOULDBLOCK)return;
                ++m_stats.readErrors;Disable("notification_read_failed",errno);return;
            }
            if(size==0){++m_stats.readErrors;Disable("notification_eof");return;}
            size_t offset=0;
            while(offset<static_cast<size_t>(size))
            {
                inotify_event event{};
                if(static_cast<size_t>(size)-offset<sizeof(event)){Disable("malformed_notification");return;}
                std::memcpy(&event,buffer+offset,sizeof(event));
                const auto record=sizeof(event)+static_cast<size_t>(event.len);
                if(record>static_cast<size_t>(size)-offset){Disable("malformed_notification");return;}
                std::string_view name;
                if(event.len)
                {
                    const char* first=buffer+offset+sizeof(event);
                    const char* end=static_cast<const char*>(std::memchr(first,0,event.len));
                    if(!end){Disable("malformed_notification_name");return;}
                    name=std::string_view(first,static_cast<size_t>(end-first));
                }
                offset+=record;
                if(event.mask&IN_Q_OVERFLOW)
                {++m_stats.overflows;m_dirty=true;m_rebind=true;continue;}
                if(event.wd==m_directoryWatch)
                {
                    if(event.mask&(IN_DELETE_SELF|IN_MOVE_SELF|IN_UNMOUNT|IN_IGNORED))
                    {Disable("directory_watch_invalidated");return;}
                    if(name!=m_name && !name.empty()){++m_stats.ignoredEvents;continue;}
                }
                else if(event.wd!=m_fileWatch){Disable("unknown_watch_event");return;}
                constexpr uint32_t known=IN_MODIFY|IN_ATTRIB|IN_CLOSE_WRITE|IN_CREATE|IN_DELETE|IN_MOVED_TO|IN_MOVED_FROM|
                                         IN_DELETE_SELF|IN_MOVE_SELF|IN_IGNORED|IN_ISDIR;
                if(event.mask&~known){Disable("unknown_notification_mask");return;}
                ++m_stats.relevantEvents;m_dirty=true;m_rebind=true;
            }
        }
        // A continuous event storm cannot block the simulator indefinitely.
        Disable("notification_drain_bound");
#endif
    }
    std::string m_path,m_name,m_value;
    std::filesystem::path m_parent;
    std::function<std::string()> m_read;
    Stats m_stats;
    int m_fd{-1},m_directoryWatch{-1},m_fileWatch{-1};
    bool m_failed{false},m_dirty{true},m_haveValue{false},m_rebind{false};
};
}
