// Real host datagrams exercise the native TAP reader's boundary exchange.
#include "ns3/tap-bridge.h"
#include "ns3/core-module.h"
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <linux/if_tun.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <net/ethernet.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <fstream>
#include <condition_variable>
#include <mutex>
#include <chrono>
#include <unistd.h>
#include <poll.h>
#include <stdexcept>
#include <cerrno>
#include <cstdlib>
#include <vector>
#include <iostream>
using namespace ns3;
static std::vector<std::vector<uint8_t>> received;
static std::vector<int64_t> deliveredAt;
static std::mutex queuedMutex;
static std::condition_variable queuedCondition;
static bool queued = false;
static void Delivered(uint8_t* bytes, ssize_t length) {
    received.emplace_back(bytes, bytes + length);
    deliveredAt.push_back(Simulator::Now().GetNanoSeconds());
    std::free(bytes);
}
static void Queued(uint8_t* bytes, ssize_t length) {
    Simulator::ScheduleWithContext(1, Seconds(0), &Delivered, bytes, length);
    { std::lock_guard<std::mutex> lock(queuedMutex); queued = true; }
    queuedCondition.notify_one();
}
static void TapPair(int (&fd)[2]) {
    fd[1] = open("/dev/net/tun", O_RDWR | O_NONBLOCK);
    ifreq interface{};
    std::strcpy(interface.ifr_name, "bas-tap-phase");
    interface.ifr_flags = IFF_TAP | IFF_NO_PI;
    if (fd[1] < 0 || ioctl(fd[1], TUNSETIFF, &interface)) throw std::runtime_error("TUNSETIFF");
    std::ofstream("/proc/sys/net/ipv6/conf/bas-tap-phase/disable_ipv6") << 1;
    int control = socket(AF_INET, SOCK_DGRAM, 0);
    interface.ifr_flags = IFF_UP;
    if (ioctl(control, SIOCSIFFLAGS, &interface) || ioctl(control, SIOCGIFINDEX, &interface))
        throw std::runtime_error("TAP link up");
    fd[0] = socket(AF_PACKET, SOCK_RAW | SOCK_NONBLOCK, htons(ETH_P_ALL));
    sockaddr_ll address{};
    address.sll_family = AF_PACKET;
    address.sll_protocol = htons(ETH_P_ALL);
    address.sll_ifindex = interface.ifr_ifindex;
    if (fd[0] < 0 || bind(fd[0], reinterpret_cast<sockaddr*>(&address), sizeof(address)))
        throw std::runtime_error("TAP sender bind");
    close(control);
}
static std::vector<uint8_t> Frame(int size, uint8_t sequence) {
    std::vector<uint8_t> result(size, sequence);
    const uint8_t ethernet[] = {2,0,0,0,0,2, 2,0,0,0,0,1, 0x88,0xb5};
    std::copy(std::begin(ethernet), std::end(ethernet), result.begin());
    return result;
}
int main(int argc, char** argv) {
    int fd[2];
    if (argc > 1 && std::string(argv[1]) == "--tap") TapPair(fd);
    else if (socketpair(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK, 0, fd)) return 1;
    int buffer = 1 << 20;
    setsockopt(fd[0], SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
    auto reader = Create<TapBridgeFdReader>();
    std::vector<std::vector<uint8_t>> expected;
    if (argc > 2 && std::string(argv[2]) == "--async-negative-control") {
        reader->Start(fd[1], MakeCallback(&Queued));
        Simulator::Schedule(MilliSeconds(5), [&]() {
            auto frame = Frame(1100, 250);
            if (send(fd[0], frame.data(), frame.size(), 0) < 0) throw std::runtime_error("send");
            std::unique_lock<std::mutex> lock(queuedMutex);
            if (!queuedCondition.wait_for(lock, std::chrono::seconds(1), [] { return queued; }))
                throw std::runtime_error("native reader did not schedule the frame");
        });
        Simulator::Stop(MilliSeconds(20));
        Simulator::Run();
        reader->Stop();
        Simulator::Destroy();
        close(fd[0]); close(fd[1]);
        if (deliveredAt.size() != 1) return 12;
        std::cout << "Asynchronous admission inside the radio interval: " << deliveredAt[0] << " ns\n";
        return deliveredAt[0] == 20'000'000 ? 0 : 13;
    }
    for (int i = 0; i < 100; ++i) {
        expected.push_back(Frame(800 + i, uint8_t(i)));
        if (send(fd[0], expected.back().data(), expected.back().size(), 0) < 0) return 2;
    }
    Simulator::Schedule(MilliSeconds(5), [&]() {
        expected.push_back(Frame(1100, 250));
        if (send(fd[0], expected.back().data(), expected.back().size(), 0) < 0)
            throw std::runtime_error("send");
    });
    Simulator::Stop(MilliSeconds(20));
    Simulator::Run();
    if (!received.empty()) return 3; // model progress alone must not admit host bytes
    if (reader->ReadAvailable(fd[1], MakeCallback(&Queued), 101) != 101) return 4;
    if (reader->ReadAvailable(fd[1], MakeCallback(&Queued), 101) != 0) return 5;
    if (!received.empty()) return 6; // bytes must go through stock simulator events
    Simulator::Stop(MilliSeconds(20));
    Simulator::Run();
    if (received != expected) return 7;
    if (!std::all_of(deliveredAt.begin(), deliveredAt.end(), [](auto at) { return at == 20'000'000; })) return 14;
    for (int i = 0; i < 3; ++i) {
        expected.push_back(Frame(64, uint8_t(100 + i)));
        send(fd[0], expected.back().data(), expected.back().size(), 0);
    }
    try { reader->ReadAvailable(fd[1], MakeCallback(&Queued), 2); return 8; }
    catch (const std::runtime_error&) {}
    if (reader->ReadAvailable(fd[1], MakeCallback(&Queued), 2) != 1) return 9;
    Simulator::Stop(MilliSeconds(20));
    Simulator::Run();
    if (received != expected) return 10;
    close(fd[1]);
    try { reader->ReadAvailable(fd[1], MakeCallback(&Queued), 2); return 11; }
    catch (const std::runtime_error&) {}
    close(fd[0]);
    Simulator::Destroy();
    std::cout << "PASS: 104 native datagrams; exact bytes/order, explicit boundary, overload and closed descriptor\n";
}
