// Macrostep barrier around stock ns-3 DefaultSimulatorImpl, not a new scheduler.
#pragma once
#include "pybind11/embed.h"
#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>
#include <stdexcept>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

namespace bas {
class Lockstep {
public:
    Lockstep(std::string path, double timeoutS, int64_t stepNs)
        : m_path(std::move(path)), m_timeoutS(timeoutS), m_stepNs(stepNs) {
        m_lock=open((m_path+".lock").c_str(), O_CREAT|O_RDWR, 0600);
        if (m_lock<0) throw std::runtime_error("cannot open lockstep I/O barrier");
        auto ack=Wait(0);
        m_originNs=ack["source_origin_ns"].cast<int64_t>();
        if (m_stepNs % ack["physics_step_ns"].cast<int64_t>())
            throw std::runtime_error("coupling step must be a multiple of Gazebo physics step");
    }
    ~Lockstep() { if(m_lock>=0) close(m_lock); }
    double SourceTime(double nativeS) const { return m_originNs/1e9+nativeS; }
    void Clock(int64_t ns, const std::string& phase, bool healthy) {
        std::ofstream out(m_path+".tmp");
        out << "{\"mode\":\"lockstep\",\"simulation_ns\":" << ns
            << ",\"monotonic_ns\":" << WallNs() << ",\"healthy\":" << (healthy?"true":"false")
            << ",\"phase\":\"" << phase << "\",\"step_ns\":" << m_stepNs << "}\n";
        out.close(); std::filesystem::rename(m_path+".tmp",m_path);
    }
    void Begin(int64_t ns, bool healthy) {
        Clock(ns,"barrier",healthy);
        const auto start=WallNs();
        while (flock(m_lock, LOCK_EX|LOCK_NB)!=0) {
            if ((WallNs()-start)/1e9>m_timeoutS) throw std::runtime_error("UART barrier timeout");
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        Clock(ns,"radio",healthy);
    }
    void Physics(int64_t targetNs, bool healthy) {
        Clock(targetNs-m_stepNs,"physics",healthy);
        std::ofstream out(m_path+".request.tmp");
        out << "{\"target_ns\":" << targetNs << "}\n"; out.close();
        std::filesystem::rename(m_path+".request.tmp",m_path+".request");
        Wait(targetNs);
        Clock(targetNs,"exchange",healthy);
        flock(m_lock,LOCK_UN);
        // Real TAP/PTY and endpoint processes need an I/O opportunity at each
        // barrier. This is sampled co-simulation, not bitwise replay or HIL.
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    static int64_t WallNs() {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    }
private:
    pybind11::dict Wait(int64_t target) {
        const auto start=WallNs();
        while ((WallNs()-start)/1e9<m_timeoutS) {
            std::ifstream in(m_path+".ack");
            if(in) {
                std::string text((std::istreambuf_iterator<char>(in)),{});
                auto ack=pybind11::module_::import("json").attr("loads")(text).cast<pybind11::dict>();
                if (!ack["fault"].is_none()) throw std::runtime_error(ack["fault"].cast<std::string>());
                const auto done=ack["target_ns"].cast<int64_t>();
                if(done>target) throw std::runtime_error("physics barrier overshoot");
                if(done==target) return ack;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        throw std::runtime_error("Gazebo/ROS lockstep barrier timeout");
    }
    std::string m_path;
    double m_timeoutS;
    int64_t m_stepNs, m_originNs{0};
    int m_lock{-1};
};
}
